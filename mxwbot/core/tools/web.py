"""Web 工具：搜索 + 抓取 + 正文提取。"""

from __future__ import annotations

import re
from typing import Any

import httpx

from mxwbot.core.tools.base import Tool, ToolResult


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------

class WebSearchTool(Tool):
    """DuckDuckGo 搜索（HTML 表单 + API 降级）。"""

    name = "web_search"
    description = "搜索网页内容，返回标题、URL 和摘要"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        },
        "required": ["query"],
    }

    async def execute(self, query: str, **kwargs: Any) -> ToolResult:
        # 方式 1: Bing 搜索（国内可访问）
        result = await self._search_bing(query)
        if result and result.success:
            return result

        # 方式 2: DDG Lite
        result = await self._search_ddg_lite(query)
        if result and result.success:
            return result

        # 方式 3: DDG HTML 表单
        result = await self._search_ddg_html(query)
        if result and result.success:
            return result

        return ToolResult(success=False, error="所有搜索方式均失败，请稍后重试")

    async def _search_bing(self, query: str) -> ToolResult | None:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    "https://www.bing.com/search",
                    params={"q": query, "count": "5"},
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/125.0.0.0 Safari/537.36"
                        ),
                        "Accept-Language": "zh-CN,zh;q=0.9",
                    },
                )
                resp.raise_for_status()
                results = _extract_bing_results(resp.text)
                if results:
                    return ToolResult(content="\n\n".join(results[:5]))
                return ToolResult(success=False, error="Bing 返回空结果")
        except Exception as e:
            return ToolResult(success=False, error=f"Bing 搜索失败: {e}")

    async def _search_ddg_html(self, query: str) -> ToolResult | None:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": query},
                    headers={"User-Agent": "MXWbot/1.0"},
                )
                resp.raise_for_status()
                results = _extract_ddg_results(resp.text)
                if results:
                    return ToolResult(content="\n\n".join(results[:5]))
                return ToolResult(success=False, error="DuckDuckGo 返回空结果")
        except Exception as e:
            return ToolResult(success=False, error=f"DDG HTML 搜索失败: {e}")

    async def _search_ddg_lite(self, query: str) -> ToolResult | None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://lite.duckduckgo.com/lite/",
                    params={"q": query},
                    headers={"User-Agent": "MXWbot/1.0"},
                )
                resp.raise_for_status()
                results = _extract_ddg_lite_results(resp.text)
                if results:
                    return ToolResult(content="\n\n".join(results[:5]))
                return ToolResult(success=False, error="DDG Lite 返回空结果")
        except Exception as e:
            return ToolResult(success=False, error=f"DDG Lite 搜索失败: {e}")


# ---------------------------------------------------------------------------
# WebFetchTool
# ---------------------------------------------------------------------------

class WebFetchTool(Tool):
    """HTTP 页面抓取 + trafilatura 正文提取。"""

    name = "web_fetch"
    description = "抓取指定 URL 的网页内容，自动提取正文"
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的 URL"},
        },
        "required": ["url"],
    }

    async def execute(self, url: str, **kwargs: Any) -> ToolResult:
        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True,
            ) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/125.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                )
                resp.raise_for_status()

                # 用 trafilatura 提取正文
                text = _extract_readable_text(resp.text, url)
                if not text or len(text.strip()) < 50:
                    return ToolResult(
                        success=False,
                        error="页面内容过少或无法提取正文（可能是动态页面）",
                    )
                # 截断到 3000 字
                return ToolResult(content=text[:3000])

        except httpx.HTTPStatusError as e:
            return ToolResult(success=False, error=f"HTTP {e.response.status_code}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))


# ---------------------------------------------------------------------------
# Helpers: DDG HTML 结果提取
# ---------------------------------------------------------------------------

def _extract_ddg_results(html: str) -> list[str]:
    """从 DDG HTML 页面提取 标题 + URL + 摘要。"""
    results: list[str] = []
    # 每个结果块: <a class="result__a" href="...">title</a> ... snippet
    blocks = re.split(r'<a class="result__a"', html)[1:]
    for block in blocks[:5]:
        url_m = re.search(r'href="([^"]+)"', block)
        title = re.sub(r'<[^>]+>', '', block.split('</a>')[0]).strip()
        snippet_m = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span)', block, re.DOTALL)
        snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip() if snippet_m else ""

        if title:
            parts = [f"* {title}"]
            if url_m:
                parts.append(f"- {url_m.group(1)}")
            if snippet:
                parts.append(snippet)
            results.append("\n".join(parts))
    return results


# ---------------------------------------------------------------------------
# Helpers: DDG Lite 结果提取
# ---------------------------------------------------------------------------

def _extract_ddg_lite_results(html: str) -> list[str]:
    """从 DDG Lite 页面提取结果。"""
    results: list[str] = []
    rows = re.findall(r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
    # 描述在 <td class="result-snippet">
    snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', html, re.DOTALL)
    for i, (url, title_raw) in enumerate(rows[:5]):
        title = re.sub(r'<[^>]+>', '', title_raw).strip()
        if not title:
            continue
        parts = [f"* {title}", f"- {url}"]
        if i < len(snippets):
            s = re.sub(r'<[^>]+>', '', snippets[i]).strip()
            if s:
                parts.append(s)
        results.append("\n".join(parts))
    return results


# ---------------------------------------------------------------------------
# Helpers: Bing 结果提取
# ---------------------------------------------------------------------------

def _extract_bing_results(html: str) -> list[str]:
    """从 Bing 搜索结果页提取标题+URL+摘要。"""
    results: list[str] = []
    # Bing 的搜索结果在 <li class="b_algo"> 中
    blocks = re.split(r'<li class="b_algo"', html)[1:]
    for block in blocks[:5]:
        url_m = re.search(r'<a[^>]*href="(https?://[^"]+)"', block)
        title_m = re.search(r'<h2[^>]*><a[^>]*>(.*?)</a>', block, re.DOTALL)
        snippet_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""
        if not title:
            continue
        parts = [f"* {title}"]
        if url_m:
            parts.append(f"- {url_m.group(1)}")
        if snippet_m:
            s = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip()
            if s:
                parts.append(s[:300])
        results.append("\n".join(parts))
    return results


# ---------------------------------------------------------------------------
# Helpers: trafilatura 正文提取
# ---------------------------------------------------------------------------

def _extract_readable_text(html: str, url: str = "") -> str:
    """用 trafilatura 提取网页正文；失败则回退到纯文本。"""
    try:
        import trafilatura
        text = trafilatura.extract(
            html,
            include_links=False,
            include_images=False,
            include_tables=False,
            favor_precision=True,
            url=url,
        )
        if text and len(text.strip()) >= 50:
            return text.strip()
    except Exception:
        pass

    # 降级：去掉所有 HTML 标签，只留文本
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()[:3000]
