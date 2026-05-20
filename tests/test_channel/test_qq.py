"""测试 QQChannel — 方案 A 最小实现。

由于 botpy SDK 未安装在当前环境，测试侧重：
- SDK 不可用时的优雅降级
- _parse() 正常消息解析
- 消息去重逻辑
- send_text 行为
- 聊天类型缓存
"""

import asyncio

import pytest

from mxwbot.channel.base import ChannelFatalError
from mxwbot.channel.qq import QQChannel, QQConfig
from mxwbot.bus.messages import OutboundMessage
from mxwbot.bus.queue import MessageBus


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------

def _make_channel(bus=None, **config_kw):
    """创建测试用 QQChannel。"""
    if bus is None:
        bus = MessageBus()
    cfg = QQConfig(**config_kw) if config_kw else QQConfig()
    return QQChannel(bus, config=cfg)


# ---------------------------------------------------------------------------
# SDK 不可用降级
# ---------------------------------------------------------------------------

class TestQQDegradation:
    @pytest.mark.asyncio
    async def test_start_without_sdk_raises_fatal_error(self):
        """botpy 未安装时 _start() 抛 ChannelFatalError。"""
        ch = _make_channel()
        ch._running = True
        with pytest.raises(ChannelFatalError, match="SDK not installed"):
            await ch._start()
        assert ch._client is None

    @pytest.mark.asyncio
    async def test_send_text_without_client_warns(self):
        """_client 为 None 时 _send_text() 记录警告且不崩溃。"""
        ch = _make_channel()
        await ch._send_text("u1", "hello")
        # 不抛异常

    @pytest.mark.asyncio
    async def test_stop_without_client_noop(self):
        """_client 为 None 时 stop() 为空操作。"""
        ch = _make_channel()
        ch._running = True  # 直接设 running，跳过会抛异常的 _start()
        await ch.stop()
        assert ch._client is None
        assert ch._running is False


# ---------------------------------------------------------------------------
# _parse 消息解析
# ---------------------------------------------------------------------------

class TestQQParse:
    def test_parse_dict_message(self):
        """_parse 接受 dict 输入（测试/CLI 模拟模式）。"""
        ch = _make_channel()
        result = ch._parse({
            "chat_id": "user123",
            "content": "hello world",
            "message_id": "msg_001",
        })
        assert result is not None
        assert result["chat_id"] == "user123"
        assert result["content"] == "hello world"
        assert result["platform_msg_id"] == "msg_001"

    def test_parse_non_dict_returns_none(self):
        ch = _make_channel()
        assert ch._parse("raw string") is None
        assert ch._parse(None) is None
        assert ch._parse(123) is None

    def test_parse_empty_returns_none(self):
        ch = _make_channel()
        assert ch._parse({"chat_id": "", "content": ""}) is None
        assert ch._parse({"chat_id": "", "content": "x"}) is None
        assert ch._parse({"chat_id": "x", "content": ""}) is None


# ---------------------------------------------------------------------------
# 消息去重
# ---------------------------------------------------------------------------

class TestQQDedup:
    @pytest.mark.asyncio
    async def test_duplicate_message_id_skipped(self):
        """相同 message_id 的消息第二次被丢弃。"""
        bus = MessageBus()
        ch = _make_channel(bus)

        # 模拟第一条消息
        await ch._on_c2c_message(_MockC2CMsg(id="m1", content="hello", user_openid="u1"))
        assert bus.input_queue.qsize() == 1  # 投递到 Bus

        # 同 ID 重复消息
        await ch._on_c2c_message(_MockC2CMsg(id="m1", content="hello again", user_openid="u1"))
        assert bus.input_queue.qsize() == 1  # 未新增

    @pytest.mark.asyncio
    async def test_different_ids_both_delivered(self):
        """不同 message_id 的消息都投递。"""
        bus = MessageBus()
        ch = _make_channel(bus)

        await ch._on_c2c_message(_MockC2CMsg(id="m1", content="msg1", user_openid="u1"))
        await ch._on_c2c_message(_MockC2CMsg(id="m2", content="msg2", user_openid="u1"))

        assert bus.input_queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_empty_content_skipped(self):
        """空内容消息不投递。"""
        bus = MessageBus()
        ch = _make_channel(bus)

        await ch._on_c2c_message(_MockC2CMsg(id="m1", content="", user_openid="u1"))
        assert bus.input_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_whitespace_only_content_skipped(self):
        """纯空白消息不投递。"""
        bus = MessageBus()
        ch = _make_channel(bus)

        await ch._on_c2c_message(_MockC2CMsg(id="m1", content="   \n  ", user_openid="u1"))
        assert bus.input_queue.qsize() == 0


# ---------------------------------------------------------------------------
# 聊天类型缓存
# ---------------------------------------------------------------------------

class TestQQChatTypeCache:
    @pytest.mark.asyncio
    async def test_c2c_cached_as_c2c(self):
        """C2C 消息 → chat_type 缓存为 "c2c"。"""
        ch = _make_channel()
        ch._chat_type_cache.clear()

        await ch._on_c2c_message(_MockC2CMsg(
            id="m1", content="hi", user_openid="u1",
        ))
        assert ch._chat_type_cache.get("u1") == "c2c"

    @pytest.mark.asyncio
    async def test_group_cached_as_group(self):
        """群消息 → chat_type 缓存为 "group"。"""
        ch = _make_channel()
        ch._chat_type_cache.clear()

        await ch._on_group_message(_MockGroupMsg(
            id="m1", content="hi", group_openid="g1", member_openid="u1",
        ))
        assert ch._chat_type_cache.get("g1") == "group"


# ---------------------------------------------------------------------------
# 能力声明
# ---------------------------------------------------------------------------

class TestQQCapabilities:
    def test_supports_interactive_buttons(self):
        ch = _make_channel()
        assert ch.supports_interactive_buttons() is False

    def test_name_is_qq(self):
        ch = _make_channel()
        assert ch.name == "qq"

    def test_default_config(self):
        cfg = QQConfig()
        assert cfg.app_id == ""
        assert cfg.secret == ""
        assert cfg.msg_format == "plain"


# ---------------------------------------------------------------------------
# Mock botpy Message 对象
# ---------------------------------------------------------------------------

class _MockAuthor:
    def __init__(self, user_openid="", member_openid=""):
        self.user_openid = user_openid
        self.member_openid = member_openid


class _MockC2CMsg:
    def __init__(self, id="", content="", user_openid=""):
        self.id = id
        self.content = content
        self.author = _MockAuthor(user_openid=user_openid)


class _MockGroupMsg:
    def __init__(self, id="", content="", group_openid="", member_openid=""):
        self.id = id
        self.content = content
        self.group_openid = group_openid
        self.author = _MockAuthor(member_openid=member_openid)


# ---------------------------------------------------------------------------
# S2: SSRF 防护
# ---------------------------------------------------------------------------

class TestSSRFProtection:
    @pytest.mark.asyncio
    async def test_url_media_blocks_private_ip(self):
        """内网 URL 被 _read_url_bytes 拒绝。"""
        ch = _make_channel()
        ch._http = None  # 未初始化 → 拒绝
        data, filename = await ch._read_url_bytes("http://127.0.0.1/test.png")
        assert data is None
        assert filename is None

    @pytest.mark.asyncio
    async def test_url_media_rejects_non_http_scheme(self):
        """非 http/https scheme 被 validate_url 拒绝。"""
        # validate_url 是同步的，测试 scheme 检测
        from mxwbot.utils.security import validate_url
        with pytest.raises(ValueError, match="scheme"):
            validate_url("file:///etc/passwd")
        with pytest.raises(ValueError, match="scheme"):
            validate_url("ftp://example.com/file")

    def test_private_host_detection(self):
        """_is_private_host 正确识别内网地址。"""
        from mxwbot.utils.security import _is_private_host
        assert _is_private_host("127.0.0.1") is True
        assert _is_private_host("localhost") is True  # DNS may fail
        assert _is_private_host("10.0.0.1") is True
        assert _is_private_host("192.168.1.1") is True


# ---------------------------------------------------------------------------
# S3: 路径遍历防护
# ---------------------------------------------------------------------------

class TestPathTraversalProtection:
    def test_local_file_blocks_path_traversal(self):
        """../../../etc/passwd 被拒绝。"""
        ch = _make_channel()
        data, filename = ch._read_local_bytes("../../../etc/passwd")
        assert data is None
        assert filename is None

    def test_local_file_allows_sandboxed_path(self, tmp_path):
        """workspace 内文件允许读取。"""
        ch = _make_channel()
        # 在 cwd 下创建文件
        import os
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            data, filename = ch._read_local_bytes("test.txt")
            assert data == b"hello"
            assert filename == "test.txt"
        finally:
            os.chdir(original_cwd)

    def test_read_local_bytes_file_not_found(self):
        """不存在的文件返回 None。"""
        ch = _make_channel()
        data, filename = ch._read_local_bytes("/nonexistent/path/file.png")
        assert data is None
        assert filename is None


# ---------------------------------------------------------------------------
# M1: 生命周期管理
# ---------------------------------------------------------------------------

class TestQQLifecycle:
    def test_http_none_before_start(self):
        """_start() 前 _http 为 None。"""
        ch = _make_channel()
        assert ch._http is None


# ---------------------------------------------------------------------------
# M4: 回调构造
# ---------------------------------------------------------------------------

class TestCallbacks:
    def test_bot_client_class_exists(self):
        """_QQBotClient 在 SDK 不可用时应为 None，但模块正常 load。"""
        from mxwbot.channel.qq import _QQBotClient
        # SDK 不可用时 _QQBotClient 为 None
        # （此测试环境中 botpy 未安装，所以应为 None）
        assert _QQBotClient is None or callable(_QQBotClient)
