"""Test core/tools/web.py"""

import pytest

from mxwbot.core.tools.web import WebFetchTool
from unittest.mock import AsyncMock, patch


class TestWebFetchTool:
    @pytest.mark.asyncio
    async def test_fetch_success(self):
        tool = WebFetchTool()
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        # trafilatura needs enough text to extract (>=50 chars after extraction)
        mock_resp.text = "<html><body><p>" + "hello world " * 10 + "</p></body></html>"
        mock_resp.raise_for_status = lambda: None

        with patch("httpx.AsyncClient.get", return_value=mock_resp):
            r = await tool.execute(url="https://example.com")
        assert r.success is True

    @pytest.mark.asyncio
    async def test_fetch_error(self):
        tool = WebFetchTool()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: (_ for _ in ()).throw(Exception("404"))

        with patch("httpx.AsyncClient.get", return_value=mock_resp):
            r = await tool.execute(url="https://example.com")
        assert r.success is False
