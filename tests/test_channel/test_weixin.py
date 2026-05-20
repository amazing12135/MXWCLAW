"""测试 WeChatChannel — 方案 A 最小文本通道。

由于微信 ilink API 需要真实 token 才能连接，测试侧重：
- Token 加载/持久化
- _parse() 消息解析
- _process_message() 文本提取 + 去重
- _send_text() context_token 缓存
- 媒体 item 静默跳过
- 轮询错误退避
"""

import asyncio
import json
from pathlib import Path

import pytest

from mxwbot.channel.base import ChannelAuthError, ChannelFatalError
from mxwbot.channel.weixin import (
    WeChatChannel,
    WeixinConfig,
    ITEM_TEXT,
    ITEM_IMAGE,
    ITEM_VOICE,
    ITEM_FILE,
    ITEM_VIDEO,
    MESSAGE_TYPE_USER,
    MESSAGE_TYPE_BOT,
)
from mxwbot.bus.queue import MessageBus
from mxwbot.utils.security import decrypt_token, encrypt_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_channel(bus=None, *, token="test-token", state_dir="", **kw):
    if bus is None:
        bus = MessageBus()
    cfg = WeixinConfig(token=token, state_dir=state_dir, **kw)
    return WeChatChannel(bus, config=cfg)


def _make_text_item(text: str) -> dict:
    return {"type": ITEM_TEXT, "text_item": {"text": text}}


def _make_msg(*, msg_id="m1", from_user="u1", ctx_token="ct1", items=None):
    return {
        "message_id": msg_id,
        "from_user_id": from_user,
        "context_token": ctx_token,
        "message_type": MESSAGE_TYPE_USER,
        "item_list": items or [_make_text_item("hello")],
    }


# ---------------------------------------------------------------------------
# Token / state persistence
# ---------------------------------------------------------------------------

class TestWeixinToken:
    def test_load_token_from_config(self):
        ch = _make_channel(token="cfg-token")
        assert ch._cfg.token == "cfg-token"

    def test_load_state_from_file(self, tmp_path: Path):
        state_dir = tmp_path / "weixin_state"
        state_dir.mkdir()
        account = state_dir / "account.json"
        account.write_text(json.dumps({"token": "saved-token"}))

        ch = _make_channel(token="", state_dir=str(state_dir))
        ok = ch._load_state()
        assert ok is True
        assert ch._token == "saved-token"

    def test_save_and_reload_state(self, tmp_path: Path):
        state_dir = tmp_path / "weixin_state"
        state_dir.mkdir()

        ch = _make_channel(token="my-token", state_dir=str(state_dir))
        ch._token = "my-token"
        ch._context_tokens["u1"] = "ct-abc"
        ch._save_state()

        # 重新加载
        ch2 = _make_channel(token="", state_dir=str(state_dir))
        ok = ch2._load_state()
        assert ok is True
        assert ch2._token == "my-token"
        assert ch2._context_tokens.get("u1") == "ct-abc"

    def test_load_state_file_missing(self):
        ch = _make_channel(token="", state_dir="/nonexistent/path")
        ok = ch._load_state()
        assert ok is False


# ---------------------------------------------------------------------------
# _parse()
# ---------------------------------------------------------------------------

class TestWeixinParse:
    def test_parse_dict(self):
        ch = _make_channel()
        result = ch._parse({
            "chat_id": "user123",
            "content": "hello",
            "message_id": "m1",
        })
        assert result is not None
        assert result["chat_id"] == "user123"
        assert result["content"] == "hello"

    def test_parse_empty_returns_none(self):
        ch = _make_channel()
        assert ch._parse({"chat_id": "", "content": "x"}) is None
        assert ch._parse({"chat_id": "x", "content": ""}) is None
        assert ch._parse(None) is None
        assert ch._parse("raw") is None


# ---------------------------------------------------------------------------
# _process_message() — 文本提取
# ---------------------------------------------------------------------------

class TestWeixinProcessMessage:
    @pytest.mark.asyncio
    async def test_text_message_published(self):
        bus = MessageBus()
        ch = _make_channel(bus)
        msg = _make_msg(msg_id="m1", from_user="u1", items=[_make_text_item("你好世界")])

        await ch._process_message(msg)

        inbound = await bus.input_queue.get()
        assert inbound.channel == "wechat"
        assert inbound.chat_id == "u1"
        assert inbound.content == "你好世界"

    @pytest.mark.asyncio
    async def test_multi_item_text_joined(self):
        bus = MessageBus()
        ch = _make_channel(bus)
        msg = _make_msg(msg_id="m1", items=[
            _make_text_item("第一段"),
            _make_text_item("第二段"),
        ])

        await ch._process_message(msg)
        inbound = await bus.input_queue.get()
        assert "第一段" in inbound.content
        assert "第二段" in inbound.content

    @pytest.mark.asyncio
    async def test_image_item_placeholder(self):
        """图片 → 占位文本 [图片]"""
        bus = MessageBus()
        ch = _make_channel(bus)
        msg = _make_msg(msg_id="m1", items=[
            {"type": ITEM_IMAGE, "image_item": {}},
        ])

        await ch._process_message(msg)
        inbound = await bus.input_queue.get()
        assert "[图片]" in inbound.content

    @pytest.mark.asyncio
    async def test_voice_item_with_text(self):
        """语音带转文字 → [语音] 转写文本"""
        bus = MessageBus()
        ch = _make_channel(bus)
        msg = _make_msg(msg_id="m1", items=[{
            "type": ITEM_VOICE,
            "voice_item": {"text": "明天见"},
        }])

        await ch._process_message(msg)
        inbound = await bus.input_queue.get()
        assert "[语音]" in inbound.content
        assert "明天见" in inbound.content

    @pytest.mark.asyncio
    async def test_voice_item_without_text(self):
        """语音无转写 → [语音] 占位"""
        bus = MessageBus()
        ch = _make_channel(bus)
        msg = _make_msg(msg_id="m1", items=[{
            "type": ITEM_VOICE, "voice_item": {},
        }])

        await ch._process_message(msg)
        inbound = await bus.input_queue.get()
        assert inbound.content == "[语音]"

    @pytest.mark.asyncio
    async def test_file_item_placeholder(self):
        """文件 → [文件: name]"""
        bus = MessageBus()
        ch = _make_channel(bus)
        msg = _make_msg(msg_id="m1", items=[{
            "type": ITEM_FILE,
            "file_item": {"file_name": "report.pdf"},
        }])

        await ch._process_message(msg)
        inbound = await bus.input_queue.get()
        assert "[文件: report.pdf]" in inbound.content

    @pytest.mark.asyncio
    async def test_video_item_placeholder(self):
        """视频 → [视频]"""
        bus = MessageBus()
        ch = _make_channel(bus)
        msg = _make_msg(msg_id="m1", items=[{
            "type": ITEM_VIDEO, "video_item": {},
        }])

        await ch._process_message(msg)
        inbound = await bus.input_queue.get()
        assert inbound.content == "[视频]"


# ---------------------------------------------------------------------------
# _process_message() — 去重 & 过滤
# ---------------------------------------------------------------------------

class TestWeixinMessageFilter:
    @pytest.mark.asyncio
    async def test_bot_message_skipped(self):
        """message_type=BOT 的消息被跳过。"""
        bus = MessageBus()
        ch = _make_channel(bus)
        msg = _make_msg(msg_id="m1")
        msg["message_type"] = MESSAGE_TYPE_BOT

        await ch._process_message(msg)
        assert bus.input_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_duplicate_skipped(self):
        """相同 msg_id 第二次被丢弃。"""
        bus = MessageBus()
        ch = _make_channel(bus)

        await ch._process_message(_make_msg(msg_id="m1"))
        assert bus.input_queue.qsize() == 1

        await ch._process_message(_make_msg(msg_id="m1"))
        assert bus.input_queue.qsize() == 1  # 未新增

    @pytest.mark.asyncio
    async def test_no_from_user_skipped(self):
        """无 from_user_id 的消息被丢弃。"""
        bus = MessageBus()
        ch = _make_channel(bus)
        msg = _make_msg(from_user="")

        await ch._process_message(msg)
        assert bus.input_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_empty_content_skipped(self):
        """item_list 为空或只有空白 → 不投递。"""
        bus = MessageBus()
        ch = _make_channel(bus)

        # 空 item_list 导致 _make_msg 用默认值，直接构造空列表消息
        msg = {
            "message_id": "m1",
            "from_user_id": "u1",
            "context_token": "ct1",
            "message_type": MESSAGE_TYPE_USER,
            "item_list": [],
        }
        await ch._process_message(msg)
        assert bus.input_queue.qsize() == 0

        # 纯空白文本
        msg2 = {
            "message_id": "m2",
            "from_user_id": "u1",
            "context_token": "ct1",
            "message_type": MESSAGE_TYPE_USER,
            "item_list": [_make_text_item("   \n  ")],
        }
        await ch._process_message(msg2)
        assert bus.input_queue.qsize() == 0


# ---------------------------------------------------------------------------
# Context token 缓存
# ---------------------------------------------------------------------------

class TestWeixinContextToken:
    @pytest.mark.asyncio
    async def test_context_token_cached(self):
        """收到消息后 context_token 被缓存。"""
        ch = _make_channel()
        msg = _make_msg(from_user="u1", ctx_token="ct-xyz")

        await ch._process_message(msg)
        assert ch._context_tokens.get("u1") == "ct-xyz"

    @pytest.mark.asyncio
    async def test_context_token_not_overwritten_by_empty(self):
        """空 context_token 不覆盖已有缓存。"""
        ch = _make_channel()
        ch._context_tokens["u1"] = "ct-original"

        msg = _make_msg(from_user="u1", ctx_token="")
        await ch._process_message(msg)

        assert ch._context_tokens.get("u1") == "ct-original"


# ---------------------------------------------------------------------------
# _send_text()
# ---------------------------------------------------------------------------

class TestWeixinSendText:
    @pytest.mark.asyncio
    async def test_send_without_token_warns(self):
        """无 token 时 _send_text 不抛异常。"""
        ch = _make_channel(token="")
        await ch._send_text("u1", "hello")

    @pytest.mark.asyncio
    async def test_send_without_context_token_warns(self):
        """无 context_token 时 _send_text 不抛异常。"""
        ch = _make_channel()
        # 有 token 但没有 context_token
        await ch._send_text("u1", "hello")


# ---------------------------------------------------------------------------
# .stop() 清理
# ---------------------------------------------------------------------------

class TestWeixinStop:
    @pytest.mark.asyncio
    async def test_stop_without_client_noop(self):
        ch = _make_channel()
        await ch.start()  # _start() 因无真实 token 无法初始化 client
        await ch.stop()   # 不崩
        assert ch._client is None


# ---------------------------------------------------------------------------
# 能力声明
# ---------------------------------------------------------------------------

class TestWeixinCapabilities:
    def test_name(self):
        assert _make_channel().name == "wechat"

    def test_supports_buttons(self):
        assert _make_channel().supports_interactive_buttons() is False

    def test_default_config(self):
        cfg = WeixinConfig()
        assert cfg.token == ""
        assert cfg.base_url == "https://ilinkai.weixin.qq.com"
        assert cfg.poll_timeout == 35


# ---------------------------------------------------------------------------
# S1: Token 加密持久化
# ---------------------------------------------------------------------------

class TestTokenEncryption:
    def test_token_encrypted_in_state_file(self, tmp_path):
        """_save_state 写入 token_encrypted 字段，不含明文 token。"""
        ch = _make_channel(token="secret-token", state_dir=str(tmp_path))
        ch._token = "secret-token"
        ch._save_state()

        account = tmp_path / "account.json"
        assert account.exists()
        data = json.loads(account.read_text(encoding="utf-8"))
        assert "token" not in data
        assert "token_encrypted" in data
        assert data["token_encrypted"] != "secret-token"

    def test_token_decrypted_on_load(self, tmp_path):
        """_load_state 正确解密 token_encrypted 字段。"""
        # 手动写加密 token
        account = tmp_path / "account.json"
        account.write_text(
            json.dumps({"token_encrypted": encrypt_token("my-token")}),
            encoding="utf-8",
        )

        ch = _make_channel(token="", state_dir=str(tmp_path))
        assert ch._token == ""
        result = ch._load_state()
        assert result is True
        assert ch._token == "my-token"

    def test_backward_compat_plaintext_token(self, tmp_path):
        """旧格式 account.json（明文 token）仍可读取，保留兼容。"""
        account = tmp_path / "account.json"
        account.write_text(
            json.dumps({"token": "old-plain-token"}),
            encoding="utf-8",
        )

        ch = _make_channel(token="", state_dir=str(tmp_path))
        result = ch._load_state()
        assert result is True
        assert ch._token == "old-plain-token"

    def test_token_encrypted_priority(self, tmp_path):
        """同时存在 token 和 token_encrypted 时优先使用加密字段。"""
        account = tmp_path / "account.json"
        account.write_text(
            json.dumps({
                "token": "old-token",
                "token_encrypted": encrypt_token("new-token"),
            }),
            encoding="utf-8",
        )

        ch = _make_channel(token="", state_dir=str(tmp_path))
        ch._load_state()
        assert ch._token == "new-token"


# ---------------------------------------------------------------------------
# M2: _start() 启动失败反馈
# ---------------------------------------------------------------------------

class TestWeixinStartErrors:
    @pytest.mark.asyncio
    async def test_start_raises_on_missing_token(self, tmp_path):
        """token 未配置且无 account.json → 抛 ChannelFatalError。"""
        ch = _make_channel(token="", state_dir=str(tmp_path))
        ch._running = True
        with pytest.raises(ChannelFatalError, match="token not configured"):
            await ch._start()

    @pytest.mark.asyncio
    async def test_start_raises_on_empty_token(self, tmp_path):
        """token 为空字符串且 account.json 存在 → 抛 ChannelAuthError。"""
        # account.json 存在但 token_encrypted 无效 → 解密后为空
        account = tmp_path / "account.json"
        account.write_text(json.dumps({
            "token_encrypted": "!!!invalid-base64!!!",
            "get_updates_buf": "",
        }))
        ch = _make_channel(token="", state_dir=str(tmp_path))
        ch._running = True
        with pytest.raises(ChannelAuthError, match="token is empty"):
            await ch._start()
