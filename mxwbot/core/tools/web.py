"""Web 工具：搜索 + 抓取。"""

from __future__ import annotations

from typing import Any

import httpx

from mxwbot.core.tools.base import Tool, ToolResult


class WebSearchTool(Tool):
    """DuckDuckGo 搜索。"""

    name = "web_search"
    description = "搜索网页内容（DuckDuckGo）"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        },
        "required": ["query"],
    }

    async def execute(self, query: str, **kwargs: Any) -> ToolResult:
        url = "https://html.duckduckgo.com/html/"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, data={"q": query})
                resp.raise_for_status()
                # 简单提取结果摘要
                body = resp.text
                snippets = _extract_ddg_snippets(body)
                if snippets:
                    return ToolResult(content="\n\n".join(snippets[:5]))
                return ToolResult(content="(未找到结果)")
        except Exception as e:
            return ToolResult(success=False, error=str(e))


def _extract_ddg_snippets(html: str) -> list[str]:
    """从 DuckDuckGo HTML 页面中提取搜索结果摘要。"""
    import re
    results: list[str] = []
    # 匹配 result__snippet 区域
    for m in re.finditer(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span)', html, re.DOTALL):
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if text and len(text) > 20:
            results.append(text)
    return results


class WebFetchTool(Tool):
    """HTTP 页面抓取。"""

    name = "web_fetch"
    description = "抓取指定 URL 的网页内容"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的 URL"},
        },
        "required": ["url"],
    }

    def __init__(self, timeout: int = 30):
        self._timeout = timeout

    async def execute(self, url: str, **kwargs: Any) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "MXWbot/1.0"})
                resp.raise_for_status()
                content = resp.text[:10_000]  # 限制大小
                return ToolResult(content=content)
        except Exception as e:
            return ToolResult(success=False, error=str(e))
