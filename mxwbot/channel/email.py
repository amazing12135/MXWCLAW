"""邮件频道适配器 — IMAP 轮询 + SMTP 发送（标准库）。

仅依赖 Python 标准库（imaplib / smtplib / email），无需第三方 SDK。
遵循 BaseChannel 模板方法契约：实现 4 个抽象方法，不覆写 send()/on_message()。
"""

from __future__ import annotations

import asyncio
import html as _html
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any

from mxwbot.channel.base import BaseChannel
from mxwbot.bus.messages import InboundMessage

logger = logging.getLogger("mxwbot.channel.email")

# IMAP reconnect detection markers (lowercase substrings of error messages)
_IMAP_STALE_MARKERS = (
    "disconnected for inactivity",
    "eof occurred in violation of protocol",
    "socket error",
    "connection reset",
    "broken pipe",
    "bye",
)
_MAX_PROCESSED_UIDS = 100_000


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class EmailConfig:
    """邮件频道配置。

    Attributes:
        imap_host / imap_port / imap_username / imap_password:
            IMAP 收件服务器配置。
        imap_use_ssl: True → IMAP4_SSL (993), False → IMAP4 + STARTTLS (143)。
        imap_mailbox: 监听的邮箱文件夹，默认 "INBOX"。
        smtp_host / smtp_port / smtp_username / smtp_password:
            SMTP 发件服务器配置。
        smtp_use_ssl: True → SMTP_SSL (465), False → SMTP + STARTTLS (587)。
        from_address: 发件人地址（From 头）。
        poll_interval_seconds: IMAP 轮询间隔。
        mark_seen: True → 读取后标记 SEEN（去重+不重复提醒）。
        max_body_chars: 正文截断长度。
        subject_prefix: 回复主题前缀，默认 "Re: "。
    """

    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_use_ssl: bool = True
    imap_mailbox: str = "INBOX"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = False
    from_address: str = ""

    poll_interval_seconds: int = 30
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "


# ---------------------------------------------------------------------------
# IMAP connection context manager
# ---------------------------------------------------------------------------

class _imap_connection:
    """IMAP 连接上下文管理器 — 自动连接登录 + 安全 logout。

    Usage::

        with _imap_connection(channel) as conn:
            channel._fetch_unread_once(conn)
    """

    def __init__(self, channel: EmailChannel) -> None:
        self._channel = channel
        self._conn: imaplib.IMAP4 | None = None

    def __enter__(self) -> imaplib.IMAP4:
        self._conn = self._channel._connect_imap()
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
        return None  # Do not suppress exceptions


# ---------------------------------------------------------------------------
# EmailChannel
# ---------------------------------------------------------------------------

class EmailChannel(BaseChannel):
    """邮件频道 — IMAP 轮询 + SMTP 发送。

    使用方式::

        channel = EmailChannel(bus, config=EmailConfig(
            imap_host="imap.gmail.com", imap_username="...", imap_password="...",
            smtp_host="smtp.gmail.com", smtp_username="...", smtp_password="...",
            from_address="bot@gmail.com",
        ))
        await channel.start()   # 启动 IMAP 轮询
        await channel.send(msg)
        await channel.stop()
    """

    name = "email"

    def __init__(self, bus, config: EmailConfig | None = None) -> None:
        super().__init__(bus)
        self._cfg = config or EmailConfig()
        self._poll_task: asyncio.Task | None = None

        # 邮件线程缓存: sender_addr → last_subject / last_message_id
        self._last_subject: dict[str, str] = {}
        self._last_msg_id: dict[str, str] = {}

        # UID 去重集合 + 上限驱逐
        self._processed_uids: set[str] = set()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _check_config(self) -> bool:
        """检查配置完整性，缺失项记录错误日志。"""
        missing = []
        for k in ("imap_host", "imap_username", "imap_password",
                   "smtp_host", "smtp_username", "smtp_password"):
            if not getattr(self._cfg, k, ""):
                missing.append(k)
        if missing:
            logger.error(
                "Email channel not configured, missing: %s",
                ", ".join(missing),
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        """校验配置 → 启动 IMAP 轮询循环。"""
        if not self._check_config():
            logger.error("Email channel: configuration incomplete, not starting")
            return

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "Email channel started (IMAP %s:%d, poll every %ds)",
            self._cfg.imap_host, self._cfg.imap_port,
            self._cfg.poll_interval_seconds,
        )

    async def _poll_loop(self) -> None:
        """主轮询循环 — asyncio.sleep 间隔 + 失败重试。"""
        interval = max(5, self._cfg.poll_interval_seconds)
        while self._running:
            try:
                messages = await asyncio.to_thread(self._fetch_unread)
                for m in messages:
                    await self._process_fetched(m)
            except Exception:
                if self._running:
                    logger.warning(
                        "Email poll error, retrying in %ds", interval,
                    )
            await asyncio.sleep(interval)

    async def _stop(self) -> None:
        """停止轮询。"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None
        logger.info("Email channel stopped")

    # ------------------------------------------------------------------
    # IMAP: fetch unread
    # ------------------------------------------------------------------

    def _connect_imap(self) -> imaplib.IMAP4:
        """创建 IMAP 连接并登录。

        SSL 模式下使用 ``ssl.create_default_context()`` 校验主机名。
        若服务器使用自签名证书，catch ``ssl.SSLError`` 后 fallback
        不校验并记录 warning。
        """
        if self._cfg.imap_use_ssl:
            ctx = ssl.create_default_context()
            try:
                conn = imaplib.IMAP4_SSL(
                    self._cfg.imap_host, self._cfg.imap_port, ssl_context=ctx,
                )
                conn.login(self._cfg.imap_username, self._cfg.imap_password)
                return conn
            except ssl.SSLError:
                logger.warning(
                    "Email: IMAP SSL verification failed for %s:%d, "
                    "retrying without verification (self-signed cert?)",
                    self._cfg.imap_host, self._cfg.imap_port,
                )
                conn = imaplib.IMAP4_SSL(self._cfg.imap_host, self._cfg.imap_port)
                conn.login(self._cfg.imap_username, self._cfg.imap_password)
                return conn
        else:
            conn = imaplib.IMAP4(self._cfg.imap_host, self._cfg.imap_port)
            conn.login(self._cfg.imap_username, self._cfg.imap_password)
            return conn

    def _fetch_unread(self) -> list[dict[str, Any]]:
        """同步 IMAP 操作 — 获取 UNSEEN 消息列表。

        连接和登出由 ``_imap_connection`` 上下文管理器处理；
        stale 连接检测触发自动重试（最多 2 次）。
        """
        for attempt in range(2):
            try:
                with _imap_connection(self) as conn:
                    return self._fetch_unread_once(conn)
            except Exception as exc:
                if attempt == 1 or not _is_stale_error(exc):
                    raise
                logger.warning("Email IMAP stale, reconnecting: %s", exc)
        return []

    def _fetch_unread_once(self, conn: imaplib.IMAP4) -> list[dict[str, Any]]:
        """在已连接 IMAP 上执行一次 UNSEEN 搜索 + 获取。"""
        mailbox = self._cfg.imap_mailbox or "INBOX"
        status, _ = conn.select(mailbox)
        if status != "OK":
            logger.warning("Email: IMAP SELECT %s returned %s", mailbox, status)
            return []

        status, data = conn.search(None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return []

        ids = data[0].split()
        results: list[dict[str, Any]] = []

        for msg_id in ids[-50:]:  # 每次最多处理 50 封
            fetch_result = self._fetch_one(conn, msg_id)
            if fetch_result:
                results.append(fetch_result)

        return results

    def _fetch_one(
        self, conn: imaplib.IMAP4, imap_id: bytes,
    ) -> dict[str, Any] | None:
        """获取并解析单封邮件。"""
        status, fetched = conn.fetch(imap_id, "(BODY.PEEK[] UID)")
        if status != "OK" or not fetched:
            return None

        raw_bytes = _extract_body_bytes(fetched)
        if raw_bytes is None:
            return None

        uid = _extract_uid(fetched)

        # UID 去重
        if uid and uid in self._processed_uids:
            return None

        parsed = BytesParser().parsebytes(raw_bytes)
        sender = parseaddr(parsed.get("From", ""))[1].strip().lower()
        if not sender:
            return None

        subject = _decode_header(parsed.get("Subject", ""))
        message_id = parsed.get("Message-ID", "").strip()
        body = _extract_text_body(parsed)
        if not body:
            body = "(empty body)"

        body = body[:self._cfg.max_body_chars]

        if uid:
            self._processed_uids.add(uid)
            if len(self._processed_uids) > _MAX_PROCESSED_UIDS:
                # 驱逐一半
                self._processed_uids = set(
                    list(self._processed_uids)[len(self._processed_uids) // 2:]
                )

        if self._cfg.mark_seen:
            try:
                conn.store(imap_id, "+FLAGS", "\\Seen")
            except Exception:
                pass

        return {
            "sender": sender,
            "subject": subject,
            "message_id": message_id,
            "uid": uid,
            "body": body,
        }

    # ------------------------------------------------------------------
    # Inbound: process → Bus
    # ------------------------------------------------------------------

    async def _process_fetched(self, m: dict) -> None:
        """将获取到的邮件转为 InboundMessage 投递 Bus。"""
        sender = m["sender"]
        subject = m["subject"]
        message_id = m["message_id"]

        # 缓存线程信息
        if subject:
            self._last_subject[sender] = subject
        if message_id:
            self._last_msg_id[sender] = message_id

        content = (
            f"[EMAIL] From: {sender}\n"
            f"Subject: {subject}\n\n"
            f"{m['body']}"
        )

        await self.bus.publish_inbound(InboundMessage(
            msg_type="message",
            channel=self.name,
            chat_id=sender,
            content=content,
            idempotency_key=f"{self.name}:{sender}:{m.get('uid', '')}",
        ))

    # ------------------------------------------------------------------
    # Outbound: SMTP send
    # ------------------------------------------------------------------

    async def _send_text(self, chat_id: str, text: str) -> None:
        """通过 SMTP 发送纯文本邮件。

        chat_id 为收件人邮箱地址。自动添加 Subject/In-Reply-To 实现线程。
        """
        if not self._cfg.smtp_host:
            logger.warning("Email: SMTP host not configured")
            return

        to_addr = chat_id.strip()
        if not to_addr:
            logger.warning("Email: missing recipient address")
            return

        # 构建邮件
        msg = EmailMessage()
        msg["From"] = self._cfg.from_address or self._cfg.smtp_username
        msg["To"] = to_addr

        # 回复线程
        base_subject = self._last_subject.get(to_addr, "")
        msg["Subject"] = _reply_subject(base_subject, self._cfg.subject_prefix)
        in_reply_to = self._last_msg_id.get(to_addr, "")
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        msg.set_content(text)

        try:
            await asyncio.to_thread(self._smtp_send, msg)
        except Exception:
            logger.error("Email send failed to=%s...", to_addr[:4])

    def _smtp_send(self, msg: EmailMessage) -> None:
        """同步 SMTP 发送（由 asyncio.to_thread 在后台线程执行）。"""
        timeout = 30
        if self._cfg.smtp_use_ssl:
            # SMTP_SSL (typically port 465)
            with smtplib.SMTP_SSL(
                self._cfg.smtp_host, self._cfg.smtp_port, timeout=timeout,
            ) as smtp:
                smtp.login(self._cfg.smtp_username, self._cfg.smtp_password)
                smtp.send_message(msg)
        else:
            # SMTP + STARTTLS (port 587)
            with smtplib.SMTP(
                self._cfg.smtp_host, self._cfg.smtp_port, timeout=timeout,
            ) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self._cfg.smtp_username, self._cfg.smtp_password)
                smtp.send_message(msg)

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def supports_interactive_buttons(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Helpers: IMAP protocol
# ---------------------------------------------------------------------------

def _is_stale_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _IMAP_STALE_MARKERS)


def _extract_body_bytes(fetched: list[Any]) -> bytes | None:
    """从 IMAP FETCH 返回列表中提取邮件原始字节。"""
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2:
            payload = item[1]
            if isinstance(payload, (bytes, bytearray)):
                return bytes(payload)
    return None


def _extract_uid(fetched: list[Any]) -> str:
    """从 IMAP FETCH 返回列表的响应头中提取 UID。"""
    for item in fetched:
        if isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
            head = bytes(item[0]).decode("utf-8", errors="ignore")
            m = re.search(r"UID\s+(\d+)", head)
            if m:
                return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Helpers: MIME decoding
# ---------------------------------------------------------------------------

def _decode_header(value: str) -> str:
    """解码 MIME 编码的邮件头（=?UTF-8?B?...?=）。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_text_body(parsed_msg: Any) -> str:
    """从 MIME 邮件中提取可读纯文本正文。

    - multipart → text/plain 优先 → text/html 转纯文本
    - 跳过 attachment 部分
    """
    if parsed_msg.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in parsed_msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            payload = _safe_get_content(part)
            if not isinstance(payload, str):
                continue
            if content_type == "text/plain":
                plain_parts.append(payload)
            elif content_type == "text/html":
                html_parts.append(payload)

        if plain_parts:
            return "\n\n".join(plain_parts).strip()
        if html_parts:
            return _html_to_text("\n\n".join(html_parts)).strip()
        return ""

    # 非 multipart
    payload = _safe_get_content(parsed_msg)
    if not isinstance(payload, str):
        return ""
    if parsed_msg.get_content_type() == "text/html":
        return _html_to_text(payload).strip()
    return payload.strip()


def _safe_get_content(part: Any) -> str | None:
    """安全获取 MIME part 的内容 — 自动处理编码问题。"""
    try:
        content = part.get_content()
    except Exception:
        payload_bytes = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        content = payload_bytes.decode(charset, errors="replace")

    if isinstance(content, str):
        return content

    # decode_header 可能返回 Header 对象
    try:
        return str(content)
    except Exception:
        return None


def _html_to_text(raw_html: str) -> str:
    """简单 HTML → 纯文本（无依赖）。"""
    text = re.sub(r"<\s*br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text)


def _reply_subject(base: str, prefix: str = "Re: ") -> str:
    """构造回复主题：已有 Re: 则保留，否则加前缀。"""
    subject = (base or "").strip() or "Re: your email"
    if subject.lower().startswith("re:"):
        return subject
    return f"{prefix}{subject}"
