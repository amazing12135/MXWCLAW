"""测试 utils/security.py — SSRF URL 校验 + 路径安全."""

import pytest

from mxwbot.utils.security import (
    _is_private_host,
    is_path_safe,
    validate_url,
    sanitize_secrets,
)


class TestSSRF:
    def test_validate_url_blocks_private_ip(self):
        with pytest.raises(ValueError, match="private"):
            validate_url("http://127.0.0.1/admin")

    def test_validate_url_blocks_private_range(self):
        for ip in ("10.0.0.1", "192.168.1.1", "172.16.0.1"):
            with pytest.raises(ValueError, match="private"):
                validate_url(f"http://{ip}/")

    def test_validate_url_allows_public(self):
        assert validate_url("https://example.com/path")

    def test_validate_url_rejects_non_http(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_url("file:///etc/passwd")
        with pytest.raises(ValueError, match="scheme"):
            validate_url("ftp://example.com")

    def test_validate_url_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            validate_url("")

    def test_validate_url_rejects_no_hostname(self):
        with pytest.raises(ValueError, match="hostname"):
            validate_url("http:///path")

    def test_is_private_host_literal(self):
        assert _is_private_host("127.0.0.1") is True
        assert _is_private_host("10.0.0.1") is True
        assert _is_private_host("192.168.1.1") is True
        assert _is_private_host("::1") is True

    def test_is_private_host_empty(self):
        assert _is_private_host("") is False


class TestPathSafety:
    def test_is_path_safe_simple(self, tmp_path):
        assert is_path_safe("test.txt", str(tmp_path))

    def test_is_path_safe_traversal(self, tmp_path):
        assert is_path_safe("../etc/passwd", str(tmp_path)) is False
        assert is_path_safe("..\\..\\windows", str(tmp_path)) is False

    def test_is_path_safe_empty(self, tmp_path):
        assert is_path_safe("", str(tmp_path)) is False


class TestSecretScrubbing:
    def test_sanitize_secrets(self):
        assert sanitize_secrets("my-key-123", ["my-key-123"]) == "***"
        assert sanitize_secrets("a b c", ["b"]) == "a *** c"

    def test_sanitize_no_match(self):
        assert sanitize_secrets("hello", ["secret"]) == "hello"
