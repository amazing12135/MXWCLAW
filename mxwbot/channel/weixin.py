"""微信频道适配器 — ilink HTTP 长轮询（最小文本通道）。

基于微信个人号 ilinkai.weixin.qq.com API 的 HTTP 长轮询实现。
仅支持文本消息收发，媒体（图片/语音/视频/文件）静默跳过。

认证方式:
  - token 预配置: WeixinConfig.token 或 {state_dir}/account.json
  - QR 码登录不在本次实现范围内（通过外部工具获取 token）

协议参考: @tencent-weixin/openclaw-weixin v1.0.3
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from mxwbot.channel.base import (
    BaseChannel,
    ChannelAuthError,
    ChannelFatalError,
)
from mxwbot.bus.messages import InboundMessage
from mxwbot.utils.security import decrypt_token, encrypt_token

logger = logging.getLogger("mxwbot.channel.weixin")

# ---------------------------------------------------------------------------
# Protocol constants (from openclaw-weixin types.ts)
# ---------------------------------------------------------------------------

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2

WEIXIN_CHANNEL_VERSION = "2.1.1"
ILINK_APP_ID = "bot"


def _encode_version(version: str) -> int:
    """Encode semver as 0x00MMNNPP (major/minor/patch in one uint32)."""
    parts = version.split(".")
    major = int(parts[0]) if len(parts) > 0 else 0
    minor = int(parts[1]) if len(parts) > 1 else 0
    patch = int(parts[2]) if len(parts) > 2 else 0
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


ILINK_APP_CLIENT_VERSION = _encode_version(WEIXIN_CHANNEL_VERSION)
BASE_INFO: dict[str, str] = {"channel_version": WEIXIN_CHANNEL_VERSION}

# Long-poll defaults
DEFAULT_POLL_TIMEOUT_S = 35
MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY_S = 30
RETRY_DELAY_S = 2


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class WeixinConfig:
    """微信频道配置。

    Attributes:
        token: bot token（从 QR 扫码登录获得）。
        base_url: ilink API 基础 URL。
        poll_timeout: 长轮询超时秒数。
        state_dir: token 持久化目录（默认为 workspace/weixin/）。
    """

    token: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    poll_timeout: int = DEFAULT_POLL_TIMEOUT_S
    state_dir: str = ""


# ---------------------------------------------------------------------------
# WeChatChannel
# ---------------------------------------------------------------------------

class WeChatChannel(BaseChannel):
    """微信频道 — ilink HTTP 长轮询（最小文本通道）。

    使用方式::

        channel = WeChatChannel(bus, config=WeixinConfig(token="..."))
        await channel.start()   # 启动长轮询
        await channel.send(msg)
        await channel.stop()
    """

    name = "wechat"

    def __init__(self, bus, config: WeixinConfig | None = None) -> None:
        super().__init__(bus)
        self._cfg = config or WeixinConfig()
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._token: str = ""
        self._get_updates_buf: str = ""

        # 消息去重
        self._processed_ids: OrderedDict[str, None] = OrderedDict()

        # 上下文 token 缓存: from_user_id → context_token
        self._context_tokens: dict[str, str] = {}

        # 服务器建议的轮询超时
        self._next_poll_timeout_s: int = self._cfg.poll_timeout

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _get_state_dir(self) -> Path:
        """获取状态持久化目录（自动创建）。"""
        if self._cfg.state_dir:
            d = Path(self._cfg.state_dir).expanduser()
        else:
            d = Path("weixin_state")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_state(self) -> bool:
        """从 account.json 加载 token 和上下文 token。

        优先读取 ``token_encrypted`` 并解密；fallback 读取明文 ``token``
        （兼容旧格式，读取后由下一次 ``_save_state`` 自动迁移到加密）。
        """
        state_file = self._get_state_dir() / "account.json"
        if not state_file.exists():
            return False
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            # 优先读加密 token
            encrypted = data.get("token_encrypted", "")
            if encrypted:
                self._token = decrypt_token(encrypted)
            else:
                self._token = data.get("token", "")
            self._get_updates_buf = data.get("get_updates_buf", "")
            ctx = data.get("context_tokens", {})
            if isinstance(ctx, dict):
                self._context_tokens = {
                    str(k): str(v) for k, v in ctx.items() if k and v
                }
            base = data.get("base_url", "")
            if base:
                self._cfg.base_url = base
            return bool(self._token)
        except Exception:
            logger.warning("Failed to load WeChat state file")
            return False

    def _save_state(self) -> None:
        """持久化当前 token 和上下文 token。

        Token 使用 AES-GCM 加密后以 ``token_encrypted`` 字段存储，
        不再写入明文 ``token`` 字段。
        """
        state_file = self._get_state_dir() / "account.json"
        try:
            data = {
                "token_encrypted": encrypt_token(self._token) if self._token else "",
                "get_updates_buf": self._get_updates_buf,
                "context_tokens": self._context_tokens,
                "base_url": self._cfg.base_url,
            }
            state_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("Failed to save WeChat state")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _random_wechat_uin() -> str:
        """生成随机 X-WECHAT-UIN（每次请求新值）。"""
        uint32 = int.from_bytes(os.urandom(4), "big")
        return base64.b64encode(str(uint32).encode()).decode()

    def _make_headers(self, *, auth: bool = True) -> dict[str, str]:
        """构造请求头 — 每次调用生成新的 X-WECHAT-UIN。"""
        headers: dict[str, str] = {
            "X-WECHAT-UIN": self._random_wechat_uin(),
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _api_post(self, endpoint: str, body: dict) -> dict:
        """POST 请求到 ilink API。"""
        assert self._client is not None
        payload = {**body, "base_info": BASE_INFO}
        url = f"{self._cfg.base_url}/{endpoint}"
        resp = await self._client.post(
            url, json=payload, headers=self._make_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # QR code login
    # ------------------------------------------------------------------

    async def login(self, *, force: bool = False) -> bool:
        """Interactive QR-code login for WeChat ilink bot.

        If a valid token already exists and *force* is False, returns
        immediately.  Otherwise the method contacts the ilink API,
        displays the QR code URL in the terminal, and polls until the
        user scans it with WeChat (max 120 s).

        On success the token is persisted via ``_save_state()``.
        """
        if not force and self._token:
            logger.info("WeChat: already logged in (use force=True to re-login)")
            return True

        console = None
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            pass

        # 1. Create temporary client (no auth token yet)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=True,
        ) as client:
            headers = self._make_headers(auth=False)
            base = self._cfg.base_url

            # 2. Request QR code
            login_resp = await client.post(
                f"{base}/ilink/bot/login",
                json={"base_info": BASE_INFO},
                headers=headers,
            )
            try:
                login_data = login_resp.json()
            except Exception:
                # API may return HTML/empty on unknown endpoints
                status = login_resp.status_code
                body = login_resp.text[:500]
                logger.warning("Login API returned non-JSON (status=%d): %s", status, body)
                if console:
                    console.print(f"[red]Login API returned HTTP {status}[/red]")
                    console.print(f"[dim]{body}[/dim]")
                    console.print()
                    console.print("[yellow]QR login is not currently supported by this ilink API.[/yellow]")
                    console.print("[yellow]Get a token manually and place it in:[/yellow]")
                    console.print(f"[cyan]{self._get_state_dir() / 'account.json'}[/cyan]")
                    console.print("[dim]Format: {\"token\": \"your-token-here\"}[/dim]")
                return False
            logger.debug("Login response: %s", login_data)

            qr_uuid = (
                login_data.get("uuid")
                or login_data.get("qrcode_uuid")
                or login_data.get("data", {}).get("uuid")
            )
            qr_url = (
                login_data.get("qr_url")
                or login_data.get("url")
                or f"https://ilinkai.weixin.qq.com/qr/{qr_uuid}"
            )

            if not qr_uuid:
                if console:
                    console.print(f"[red]Login API returned unexpected data:[/red]")
                    console.print(login_data)
                logger.error("WeChat login: no UUID in response: %s", login_data)
                return False

            # 3. Display QR URL (open in browser to scan)
            if console:
                console.print()
                console.print("[bold cyan]WeChat Login[/bold cyan]")
                console.print()
                console.print(f"[bold]QR URL:[/bold] [cyan underline]{qr_url}[/cyan underline]")
                console.print()
                console.print("[dim]Copy this URL to your browser, then scan the QR code with WeChat[/dim]")
                console.print("[dim]Waiting for you to scan… (timeout: 120s)[/dim]")
                console.print()
            else:
                print(f"\nWeChat Login\nQR URL: {qr_url}\n")

            # 4. Poll for scan confirmation (3s interval, 120s timeout)
            for attempt in range(40):
                await asyncio.sleep(3)
                try:
                    poll_resp = await client.post(
                        f"{base}/ilink/bot/checklogin",
                        json={"base_info": BASE_INFO, "uuid": qr_uuid},
                        headers=headers,
                    )
                    poll_data = poll_resp.json()

                    # Try common token field names
                    token = (
                        poll_data.get("token")
                        or poll_data.get("data", {}).get("token")
                    )
                    if token:
                        self._token = token
                        self._save_state()
                        if console:
                            console.print("\n[bold green]✓ Login successful![/bold green]")
                        logger.info("WeChat login: token saved")
                        return True

                    status = (
                        poll_data.get("status")
                        or poll_data.get("data", {}).get("status")
                        or poll_data.get("ret")
                    )
                    if status in ("scanned", "confirmed", 1):
                        if console and attempt % 3 == 0:
                            console.print("[yellow]Scanned! Confirm login on your phone…[/yellow]")
                    elif status == "expired":
                        if console:
                            console.print("[red]QR code expired. Run login again.[/red]")
                        return False
                except Exception as exc:
                    logger.debug("Poll error (attempt %d): %s", attempt + 1, exc)

            if console:
                console.print("[red]Login timed out (120s).[/red]")
            return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        """加载 token → 创建 httpx client → 启动长轮询循环。

        Raises:
            ChannelFatalError: token 完全未配置。
            ChannelAuthError: token 为空字符串。
        """
        # 加载 token
        if self._cfg.token:
            self._token = self._cfg.token
        else:
            state_file = self._get_state_dir() / "account.json"
            if not state_file.exists():
                raise ChannelFatalError(
                    "WeChat token not configured. "
                    "Set WeixinConfig.token or place account.json in state_dir."
                )
            self._load_state()

        if not self._token:
            raise ChannelAuthError("WeChat token is empty, cannot start")

        self._next_poll_timeout_s = self._cfg.poll_timeout
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._next_poll_timeout_s + 10, connect=30),
            follow_redirects=True,
        )

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("WeChat channel started (text-only, long-poll)")

    async def _poll_loop(self) -> None:
        """长轮询主循环 — 带连续失败退避。"""
        consecutive_failures = 0

        while self._running:
            try:
                await self._poll_once()
                consecutive_failures = 0
            except httpx.TimeoutException:
                # 长轮询超时是正常的，立即重试
                continue
            except Exception:
                if not self._running:
                    break
                consecutive_failures += 1
                delay = (
                    BACKOFF_DELAY_S
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                    else RETRY_DELAY_S
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0
                logger.warning(
                    "WeChat poll error, retrying in %ds (failures=%d)",
                    delay, consecutive_failures,
                )
                await asyncio.sleep(delay)

    async def _poll_once(self) -> None:
        """执行一次 getupdates 长轮询。"""
        assert self._client is not None

        # 调整 httpx timeout 为当前轮询超时
        self._client.timeout = httpx.Timeout(
            self._next_poll_timeout_s + 10, connect=30,
        )

        body: dict[str, Any] = {"get_updates_buf": self._get_updates_buf}
        data = await self._api_post("ilink/bot/getupdates", body)

        # 检查 API 错误
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise RuntimeError(
                f"getUpdates failed: ret={ret} errcode={errcode} "
                f"errmsg={data.get('errmsg', '')}"
            )

        # 采纳服务器建议的轮询超时
        server_timeout_ms = data.get("longpolling_timeout_ms")
        if server_timeout_ms and server_timeout_ms > 0:
            self._next_poll_timeout_s = max(server_timeout_ms // 1000, 5)

        # 更新游标
        new_buf = data.get("get_updates_buf", "")
        if new_buf:
            self._get_updates_buf = new_buf

        # 处理消息（仅更新内存中的 context_tokens）
        msgs: list[dict] = data.get("msgs") or []
        for msg in msgs:
            try:
                await self._process_message(msg)
            except Exception:
                logger.warning("Error processing WeChat message")

        # 统一落盘：游标 + context_tokens 一次写入
        if new_buf or msgs:
            self._save_state()

    async def _stop(self) -> None:
        """停止轮询 + 关闭 HTTP client + 持久化状态。"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None

        if self._client is not None:
            await self._client.aclose()
            self._client = None

        self._save_state()
        logger.info("WeChat channel stopped")

    # ------------------------------------------------------------------
    # Inbound: parse & process
    # ------------------------------------------------------------------

    async def _process_message(self, msg: dict) -> None:
        """处理单条 WeixinMessage — 去重 → 提取文本 → 缓存 token → 投递 Bus。

        非文本 item（图片/语音/文件/视频）静默跳过。
        """
        # 跳过 bot 自己的消息
        if msg.get("message_type") == MESSAGE_TYPE_BOT:
            return

        # 去重
        msg_id = str(
            msg.get("message_id", "")
            or msg.get("seq", "")
            or f"{msg.get('from_user_id', '')}_{msg.get('create_time_ms', '')}"
        )
        if not msg_id or msg_id in self._processed_ids:
            return
        self._processed_ids[msg_id] = None
        while len(self._processed_ids) > 1000:
            self._processed_ids.popitem(last=False)

        # 发送者
        from_user_id = str(msg.get("from_user_id", "") or "")
        if not from_user_id:
            return

        # 缓存 context_token（回复必需）
        ctx_token = msg.get("context_token", "")
        if ctx_token:
            self._context_tokens[from_user_id] = ctx_token

        # 解析 item_list — 仅提取文本
        item_list: list[dict] = msg.get("item_list") or []
        content_parts: list[str] = []

        for item in item_list:
            item_type = item.get("type", 0)

            if item_type == ITEM_TEXT:
                text_item = item.get("text_item") or {}
                text = (text_item.get("text", "") or "").strip()
                if text:
                    content_parts.append(text)

            elif item_type == ITEM_IMAGE:
                content_parts.append("[图片]")

            elif item_type == ITEM_VOICE:
                voice_item = item.get("voice_item") or {}
                voice_text = (voice_item.get("text", "") or "").strip()
                if voice_text:
                    content_parts.append(f"[语音] {voice_text}")
                else:
                    content_parts.append("[语音]")

            elif item_type == ITEM_FILE:
                file_item = item.get("file_item") or {}
                file_name = file_item.get("file_name", "文件")
                content_parts.append(f"[文件: {file_name}]")

            elif item_type == ITEM_VIDEO:
                content_parts.append("[视频]")

        content = "\n".join(content_parts).strip()
        if not content:
            return

        await self.bus.publish_inbound(
            InboundMessage(
                msg_type="message",
                channel=self.name,
                chat_id=from_user_id,
                content=content,
                idempotency_key=f"{self.name}:{from_user_id}:{msg_id}",
            )
        )

    # ------------------------------------------------------------------
    # Outbound: rich media hook
    # ------------------------------------------------------------------

    async def _on_before_send(self, msg) -> None:
        """发送文本前处理富媒体 — 遍历 msg.media 逐一 AES 加密上传。

        使用 ilink CDN upload API → sendmessage media item 发送。
        上传失败时发送文本错误通知。
        """
        if not msg.media:
            return

        for media_ref in msg.media:
            ctx_token = self._context_tokens.get(msg.chat_id, "")
            if not ctx_token:
                logger.warning(
                    "WeChat: no context_token for %s, cannot send media",
                    msg.chat_id,
                )
                await self._send_text(
                    msg.chat_id,
                    f"[发送失败: 无 context_token — {media_ref}]",
                )
                continue

            ok = await self._send_media_file(
                msg.chat_id, media_ref, ctx_token,
            )
            if not ok:
                await self._send_text(
                    msg.chat_id,
                    f"[发送失败: {media_ref}]",
                )

    async def _send_media_file(
        self, chat_id: str, media_ref: str, ctx_token: str,
    ) -> bool:
        """读取媒体 → AES-128-ECB 加密 → CDN 上传 → sendmessage。

        WeChat ilink 协议: getuploadurl → AES encrypt → CDN upload →
        x-encrypted-param → sendmessage with media item。

        SDK 未安装或 token 不可用时返回 False。
        """
        # 当前为骨架：需要 httpx + pycryptodome + qq-botpy 等依赖
        # 完整实现在后续 Phase 完成
        logger.warning(
            "WeChat media upload not yet implemented (ref=%s)", media_ref,
        )
        return False

    # ------------------------------------------------------------------
    # Outbound: send
    # ------------------------------------------------------------------

    async def _send_text(self, chat_id: str, text: str) -> None:
        """发送纯文本消息 via POST /ilink/bot/sendmessage。"""
        if self._client is None or not self._token:
            logger.warning("WeChat: not initialized, cannot send")
            return

        ctx_token = self._context_tokens.get(chat_id, "")
        if not ctx_token:
            logger.warning(
                "WeChat: no context_token for chat_id=%r, cannot send "
                "(wait for user to send a message first)", chat_id,
            )
            return

        client_id = f"mxwbot-{uuid.uuid4().hex[:12]}"
        item_list: list[dict] = [
            {"type": ITEM_TEXT, "text_item": {"text": text}},
        ]

        weixin_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": chat_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": 2,  # MESSAGE_STATE_FINISH
            "item_list": item_list,
            "context_token": ctx_token,
        }

        try:
            data = await self._api_post(
                "ilink/bot/sendmessage", {"msg": weixin_msg},
            )
            errcode = data.get("errcode", 0)
            if errcode and errcode != 0:
                logger.warning(
                    "WeChat send error (code=%s): %s",
                    errcode, data.get("errmsg", ""),
                )
        except Exception:
            logger.error(
                "WeChat send_text failed chat_id=%s...", chat_id[:4],
            )

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def supports_interactive_buttons(self) -> bool:
        return False

    def is_connected(self) -> bool:
        """平台连接状态 — 运行中且 client 与 token 均可用。"""
        return self._running and self._client is not None and bool(self._token)
