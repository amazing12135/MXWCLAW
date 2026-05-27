"""测试 web 搜索工具"""

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

async def main():
    from mxwbot.core.tools.web import WebSearchTool, WebFetchTool

    # Test 1: Search (Bing → DDG Lite → DDG HTML)
    print("=" * 50)
    print("Test 1: 搜索 'Python tutorial' (Bing优先)")
    tool = WebSearchTool()
    r = await tool.execute("Python tutorial")
    print(f"  Success: {r.success}")
    c = (r.content or r.error or "")
    c = c.replace('\U0001f4cc', '[PIN]').replace('\U0001f517', '[LINK]')
    print(f"  Content: {c[:300]}")

    # Test 2: Chinese search
    print("\n" + "=" * 50)
    print("Test 2: search 'Champions League 2026'")
    r = await tool.execute("Champions League 2026 final score")
    print(f"  Success: {r.success}")
    c = (r.content or r.error or "")
    c = c.replace('\U0001f4cc', '[PIN]').replace('\U0001f517', '[LINK]')
    print(f"  Content: {c[:300]}")

    # Test 3: Fetch a real page
    print("\n" + "=" * 50)
    print("Test 3: Fetch 'https://httpbin.org/html'")
    fetch = WebFetchTool()
    r = await fetch.execute("https://httpbin.org/html")
    print(f"  Success: {r.success}")
    print(f"  Content length: {len(r.content) if r.content else 0}")
    print(f"  Preview: {(r.content or r.error)[:200]}")

    # Test 4: Fetch a news page
    print("\n" + "=" * 50)
    print("Test 4: Fetch 'https://en.wikipedia.org/wiki/Python_(programming_language)'")
    r = await fetch.execute("https://en.wikipedia.org/wiki/Python_(programming_language)")
    print(f"  Success: {r.success}")
    print(f"  Content length: {len(r.content) if r.content else 0}")
    print(f"  Preview: {(r.content or r.error)[:200]}")

    print("\nDone!")

asyncio.run(main())
