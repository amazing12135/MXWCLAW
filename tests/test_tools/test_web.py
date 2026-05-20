"""Test core/tools/web.py"""

import pytest

from mxwbot.core.tools.web import WebFetchTool
from unittest.mock import AsyncMock, patch


class TestWebFetchTool:
    @pytest.mark.asyncio
    async def test_fetch_success(self):
        tool = WebFetchTool(timeout=5)
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>hello</html>"
        mock_resp.raise_for_status = lambda: None

        with patch("httpx.AsyncClient.get", return_value=mock_resp):
            r = await tool.execute(url="https://example.com")
        assert r.success is True
        assert "hello" in r.content

    @pytest.mark.asyncio
    async def test_fetch_error(self):
        tool = WebFetchTool(timeout=5)
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: (_ for _ in ()).throw(Exception("404"))

        with patch("httpx.AsyncClient.get", return_value=mock_resp):
            r = await tool.execute(url="https://example.com")
        assert r.success is False
