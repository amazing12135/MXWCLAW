"""MXWbot E2E Sandbox Test Suite.

Run all 24 scenarios from the evaluation framework through process_direct().
Requires only a DeepSeek API key — no channels needed.

Usage::

    $env:MXWBOT_API_KEY="sk-xxx"
    .venv/Scripts/python.exe tests/e2e/run_e2e.py

Output: ``tests/e2e/results/report.md`` + ``metrics.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mxwbot.config.schema import (
    AgentDefaultConfig, HeartBeatConfig, MXWConfig, ProviderConfig,
)
from mxwbot.system.manager import SystemManager


# ============================================================================
# Data
# ============================================================================

@dataclass
class ScenarioResult:
    id: str           # e.g. "A1"
    name: str         # e.g. "纯文本问答"
    passed: bool
    duration_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    finish_reason: str = ""
    error: str = ""
    content_preview: str = ""


# ============================================================================
# Runner
# ============================================================================

class E2ERunner:
    def __init__(self, api_key: str, model: str = "deepseek-v4-flash",
                 base_url: str = "https://api.deepseek.com") -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self.mgr: SystemManager | None = None
        self._consumer_task: asyncio.Task | None = None
        self._setup_ms: float = 0

    # -- lifecycle -----------------------------------------------------------

    async def setup(self) -> float:
        t0 = time.monotonic()
        ws = tempfile.mkdtemp(prefix="mxwbot_e2e_")
        cfg = MXWConfig(
            workspace=ws,
            providers=[ProviderConfig(
                name="deepseek", api_key=self._api_key, model=self._model,
                base_url=self._base_url,
            )],
            channels=[],
            agent=AgentDefaultConfig(max_iterations=10),
            heartbeat=HeartBeatConfig(enabled=False),
        )
        self.mgr = SystemManager(cfg)
        await self.mgr.bootstrap()
        self.mgr.loop_pool._running = True
        self._consumer_task = asyncio.create_task(self.mgr.loop_pool._consume())
        self._setup_ms = (time.monotonic() - t0) * 1000
        return self._setup_ms

    async def teardown(self) -> None:
        if self.mgr is not None:
            self.mgr.loop_pool._running = False
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

    # -- run one scenario ----------------------------------------------------

    async def run_scenario(
        self, sid: str, name: str,
        content: str, session_key: str, *,
        keywords: list[str] | None = None,
        timeout: float = 120,
    ) -> ScenarioResult:
        t0 = time.monotonic()
        try:
            result = await self.mgr.loop_pool.process_direct(
                content, session_key, timeout=timeout,
            )
            duration = (time.monotonic() - t0) * 1000
            finish = result.finish_reason or "unknown"
            text = result.content or ""

            # Basic correctness: got a non-empty response, not an error
            ok = True
            if not text.strip():
                ok = False
            elif finish == "error":
                ok = False

            # Keyword checks
            if ok and keywords:
                tl = text.lower()
                missing = [k for k in keywords if k.lower() not in tl]
                if missing:
                    ok = False

            usage = result.usage if hasattr(result, 'usage') else None
            in_tok = usage.input_tokens if usage else 0
            out_tok = usage.output_tokens if usage else 0
            tc = getattr(result, 'tool_calls_made', 0)
            return ScenarioResult(
                id=sid, name=name, passed=ok,
                duration_ms=duration,
                input_tokens=in_tok,
                output_tokens=out_tok,
                tool_calls=tc,
                finish_reason=finish,
                content_preview=text[:300],
            )
        except asyncio.TimeoutError:
            return ScenarioResult(
                id=sid, name=name, passed=False,
                duration_ms=(time.monotonic() - t0) * 1000,
                error="TimeoutError",
            )
        except Exception as exc:
            return ScenarioResult(
                id=sid, name=name, passed=False,
                duration_ms=(time.monotonic() - t0) * 1000,
                error=f"{type(exc).__name__}: {exc!s}"[:300],
            )


# ============================================================================
# Scenario groups (async generators yielding (sid, name, content, session, kw))
# ============================================================================

async def run_group_a(runner: E2ERunner) -> list[ScenarioResult]:
    """A组 — 基础对话 (5 scenarios)."""
    results = []

    # A1 — 纯文本问答
    r = await runner.run_scenario(
        "A1", "纯文本问答",
        "Python 的 GIL 是什么？请用一句话简短回答。",
        "e2e:a1",
        keywords=["GIL", "Python"],
    )
    results.append(r)

    # A2 — 多轮上下文
    ses = "e2e:a2"
    await runner.run_scenario("A2a", "多轮上下文-设置", "我叫小明", ses)
    await runner.run_scenario("A2b", "多轮上下文-补充", "我是一名工程师", ses)
    r = await runner.run_scenario(
        "A2", "多轮上下文-查询",
        "我叫什么？我做什么工作？", ses,
        keywords=["小明", "工程师"],
    )
    results.append(r)

    # A3 — 中英文混合
    r = await runner.run_scenario(
        "A3", "中英文混合",
        "帮我翻译 'Hello World' 为中文",
        "e2e:a3",
        keywords=["你好"],
    )
    results.append(r)

    # A4 — 空消息
    r = await runner.run_scenario(
        "A4", "空消息",
        " ", "e2e:a4",
    )
    # A4 passes as long as it doesn't error (already checked in run_scenario)
    results.append(r)

    # A5 — 超短消息
    r = await runner.run_scenario(
        "A5", "超短消息",
        "?", "e2e:a5",
    )
    results.append(r)

    return results


async def run_group_b(runner: E2ERunner) -> list[ScenarioResult]:
    """B组 — 工具调用 (8 scenarios)."""
    results = []

    # Create a test file for tool scenarios
    test_file = Path(runner.mgr.config.workspace) / "test_data.txt"
    test_file.write_text("Hello World\nThis is a test file.\nTODO: fix this bug\n", encoding="utf-8")

    # B1 — 读文件
    r = await runner.run_scenario(
        "B1", "只读-读文件",
        "读取 test_data.txt 的内容", "e2e:b1",
        keywords=["Hello", "TODO"],
    )
    results.append(r)

    # B2 — 列目录
    r = await runner.run_scenario(
        "B2", "只读-列目录",
        "列出当前目录下的所有 .py 文件", "e2e:b2",
        keywords=["py"],  # at least mentions Python files
    )
    results.append(r)

    # B3 — 搜索内容
    r = await runner.run_scenario(
        "B3", "只读-搜索内容",
        "在所有 .py 文件中搜索 'import' 这个关键词", "e2e:b3",
        keywords=["import"],
    )
    results.append(r)

    # B4 — 写-创建文件 (auto-approved in process_direct)
    r = await runner.run_scenario(
        "B4", "写-创建文件",
        "创建 hello.txt，内容为 'Hello World'", "e2e:b4",
        keywords=["hello"],
    )
    results.append(r)

    # B5 — 写-修改文件
    r = await runner.run_scenario(
        "B5", "写-修改文件",
        "把 hello.txt 里的 World 改成 MXW", "e2e:b5",
        keywords=["MXW"],
    )
    results.append(r)

    # B6 — 写-删除文件
    r = await runner.run_scenario(
        "B6", "写-删除文件",
        "删除 hello.txt", "e2e:b6",
    )
    results.append(r)

    # B7 — 写-确认超时 (process_direct skips confirmation, tools auto-execute)
    r = await runner.run_scenario(
        "B7", "写-确认(自动通过)",
        "创建 confirmed.txt，内容为 'auto-approved'", "e2e:b7",
        keywords=["auto"],
    )
    results.append(r)

    # B8 — 多工具串行
    r = await runner.run_scenario(
        "B8", "多工具串行",
        "读取 test_data.txt → 列出当前目录文件", "e2e:b8",
        keywords=["TODO"],
    )
    results.append(r)

    return results


async def run_group_c(runner: E2ERunner) -> list[ScenarioResult]:
    """C组 — 批量确认 (3 scenarios)."""
    results = []

    # Create files for batch tests
    ws = Path(runner.mgr.config.workspace)
    (ws / "a.txt").write_text("content a", encoding="utf-8")
    (ws / "b.txt").write_text("content b", encoding="utf-8")

    # C1 — 全是只读 (should not trigger confirmation at all)
    r = await runner.run_scenario(
        "C1", "全是只读-不弹确认",
        "读取 a.txt 和 b.txt 的内容", "e2e:c1",
        keywords=["content"],
    )
    results.append(r)

    # C2 — 混合读写
    r = await runner.run_scenario(
        "C2", "混合读写",
        "读取 a.txt，然后把内容写入 c.txt", "e2e:c2",
        keywords=["content"],
    )
    results.append(r)

    # C3 — 全是写 (auto-approved)
    r = await runner.run_scenario(
        "C3", "全是写-一次确认",
        "创建 x.txt、y.txt、z.txt，内容分别为 1 2 3", "e2e:c3",
        keywords=["1", "2", "3"],
    )
    results.append(r)

    return results


async def run_group_d(runner: E2ERunner) -> list[ScenarioResult]:
    """D组 — 命令与会话 (3 scenarios)."""
    results = []

    # D1 — /clear
    ses = "e2e:d1"
    await runner.run_scenario("D1a", "命令-发消息", "记住: 我的密码是123456", ses)
    r = await runner.run_scenario(
        "D1", "命令-/clear",
        "/clear", ses,
        keywords=["已清空"],
    )
    results.append(r)
    # Verify: after /clear, asking about password should NOT find it
    r2 = await runner.run_scenario(
        "D1v", "命令-/clear验证",
        "我的密码是什么？", ses,
    )
    # Should NOT contain "123456" after clear
    d1v_ok = "123456" not in (r2.content_preview or "")
    results.append(ScenarioResult(
        id="D1v", name="/clear 验证-信息已遗忘",
        passed=d1v_ok,
        duration_ms=r2.duration_ms,
        finish_reason=r2.finish_reason,
        content_preview=r2.content_preview[:200],
    ))

    # D2 — /status
    r = await runner.run_scenario(
        "D2", "命令-/status",
        "/status", "e2e:d2",
        keywords=["消息"],
    )
    results.append(r)

    # D4 — 会话持久化
    ses = "e2e:d4"
    await runner.run_scenario("D4a", "会话-写消息", "今天天气真好", ses)
    # Read the session from disk
    session = await runner.mgr.sessions.get_session("cli", "direct")
    d4_ok = len(session.messages) > 0
    results.append(ScenarioResult(
        id="D4", name="会话持久化",
        passed=d4_ok,
        duration_ms=0,
        content_preview=f"session messages count: {len(session.messages)}",
    ))

    return results


async def run_group_e(runner: E2ERunner) -> list[ScenarioResult]:
    """E组 — 记忆系统 (2 scenarios)."""
    results = []

    # E1 — 会话内记忆
    ses = "e2e:e1"
    await runner.run_scenario("E1a", "记忆-录入", "我的项目叫 MXWbot，它是一个聊天机器人框架", ses)
    r = await runner.run_scenario(
        "E1", "记忆-查询",
        "我的项目叫什么？它是一个什么框架？", ses,
        keywords=["MXWbot"],
    )
    results.append(r)

    # E3 — 记忆压缩 (发送多条消息触发 compact)
    ses = "e2e:e3"
    # Send several messages to build up context
    for i in range(5):
        await runner.run_scenario(
            f"E3-{i}", f"记忆-填充{i}",
            f"消息{i}: 这是一个用于测试记忆压缩功能的长文本消息。" * 8,
            ses,
            timeout=60,
        )
    r = await runner.run_scenario(
        "E3", "记忆压缩",
        "总结一下我们之前讨论了什么？", ses,
    )
    results.append(r)

    return results


async def run_group_f(runner: E2ERunner) -> list[ScenarioResult]:
    """F组 — SubAgent (1 scenario)."""
    results = []

    # F1 — 单 SubAgent: 统计 .py 文件行数
    r = await runner.run_scenario(
        "F1", "SubAgent 统计",
        "帮我统计 mxwbot/core/ 目录下所有 .py 文件的总行数。用 subagent 来完成。",
        "e2e:f1",
        keywords=["mxwbot", "core"],
        timeout=180,
    )
    results.append(r)

    # F2 — 并行 SubAgent: 读取 workspace 内 file
    r = await runner.run_scenario(
        "F2", "并行 SubAgent",
        "同时读取 a.txt 和 b.txt 的内容", "e2e:f2",
        keywords=["content"],
        timeout=180,
    )
    results.append(r)

    return results


# ============================================================================
# Report generation
# ============================================================================

def generate_report(all_results: list[ScenarioResult], setup_ms: float,
                    total_ms: float) -> str:
    """Generate markdown report + JSON metrics."""
    passed = sum(1 for r in all_results if r.passed)
    total = len(all_results)
    rate = (passed / total * 100) if total > 0 else 0

    total_input = sum(r.input_tokens for r in all_results)
    total_output = sum(r.output_tokens for r in all_results)
    durations = [r.duration_ms for r in all_results if r.duration_ms > 0]
    avg_ms = sum(durations) / len(durations) if durations else 0

    # Group breakdown
    groups: dict[str, list[ScenarioResult]] = {}
    for r in all_results:
        g = r.id[0] if r.id else "?"
        groups.setdefault(g, []).append(r)

    lines: list[str] = []
    lines.append("# MXWbot E2E 沙箱测试报告")
    lines.append(f"\n**测试时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"**初始 setup**: {setup_ms:.0f} ms")
    lines.append(f"**总耗时**: {total_ms:.0f} ms ({total_ms/1000:.1f} s)")

    # ── Summary ──
    lines.append(f"\n## 概要")
    lines.append(f"| 指标 | 值 |")
    lines.append(f"|------|----|")
    lines.append(f"| 场景通过率 | **{passed}/{total} ({rate:.1f}%)** |")
    lines.append(f"| 总 Token (入) | {total_input:,} |")
    lines.append(f"| 总 Token (出) | {total_output:,} |")
    lines.append(f"| 平均延迟 | {avg_ms:.0f} ms |")

    # ── Group summaries ──
    lines.append(f"\n## 分组结果")
    lines.append(f"| 组 | 场景数 | 通过 | 通过率 |")
    lines.append(f"|----|--------|------|--------|")
    for gid in sorted(groups):
        g = groups[gid]
        gp = sum(1 for r in g if r.passed)
        lines.append(f"| {gid}组 | {len(g)} | {gp} | {gp/len(g)*100:.0f}% |")

    # ── Detail ──
    lines.append(f"\n## 详细结果")
    lines.append(f"| ID | 场景 | 结果 | 延迟 | Token(in/out) | 工具调用 | 原因 |")
    lines.append(f"|----|------|------|------|---------------|----------|------|")
    for r in all_results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(
            f"| {r.id} | {r.name} | {status} | {r.duration_ms:.0f}ms "
            f"| {r.input_tokens}/{r.output_tokens} | {r.tool_calls} "
            f"| {r.finish_reason}{' | '+r.error if r.error else ''} |"
        )

    # ── Failures ──
    failures = [r for r in all_results if not r.passed]
    if failures:
        lines.append(f"\n## 失败详情")
        for r in failures:
            lines.append(f"\n### {r.id} — {r.name}")
            lines.append(f"- **状态**: {r.finish_reason or 'N/A'}")
            lines.append(f"- **错误**: {r.error or '内容检查未通过'}")
            if r.content_preview:
                lines.append(f"- **回复预览**:\n  ```\n  {r.content_preview[:500]}\n  ```")

    # ── Metrics JSON ──
    metrics = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "scenario_pass_rate": round(rate / 100, 3),
        "total_scenarios": total,
        "passed": passed,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "avg_latency_ms": round(avg_ms, 0),
        "setup_ms": round(setup_ms, 0),
        "total_ms": round(total_ms, 0),
        "group_breakdown": {
            gid: {
                "total": len(g),
                "passed": sum(1 for r in g if r.passed),
            }
            for gid, g in sorted(groups.items())
        },
    }

    return "\n".join(lines), metrics


# ============================================================================
# Main
# ============================================================================

async def main() -> int:
    api_key = os.environ.get("MXWBOT_API_KEY", "")
    if not api_key:
        print("ERROR: MXWBOT_API_KEY 环境变量未设置")
        print("\n用法: $env:MXWBOT_API_KEY='sk-xxx'; .venv/Scripts/python.exe tests/e2e/run_e2e.py")
        return 1

    model = os.environ.get("MXWBOT_MODEL", "deepseek-v4-flash")
    base_url = os.environ.get("MXWBOT_BASE_URL", "https://api.deepseek.com")

    print("=" * 72)
    print("  MXWbot E2E 沙箱测试")
    print(f"  Model: {model}  |  Base URL: {base_url}")
    print("=" * 72)

    runner = E2ERunner(api_key=api_key, model=model, base_url=base_url)

    # Setup
    print("\n[1/8] 初始化 SystemManager + bootstrap ...")
    setup_ms = await runner.setup()
    print(f"       setup 完成 ({setup_ms:.0f} ms)")

    all_results: list[ScenarioResult] = []
    t_start = time.monotonic()

    groups = [
        ("A", "基础对话", run_group_a),
        ("B", "工具调用", run_group_b),
        ("C", "批量确认", run_group_c),
        ("D", "命令与会话", run_group_d),
        ("E", "记忆系统", run_group_e),
        ("F", "SubAgent", run_group_f),
    ]

    for i, (gid, gname, gfunc) in enumerate(groups, start=2):
        print(f"\n[{i}/8] {gid}组 — {gname} ...")
        group_results = await gfunc(runner)
        for r in group_results:
            status = "PASS" if r.passed else "FAIL"
            print(f"       {r.id} {r.name}: {status} "
                  f"({r.duration_ms:.0f}ms, {r.input_tokens}/{r.output_tokens} tok, "
                  f"tools={r.tool_calls})")
        all_results.extend(group_results)

    total_ms = (time.monotonic() - t_start) * 1000

    # Teardown
    print(f"\n[8/8] 清理 ...")
    await runner.teardown()

    # Report
    report, metrics = generate_report(all_results, setup_ms, total_ms)

    # Write to file
    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # Print summary
    passed = sum(1 for r in all_results if r.passed)
    total = len(all_results)
    print(f"\n{'=' * 72}")
    print(f"  测试完成: {passed}/{total} 通过 ({passed/total*100:.1f}%)")
    print(f"  总耗时: {total_ms/1000:.1f}s")
    print(f"  报告: tests/e2e/results/report.md")
    print(f"  JSON: tests/e2e/results/metrics.json")
    print(f"{'=' * 72}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
