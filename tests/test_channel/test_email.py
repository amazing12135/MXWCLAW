"""测试 EmailChannel — 方案 A 模板方法。

Email 是唯一全标准库的 Channel，可通过 mock imaplib/smtplib 完整测试。
"""

import asyncio
import email
from email.mime import multipart as mime_multipart
from email.mime import text as mime_text
import imaplib
import smtplib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mxwbot.channel.email import (
    EmailChannel,
    EmailConfig,
    _decode_header,
    _extract_text_body,
    _html_to_text,
    _reply_subject,
    _extract_uid,
    _imap_connection,
    _is_stale_error,
)
from mxwbot.bus.queue import MessageBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_channel(bus=None, **config_kw):
    if bus is None:
        bus = MessageBus()
    defaults = {
        "imap_host": "imap.test.com",
        "imap_username": "bot@test.com",
        "imap_password": "secret",
        "smtp_host": "smtp.test.com",
        "smtp_username": "bot@test.com",
        "smtp_password": "secret",
        "from_address": "bot@test.com",
        "poll_interval_seconds": 1,
    }
    defaults.update(config_kw)
    return EmailChannel(bus, config=EmailConfig(**defaults))


def _make_raw_email(
    from_addr="user@test.com",
    subject="Test Subject",
    body="Hello world",
    msg_id="<abc@test.com>",
    content_type="text/plain",
):
    """构造原始 MIME 邮件字节。"""
    msg = email.message.EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["Message-ID"] = msg_id
    msg["Date"] = "Mon, 18 May 2026 10:00:00 +0000"
    msg.set_content(body, subtype=content_type.split("/")[-1])
    return msg.as_bytes()


def _mock_imap_fetch(raw_bytes, uid="12345"):
    """构造 IMAP FETCH 返回的 (header, body) 元组。"""
    header = f"1 (UID {uid})".encode()
    return [(header, raw_bytes)]


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestEmailConfig:
    def test_default_config(self):
        cfg = EmailConfig()
        assert cfg.imap_host == ""
        assert cfg.poll_interval_seconds == 30
        assert cfg.subject_prefix == "Re: "
        assert cfg.mark_seen is True

    def test_validate_missing_config(self):
        ch = _make_channel(imap_host="", smtp_host="")
        assert ch._check_config() is False

    def test_validate_complete_config(self):
        ch = _make_channel()
        assert ch._check_config() is True


# ---------------------------------------------------------------------------
# _parse()
# ---------------------------------------------------------------------------

class TestEmailParse:
    def test_parse_dict(self):
        ch = _make_channel()
        result = ch._parse({
            "chat_id": "user@test.com",
            "content": "hello",
            "message_id": "m1",
        })
        assert result is not None
        assert result["chat_id"] == "user@test.com"

    def test_parse_empty_returns_none(self):
        ch = _make_channel()
        assert ch._parse({"chat_id": "", "content": "x"}) is None
        assert ch._parse(None) is None


# ---------------------------------------------------------------------------
# UID extraction
# ---------------------------------------------------------------------------

class TestUIDExtraction:
    def test_extract_uid_from_header(self):
        header = b"1 (UID 12345)"
        uid = _extract_uid([(header, b"body")])
        assert uid == "12345"

    def test_no_uid_returns_empty(self):
        header = b"1 (FLAGS (\\Seen))"
        uid = _extract_uid([(header, b"body")])
        assert uid == ""


# ---------------------------------------------------------------------------
# MIME helpers
# ---------------------------------------------------------------------------

class TestMimeHelpers:
    def test_decode_header_plain(self):
        assert _decode_header("Hello") == "Hello"

    def test_decode_header_utf8_base64(self):
        import base64
        encoded = f"=?UTF-8?B?{base64.b64encode('测试'.encode()).decode()}?="
        assert _decode_header(encoded) == "测试"

    def test_html_to_text(self):
        html = "<p>Hello</p><p>World<br>Line2</p>"
        text = _html_to_text(html)
        assert "Hello" in text
        assert "World" in text

    def test_html_to_text_unicode_escape(self):
        text = _html_to_text("&lt;hello&gt; &amp; &#x4F60;&#x597D;")
        assert "<hello>" in text
        assert "&" in text

    def test_reply_subject_simple(self):
        assert _reply_subject("Hello") == "Re: Hello"

    def test_reply_subject_already_re(self):
        assert _reply_subject("Re: Hello") == "Re: Hello"

    def test_reply_subject_empty(self):
        assert "Re:" in _reply_subject("")

    def test_extract_text_body_plain(self):
        raw = _make_raw_email(body="Hello world", content_type="text/plain")
        parsed = email.parser.BytesParser().parsebytes(raw)
        assert _extract_text_body(parsed) == "Hello world"

    def test_extract_text_body_html(self):
        raw = _make_raw_email(body="<p>Hello</p>", content_type="text/html")
        parsed = email.parser.BytesParser().parsebytes(raw)
        text = _extract_text_body(parsed)
        assert "Hello" in text

    def test_extract_text_body_multipart_prefers_plain(self):
        """multipart/alternative → text/plain 优先于 text/html。"""
        msg = mime_multipart.MIMEMultipart("alternative")
        msg["From"] = "user@test.com"
        msg["Subject"] = "Test"
        msg.attach(mime_text.MIMEText("plain text", "plain"))
        msg.attach(mime_text.MIMEText("<p>html text</p>", "html"))
        parsed = email.parser.BytesParser().parsebytes(msg.as_bytes())
        text = _extract_text_body(parsed)
        assert text == "plain text"

    def test_extract_text_body_multipart_html_fallback(self):
        """仅有 text/html 时降级提取。"""
        msg = email.message.EmailMessage()
        msg["From"] = "user@test.com"
        msg["Subject"] = "Test"
        msg.set_content("<p>only html</p>", subtype="html")
        parsed = email.parser.BytesParser().parsebytes(msg.as_bytes())
        text = _extract_text_body(parsed)
        assert "only html" in text


# ---------------------------------------------------------------------------
# IMAP: _fetch_unread
# ---------------------------------------------------------------------------

class TestEmailFetch:
    def test_fetch_unread_empty(self):
        """无 UNSEEN 邮件时返回空列表。"""
        ch = _make_channel()
        with patch.object(imaplib, "IMAP4_SSL") as mock_ssl:
            mock_conn = MagicMock()
            mock_conn.select.return_value = ("OK", [b"1"])
            mock_conn.search.return_value = ("OK", [b""])
            mock_ssl.return_value = mock_conn

            result = ch._fetch_unread()
            assert result == []
            mock_conn.logout.assert_called_once()

    def test_fetch_unread_one_message(self):
        """有一封 UNSEEN 时正确解析。"""
        ch = _make_channel()
        raw = _make_raw_email(body="Test body", msg_id="<m1@t.com>")

        with patch.object(imaplib, "IMAP4_SSL") as mock_ssl:
            mock_conn = MagicMock()
            mock_conn.select.return_value = ("OK", [b"1"])
            mock_conn.search.return_value = ("OK", [b"1"])
            mock_conn.fetch.return_value = ("OK", _mock_imap_fetch(raw, "100"))

            mock_ssl.return_value = mock_conn

            result = ch._fetch_unread()
            assert len(result) == 1
            assert result[0]["sender"] == "user@test.com"
            assert result[0]["body"] == "Test body"
            assert result[0]["uid"] == "100"

    def test_uid_dedup_second_fetch(self):
        """第二次获取同一 UID 时返回空（去重生效）。"""
        ch = _make_channel()
        raw = _make_raw_email(msg_id="<m1@t.com>")

        # 第一次
        with patch.object(imaplib, "IMAP4_SSL") as mock_ssl:
            mock_conn = MagicMock()
            mock_conn.select.return_value = ("OK", [b"1"])
            mock_conn.search.return_value = ("OK", [b"1"])
            mock_conn.fetch.return_value = ("OK", _mock_imap_fetch(raw, "100"))
            mock_ssl.return_value = mock_conn
            result1 = ch._fetch_unread()
            assert len(result1) == 1

        # 第二次 — 同一 UID 应被去重
        with patch.object(imaplib, "IMAP4_SSL") as mock_ssl:
            mock_conn = MagicMock()
            mock_conn.select.return_value = ("OK", [b"1"])
            mock_conn.search.return_value = ("OK", [b"1"])
            mock_conn.fetch.return_value = ("OK", _mock_imap_fetch(raw, "100"))
            mock_ssl.return_value = mock_conn
            result2 = ch._fetch_unread()
            assert len(result2) == 0

    def test_stale_imap_reconnect(self):
        """IMAP 过期时自动重连一次。"""
        ch = _make_channel()

        with patch.object(imaplib, "IMAP4_SSL") as mock_ssl:
            conn1 = MagicMock()
            conn1.select.side_effect = OSError("EOF occurred in violation of protocol")
            conn2 = MagicMock()
            conn2.select.return_value = ("OK", [b"1"])
            conn2.search.return_value = ("OK", [b""])
            mock_ssl.side_effect = [conn1, conn2]

            result = ch._fetch_unread()
            assert result == []
            conn1.logout.assert_called_once()
            conn2.logout.assert_called_once()


# ---------------------------------------------------------------------------
# Inbound: _process_fetched
# ---------------------------------------------------------------------------

class TestEmailProcessFetched:
    @pytest.mark.asyncio
    async def test_publishes_inbound_message(self):
        bus = MessageBus()
        ch = _make_channel(bus)
        await ch._process_fetched({
            "sender": "user@test.com",
            "subject": "Test",
            "message_id": "<m1@t.com>",
            "uid": "100",
            "body": "Hello from email",
        })

        inbound = await bus.input_queue.get()
        assert inbound.channel == "email"
        assert inbound.chat_id == "user@test.com"
        assert "Hello from email" in inbound.content
        assert "[EMAIL]" in inbound.content
        assert "From: user@test.com" in inbound.content
        assert "Subject: Test" in inbound.content

    @pytest.mark.asyncio
    async def test_caches_subject_and_msg_id(self):
        ch = _make_channel()
        await ch._process_fetched({
            "sender": "u@t.com",
            "subject": "Hello",
            "message_id": "<abc@t.com>",
            "uid": "1",
            "body": "hi",
        })
        assert ch._last_subject.get("u@t.com") == "Hello"
        assert ch._last_msg_id.get("u@t.com") == "<abc@t.com>"


# ---------------------------------------------------------------------------
# Outbound: _send_text via SMTP
# ---------------------------------------------------------------------------

class TestEmailSendText:
    def test_send_text_smtp_called(self):
        """_send_text 调起 SMTP 发送。"""
        ch = _make_channel()
        ch._last_subject["user@test.com"] = "Original subject"

        with patch.object(smtplib, "SMTP") as mock_smtp:
            mock_smtp_conn = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_smtp_conn

            asyncio.run(ch._send_text("user@test.com", "Reply text"))

            mock_smtp_conn.login.assert_called_once()
            mock_smtp_conn.send_message.assert_called_once()
            sent_msg = mock_smtp_conn.send_message.call_args[0][0]
            assert sent_msg["To"] == "user@test.com"
            assert "Re:" in sent_msg["Subject"]
            assert sent_msg.get_content() == "Reply text\n"

    def test_send_text_no_smtp_host(self):
        """SMTP host 未配置时 _send_text 静默跳过。"""
        ch = _make_channel(smtp_host="")
        asyncio.run(ch._send_text("user@test.com", "test"))


# ---------------------------------------------------------------------------
# .stop() cleanup
# ---------------------------------------------------------------------------

class TestEmailStop:
    @pytest.mark.asyncio
    async def test_stop_cancels_poll_task(self):
        ch = _make_channel()
        await ch.start()
        assert ch._poll_task is not None
        await ch.stop()
        assert ch._poll_task is None or ch._poll_task.done()


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

class TestEmailCapabilities:
    def test_name(self):
        assert _make_channel().name == "email"

    def test_supports_buttons(self):
        assert _make_channel().supports_interactive_buttons() is False


# ---------------------------------------------------------------------------
# S4: IMAP SSL 主机名校验
# ---------------------------------------------------------------------------

class TestIMAPSSL:
    def test_ssl_context_passed_to_imap4_ssl(self):
        """_connect_imap 使用 ssl.create_default_context()。"""
        with patch("imaplib.IMAP4_SSL") as mock_ssl:
            mock_conn = MagicMock()
            mock_ssl.return_value = mock_conn

            ch = _make_channel(imap_use_ssl=True)
            conn = ch._connect_imap()

            # 检查 IMAP4_SSL 被调用时含有 ssl_context
            call_kwargs = mock_ssl.call_args
            assert call_kwargs is not None
            # ssl_context 应该被传入
            assert "ssl_context" in call_kwargs[1]

    def test_ssl_fallback_on_error(self):
        """SSL 校验失败时 fallback 不校验。"""
        import ssl
        with patch("imaplib.IMAP4_SSL") as mock_ssl:
            mock_conn_bad = MagicMock()
            mock_conn_bad.login.side_effect = ssl.SSLError("cert verify failed")
            mock_conn_good = MagicMock()
            mock_ssl.side_effect = [mock_conn_bad, mock_conn_good]

            ch = _make_channel(imap_use_ssl=True)
            conn = ch._connect_imap()

            # 第二次调用（fallback）成功
            assert conn is mock_conn_good
            assert mock_ssl.call_count >= 2


# ---------------------------------------------------------------------------
# M5: IMAP 上下文管理器
# ---------------------------------------------------------------------------

class TestIMAPConnectionCM:
    def test_context_manager_connects_and_logs_out(self):
        """_imap_connection 正常获取连接并 logout。"""
        mock_conn = MagicMock()
        ch = _make_channel()
        ch._connect_imap = MagicMock(return_value=mock_conn)

        with _imap_connection(ch) as conn:
            assert conn is mock_conn

        mock_conn.logout.assert_called_once()

    def test_context_manager_cleanup_on_error(self):
        """操作异常时上下文管理器仍 logout。"""
        mock_conn = MagicMock()
        ch = _make_channel()
        ch._connect_imap = MagicMock(return_value=mock_conn)

        try:
            with _imap_connection(ch):
                raise RuntimeError("fetch failed")
        except RuntimeError:
            pass

        # 即使异常，logout 仍被调用
        mock_conn.logout.assert_called_once()

    def test_imap_retry_on_stale(self):
        """stale error 触发重连重试。"""
        ch = _make_channel()
        mock_conn_stale = MagicMock()
        mock_conn_good = MagicMock()
        ch._connect_imap = MagicMock(side_effect=[
            mock_conn_stale, mock_conn_good,
        ])
        ch._fetch_unread_once = MagicMock(side_effect=[
            imaplib.IMAP4.error("eof occurred in violation of protocol"),
            [{"sender": "test@test.com", "subject": "ok", "message_id": "m1", "uid": "1", "body": "ok"}],
        ])

        results = ch._fetch_unread()
        assert len(results) == 1
        assert results[0]["body"] == "ok"
        assert ch._connect_imap.call_count == 2

    def test_is_stale_error_matches_markers(self):
        """_is_stale_error 正确匹配各种断线标记。"""
        for marker in [
            "disconnected for inactivity",
            "EOF occurred in violation of protocol",
            "socket error",
            "connection reset",
            "broken pipe",
            "bye",
        ]:
            assert _is_stale_error(Exception(marker)) is True

    def test_is_stale_error_rejects_normal_error(self):
        """非 stale error 应返回 False。"""
        assert _is_stale_error(ValueError("bad thing")) is False
