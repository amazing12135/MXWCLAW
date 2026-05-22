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

    async def _api_get(
        self, endpoint: str, params: dict | None = None, *, auth: bool = True,
    ) -> dict:
        """GET 请求到 ilink API。"""
        assert self._client is not None
        url = f"{self._cfg.base_url}/{endpoint}"
        resp = await self._client.get(
            url, params=params, headers=self._make_headers(auth=auth),
        )
        resp.raise_for_status()
        return resp.json()

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
    # QR code login  (matches nanobot login-qr.ts protocol)
    # ------------------------------------------------------------------

    async def _fetch_qr_code(self) -> tuple[str, str]:
        """获取 QR 码。返回 (qrcode_id, qrcode_img_content)。"""
        data = await self._api_get(
            "ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            auth=False,
        )
        qrcode_img = data.get("qrcode_img_content", "")
        qrcode_id = data.get("qrcode", "")
        if not qrcode_id:
            raise RuntimeError(f"Failed to get QR code from WeChat API: {data}")
        return qrcode_id, (qrcode_img or qrcode_id)

    @staticmethod
    def _print_qr_code(url: str) -> None:
        """终端显示 QR 码（qrcode 库可用时打印 ASCII，否则打印 URL）。"""
        try:
            import qrcode as qr_lib
            qr = qr_lib.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            print(f"\nQR Login URL: {url}\n")

    async def _qr_login(self) -> bool:
        """执行 QR 码登录轮询。匹配 nanobot _qr_login 逻辑。"""
        refresh_count = 0

        qrcode_id, scan_url = await self._fetch_qr_code()
        self._print_qr_code(scan_url)

        while True:
            try:
                data = await self._api_get(
                    "ilink/bot/get_qrcode_status",
                    params={"qrcode": qrcode_id},
                    auth=False,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                await asyncio.sleep(1)
                continue
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    await asyncio.sleep(1)
                    continue
                raise

            status = data.get("status", "")

            if status == "confirmed":
                token = data.get("bot_token", "")
                base_url = data.get("baseurl", "")
                bot_id = data.get("ilink_bot_id", "")
                if token:
                    self._token = token
                    if base_url:
                        self._cfg.base_url = base_url
                    self._save_state()
                    logger.info(
                        "WeChat login success: bot_id=%s", bot_id,
                    )
                    return True
                logger.error("Login confirmed but no bot_token in response")
                return False

            elif status == "scaned_but_redirect":
                redirect_host = str(data.get("redirect_host", "") or "").strip()
                if redirect_host:
                    if not (redirect_host.startswith("http://") or redirect_host.startswith("https://")):
                        redirect_host = f"https://{redirect_host}"
                    # Switch to redirected host for subsequent polling
                    self._cfg.base_url = redirect_host
                    logger.info("WeChat QR poll redirected to %s", redirect_host)

            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    logger.warning("QR code expired too many times, giving up")
                    return False
                qrcode_id, scan_url = await self._fetch_qr_code()
                self._print_qr_code(scan_url)

            # "wait" — keep polling
            await asyncio.sleep(1)

    async def login(self, *, force: bool = False) -> bool:
        """交互式二维码扫码登录。

        获取 QR 码 → 终端显示 → 轮询等待微信扫码确认 → 自动保存 token。
        成功返回 True。
        """
        if not force and (self._token or self._load_state()):
            logger.info("WeChat: already logged in (use force=True to re-login)")
            return True

        if force:
            self._token = ""
            state_file = self._get_state_dir() / "account.json"
            if state_file.exists():
                state_file.unlink()

        console = None
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            pass

        if console:
            console.print("\n[bold cyan]WeChat QR Login[/bold cyan]")
            console.print("[dim]Fetching QR code from ilink API…[/dim]\n")

        # 复用 wx 的 client 和 headers（login 阶段不需要 auth token）
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=30),
            follow_redirects=True,
        )
        try:
            ok = await self._qr_login()
            if ok and console:
                console.print("\n[bold green]✓ Login successful![/bold green]")
            elif not ok and console:
                console.print("\n[red]Login failed or timed out.[/red]")
            return ok
        finally:
            if self._client:
                await self._client.aclose()
                self._client = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        """加载 token → 创建 httpx client → 启动长轮询循环。

        无 token 时自动触发二维码扫码登录。
        """
        # 加载 token
        if self._cfg.token:
            self._token = self._cfg.token
        else:
            self._load_state()

        # 无 token 时尝试扫码登录
        if not self._token:
            logger.info("WeChat: no token found, starting QR login…")
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=30),
                follow_redirects=True,
            )
            try:
                ok = await self._qr_login()
                if not ok:
                    raise ChannelAuthError(
                        "WeChat QR login failed. "
                        "Run 'mxwbot channel login wechat' to retry."
                    )
            finally:
                if self._client:
                    await self._client.aclose()
                    self._client = None

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
