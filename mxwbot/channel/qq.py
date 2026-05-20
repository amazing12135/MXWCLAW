"""QQ 频道适配器 — botpy SDK。

基于 QQ 官方 botpy SDK 的 WebSocket 连接，支持 C2C 私聊和群聊 @ 消息。
SDK 未安装或配置缺失时 ``_start()`` 抛 ``ChannelFatalError`` /
``ChannelAuthError`` 供上层处理。

协议: QQ Bot API v2 (WebSocket)
SDK: qq-botpy (botpy)
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from mxwbot.channel.base import (
    BaseChannel,
    ChannelAuthError,
    ChannelFatalError,
)
from mxwbot.utils.security import validate_url

logger = logging.getLogger("mxwbot.channel.qq")

# ---------------------------------------------------------------------------
# Graceful SDK import
# ---------------------------------------------------------------------------

try:
    import botpy
    from botpy.message import C2CMessage, GroupMessage
    QQ_AVAILABLE = True
except ImportError:  # pragma: no cover
    QQ_AVAILABLE = False
    botpy = None  # type: ignore
    C2CMessage = None  # type: ignore
    GroupMessage = None  # type: ignore

# Forward declaration for _QQBotClient (defined below if botpy is available)
_QQBotClient: type | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class QQConfig:
    """QQ 频道配置。

    Attributes:
        app_id: QQ Bot AppID (开发者 ID)。
        secret: QQ Bot Secret (机器人密钥)。
        msg_format: 消息格式 — ``"plain"`` 纯文本或 ``"markdown"`` Markdown。
    """

    app_id: str = ""
    secret: str = ""
    msg_format: str = "plain"  # "plain" | "markdown"

    # 消息去重窗口
    dedup_window: int = 1000


# ---------------------------------------------------------------------------
# Internal bot client — routes botpy events via callbacks
# ---------------------------------------------------------------------------

if QQ_AVAILABLE:

    class _QQBotClient(botpy.Client):  # type: ignore[name-defined]
        """Botpy client that routes events via callback functions.

        Unlike the previous closure-based approach, this class accepts
        callback functions at construction time, avoiding dynamic class
        creation on every ``_start()`` call.
        """

        def __init__(
            self,
            on_c2c,
            on_group,
            on_direct,
            *args,
            **kwargs,
        ) -> None:
            intents = botpy.Intents(  # type: ignore[attr-defined]
                public_messages=True, direct_message=True,
            )
            super().__init__(intents=intents, *args, **kwargs)
            self._on_c2c = on_c2c
            self._on_group = on_group
            self._on_direct = on_direct

        async def on_ready(self):
            logger.info("QQ bot ready: %s", self.robot.name)

        async def on_c2c_message_create(self, message: C2CMessage):
            await self._on_c2c(message)

        async def on_group_at_message_create(self, message: GroupMessage):
            await self._on_group(message)

        async def on_direct_message_create(self, message: C2CMessage):
            await self._on_direct(message)


# ---------------------------------------------------------------------------
# QQChannel
# ---------------------------------------------------------------------------

class QQChannel(BaseChannel):
    """QQ 频道 — botpy SDK 适配。

    使用方式::

        channel = QQChannel(bus, config=QQConfig(app_id="...", secret="..."))
        await channel.start()   # WebSocket 连接 + 自动重连
        # ...
        await channel.stop()
    """

    name = "qq"

    def __init__(self, bus, config: QQConfig | None = None) -> None:
        super().__init__(bus)
        self._cfg = config or QQConfig()
        self._client: Any = None
        self._http: Any = None  # httpx.AsyncClient，_start() 中创建
        self._reconnect_task: asyncio.Task | None = None

        # 消息去重
        self._processed_ids: deque[str] = deque(
            maxlen=self._cfg.dedup_window,
        )

        # 聊天类型缓存: chat_id → "c2c" | "group"
        self._chat_type_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        """启动 QQ Bot WebSocket 连接 + 自动重连循环。

        Raises:
            ChannelFatalError: SDK 未安装。
            ChannelAuthError: app_id 或 secret 未配置。
        """
        if not QQ_AVAILABLE:
            raise ChannelFatalError(
                "QQ SDK not installed. Run: pip install qq-botpy"
            )

        if not self._cfg.app_id or not self._cfg.secret:
            raise ChannelAuthError(
                "QQ app_id and secret not configured"
            )

        import httpx

        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=30),
            follow_redirects=False,
        )

        self._client = _QQBotClient(
            on_c2c=self._on_c2c_message,
            on_group=self._on_group_message,
            on_direct=self._on_c2c_message,
        )
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        logger.info(
            "QQ bot started (app_id=%s...) C2C & Group supported",
            self._cfg.app_id[:8],
        )

    async def _reconnect_loop(self) -> None:
        """自动重连循环 — 断线后 5 秒重试。"""
        while self._running:
            try:
                await self._client.start(
                    appid=self._cfg.app_id,
                    secret=self._cfg.secret,
                )
            except Exception:
                logger.warning("QQ bot disconnected, reconnecting in 5s...")
            if self._running:
                await asyncio.sleep(5)

    async def _stop(self) -> None:
        """停止 Bot 连接。"""
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

        logger.info("QQ bot stopped")

    # ------------------------------------------------------------------
    # Rich media hook
    # ------------------------------------------------------------------

    async def _on_before_send(self, msg) -> None:
        """发送文本前处理富媒体 — 遍历 msg.media 逐一上传发送。

        使用 botpy base64 upload API → msg_type=7 发送。
        上传失败时发送文本错误通知。
        """
        if not msg.media:
            return

        for media_ref in msg.media:
            ok = await self._send_rich_media(
                msg.chat_id, media_ref,
                is_group=self._chat_type_cache.get(msg.chat_id) == "group",
            )
            if not ok:
                await self._send_text(
                    msg.chat_id,
                    f"[发送失败: {media_ref}]",
                )

    async def _send_rich_media(
        self, chat_id: str, media_ref: str, *, is_group: bool = False,
    ) -> bool:
        """读取媒体文件 → base64 编码 → botpy 上传 → msg_type=7 发送。

        media_ref: 本地路径、file:// URI 或 http(s):// URL。
        """
        if self._client is None:
            logger.warning("QQ: client not initialized, cannot send media")
            return False

        media_ref = (media_ref or "").strip()
        if not media_ref:
            return False

        # 读取字节
        try:
            data, filename = await self._read_media_bytes(media_ref)
        except Exception:
            logger.warning("QQ: media read failed ref=%s", media_ref[:40])
            return False

        if not data or not filename:
            return False

        # 确定文件类型: 1=image, 4=file
        ext = os.path.splitext(filename)[1].lower()
        _image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
        file_type = 1 if ext in _image_exts else 4

        try:
            media_obj = await self._upload_media(
                chat_id, is_group, file_type, data, filename,
            )
            if not media_obj:
                logger.error("QQ: media upload returned empty")
                return False

            # 发送 media message (msg_type=7)
            payload: dict[str, Any] = {
                "msg_type": 7,
                "media": media_obj,
            }
            if is_group:
                await self._client.api.post_group_message(
                    group_openid=chat_id, **payload,
                )
            else:
                await self._client.api.post_c2c_message(
                    openid=chat_id, **payload,
                )
            logger.info("QQ: media sent: %s", filename)
            return True
        except Exception:
            logger.error("QQ: media send failed file=%s", filename[:20])
            return False

    async def _read_media_bytes(
        self, media_ref: str,
    ) -> tuple[bytes | None, str | None]:
        """读取媒体字节 — 支持本地文件、file:// URI、http(s):// URL。

        安全措施:
          - URL: 校验 scheme + 阻止内网地址 + 手动跟踪 redirect
          - 本地文件: resolve() 后检查在 allowed_dirs 内
        """
        media_ref = media_ref.strip()

        # 远程 URL
        if media_ref.startswith("http://") or media_ref.startswith("https://"):
            return await self._read_url_bytes(media_ref)

        # 本地文件
        return self._read_local_bytes(media_ref)

    async def _read_url_bytes(
        self, target_url: str,
    ) -> tuple[bytes | None, str | None]:
        """安全地读取远程 URL — SSRF 防护。"""
        if self._http is None:
            logger.warning("QQ: HTTP client not initialized")
            return None, None

        # 校验初始 URL
        try:
            current = validate_url(target_url)
        except ValueError as exc:
            logger.warning("QQ: URL rejected: %s", exc)
            return None, None

        visited: set[str] = set()
        for _hop in range(5):  # 最多 5 跳 redirect
            resp = await self._http.get(current, follow_redirects=False)
            if 300 <= resp.status_code < 400:
                redirect = resp.headers.get("Location", "")
                if not redirect:
                    logger.warning("QQ: redirect with no Location header")
                    return None, None
                current = urljoin(current, redirect)
                try:
                    current = validate_url(current)
                except ValueError as exc:
                    logger.warning("QQ: redirect target rejected: %s", exc)
                    return None, None
                if current in visited:
                    logger.warning("QQ: redirect loop detected")
                    return None, None
                visited.add(current)
                continue

            resp.raise_for_status()
            filename = os.path.basename(urlparse(current).path) or "file.bin"
            return resp.content, filename

        logger.warning("QQ: too many redirects for %s", target_url[:80])
        return None, None

    def _read_local_bytes(
        self, media_ref: str,
    ) -> tuple[bytes | None, str | None]:
        """安全地读取本地文件 — 路径遍历防护。"""
        # Parse the path from media_ref
        if media_ref.startswith("file://"):
            parsed = urlparse(media_ref)
            path_str = unquote(parsed.path or parsed.netloc)
        else:
            path_str = media_ref

        # Resolve and validate sandbox
        try:
            resolved = Path(path_str).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            logger.warning("QQ: path resolution failed: %s", exc)
            return None, None

        # Allowed directories (workspace + state_dir if configured)
        allowed_dirs: list[Path] = [Path.cwd()]
        state_dir = getattr(self._cfg, "state_dir", "")
        if state_dir:
            try:
                allowed_dirs.append(Path(state_dir).expanduser().resolve())
            except Exception:
                pass

        # Verify path stays within allowed directories
        safe = any(
            str(resolved) == str(d) or str(resolved).startswith(str(d) + os.sep)
            for d in allowed_dirs
        )
        if not safe:
            logger.warning(
                "QQ: path traversal blocked: %s (resolved: %s)",
                media_ref[:60], str(resolved)[:100],
            )
            return None, None

        if not resolved.is_file():
            logger.warning("QQ: media file not found: %s", str(resolved)[:80])
            return None, None

        data = resolved.read_bytes()
        return data, resolved.name

    async def _upload_media(
        self, chat_id: str, is_group: bool,
        file_type: int, data: bytes, filename: str,
    ) -> Any:
        """上传媒体到 QQ CDN — base64 编码后调用 botpy upload API。"""
        import base64 as _base64

        if self._client is None:
            return None

        b64 = _base64.b64encode(data).decode()

        if is_group:
            endpoint = "/v2/groups/{group_openid}/files"
            id_key = "group_openid"
        else:
            endpoint = "/v2/users/{openid}/files"
            id_key = "openid"

        try:
            from botpy.http import Route
            route = Route("POST", endpoint, **{id_key: chat_id})
            return await self._client.api._http.request(route, json={
                id_key: chat_id,
                "file_type": file_type,
                "file_data": b64,
                "file_name": filename,
                "srv_send_msg": False,
            })
        except Exception:
            logger.warning("QQ: media upload failed")
            return None

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def _send_text(self, chat_id: str, text: str) -> None:
        """发送纯文本 / Markdown 消息。

        根据 _chat_type_cache 自动选择 C2C 或 Group API。
        """
        if self._client is None:
            logger.warning("QQ client not initialized, cannot send")
            return

        chat_type = self._chat_type_cache.get(chat_id, "c2c")
        is_group = chat_type == "group"
        use_markdown = self._cfg.msg_format == "markdown"

        payload: dict[str, Any] = {"content": text}
        if use_markdown:
            payload["msg_type"] = 2  # Markdown
            payload["markdown"] = {"content": text}
        else:
            payload["msg_type"] = 0  # Plain text

        try:
            if is_group:
                await self._client.api.post_group_message(
                    group_openid=chat_id, **payload,
                )
            else:
                await self._client.api.post_c2c_message(
                    openid=chat_id, **payload,
                )
        except Exception:
            logger.error("QQ send_text failed")

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def supports_interactive_buttons(self) -> bool:
        return False

    def is_connected(self) -> bool:
        """平台连接状态 — 运行中且 bot client 已创建。"""
        return self._running and self._client is not None

    # ------------------------------------------------------------------
    # Internal: botpy event handlers
    # ------------------------------------------------------------------

    async def _on_c2c_message(self, message: Any) -> None:
        """处理 C2C 私聊消息 → 去重 → 投递 Bus。"""
        msg_id = getattr(message, "id", "")
        if msg_id in self._processed_ids:
            return
        self._processed_ids.append(msg_id)

        # botpy C2C message: author.user_openid
        author = getattr(message, "author", None)
        user_id = getattr(author, "user_openid", "") if author else ""
        chat_id = user_id
        self._chat_type_cache[chat_id] = "c2c"

        content = (getattr(message, "content", "") or "").strip()
        if not content:
            return

        await self.bus.publish_inbound(
            self._make_inbound(chat_id, content, msg_id)
        )

    async def _on_group_message(self, message: Any) -> None:
        """处理群聊 @ 消息 → 去重 → 投递 Bus。"""
        msg_id = getattr(message, "id", "")
        if msg_id in self._processed_ids:
            return
        self._processed_ids.append(msg_id)

        # botpy Group message: group_openid, author.member_openid
        chat_id = getattr(message, "group_openid", "") or ""
        author = getattr(message, "author", None)
        self._chat_type_cache[chat_id] = "group"

        content = (getattr(message, "content", "") or "").strip()
        if not content:
            return

        await self.bus.publish_inbound(
            self._make_inbound(chat_id, content, msg_id)
        )

    def _make_inbound(self, chat_id: str, content: str, msg_id: str):
        """构造标准 InboundMessage。"""
        from mxwbot.bus.messages import InboundMessage

        return InboundMessage(
            msg_type="message",
            channel=self.name,
            chat_id=chat_id,
            content=content,
            idempotency_key=f"{self.name}:{chat_id}:{msg_id}",
        )
