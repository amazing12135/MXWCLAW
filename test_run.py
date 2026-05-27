"""MXWbot DeepSeek 功能测试脚本."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mxwbot.config.loader import load_config
from mxwbot.system.manager import SystemManager


async def test(name: str, msg: str) -> str:
    print(f"\n{'='*60}")
    print(f"Test: {name}")
    print(f"Input: {msg}")
    print(f"{'='*60}")

    cfg = load_config(Path("config.yaml"))
    mgr = SystemManager(cfg)
    await mgr.bootstrap()

    tokens = []
    async def on_token(t):
        tokens.append(t)
        sys.stdout.write(t)
        sys.stdout.flush()

    try:
        result = await asyncio.wait_for(
            mgr.process_direct(msg, f"test_{name.replace(' ', '_')[:20]}",
                              on_stream=on_token),
            timeout=120,
        )
    except asyncio.TimeoutError:
        result = None
        print("\n[TIMEOUT]")

    if result:
        print(f"\nfinish_reason={result.finish_reason}, tool_calls={result.tool_calls_made}")

    await mgr.shutdown()
    return result.content if result else ""


async def main():
    print("MXWbot DeepSeek 功能测试")
    print("=" * 60)

    # Test 1: 基础对话
    await test("基础对话", "用中文介绍你自己, 一句话")

    # Test 2: 文件操作
    await test("写文件", "创建一个文件test.txt在当前目录, 内容是: Hello from MXWbot!")

    # Test 3: 读文件
    await test("读文件", "读取test.txt的内容, 告诉我里面写了什么")

    # Test 4: 列表文件
    await test("列表文件", "列出当前目录下所有.py文件")

    # Test 5: 记忆存储
    await test("记忆写入", "请记住: 我最喜欢的颜色是蓝色。使用记忆系统存储这个信息")

    # Test 6: 记忆检索
    await test("记忆检索", "我之前告诉你我最喜欢的颜色是什么? 从记忆中查找")

    print("\n\nAll tests completed!")


asyncio.run(main())
