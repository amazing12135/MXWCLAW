"""Token budget helpers — pure functions for counting / ratio / truncation.

Uses ``tiktoken`` for accurate counting with a character-count fallback
for unsupported models.  Encoding lookup is LRU-cached at module level.
"""

from __future__ import annotations

import functools
from typing import Any


# ---------------------------------------------------------------------------
# Encoding lookup (cached)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _get_encoding(model: str):
    """Return a tiktoken Encoding for *model*, falling back to cl100k_base."""
    import tiktoken
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


# ---------------------------------------------------------------------------
# Per-string estimate
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str, model: str) -> int:
    """Token-count a plain string — tiktoken with heuristic fallback."""
    if not text:
        return 0
    try:
        enc = _get_encoding(model)
        return len(enc.encode(text))
    except Exception:
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff")
        return cjk + max(1, (len(text) - cjk) // 4)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def count_tokens(
    messages: list[dict[str, Any]],
    *,
    model: str = "gpt-4",
) -> int:
    """Return the estimated token count for a list of chat messages."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content, model)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += _estimate_tokens(block.get("text", ""), model)
        for tc in msg.get("tool_calls", []) or []:
            if isinstance(tc, dict):
                total += _estimate_tokens(
                    tc.get("function", {}).get("arguments", ""), model
                )
                total += 4  # overhead per tool call
    return total


def token_usage_ratio(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    model: str = "gpt-4",
) -> float:
    """Return ``count / max_tokens`` in [0, ∞)."""
    if max_tokens <= 0:
        return float("inf") if messages else 0.0
    return count_tokens(messages, model=model) / max_tokens


def truncate_messages(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    target_ratio: float = 0.8,
    model: str = "gpt-4",
) -> list[dict[str, Any]]:
    """Drop oldest non-system messages until count ≤ target_ratio * max_tokens.

    Always retains the first message (typically the system prompt).
    """
    if not messages:
        return []

    budget = int(max_tokens * target_ratio)
    current = count_tokens(messages, model=model)
    if current <= budget:
        return list(messages)

    first = messages[0]
    first_tokens = count_tokens([first], model=model)
    remaining_budget = budget - first_tokens
    if remaining_budget <= 0:
        return [first]

    kept: list[dict[str, Any]] = []
    for msg in reversed(messages[1:]):
        t = count_tokens([msg], model=model)
        if t <= remaining_budget:
            kept.insert(0, msg)
            remaining_budget -= t
        else:
            break

    return [first] + kept


# ---------------------------------------------------------------------------
# Smart truncation — alignment + orphan removal
# ---------------------------------------------------------------------------

def _find_user_turn_start(messages: list[dict[str, Any]]) -> int:
    """Return the index of the first ``role == "user"`` message.

    If none found, returns 0 (keep everything) — better than dropping all.
    """
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            return i
    return 0


def _remove_orphaned_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop leading tool-result messages that have no preceding tool_call.

    This prevents the LLM from seeing orphaned ``role: "tool"`` messages
    after truncation removed the assistant message that requested them.
    """
    # Build set of tool_call ids present
    tool_call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []) or []:
                if isinstance(tc, dict) and tc.get("id"):
                    tool_call_ids.add(tc["id"])

    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id not in tool_call_ids:
                continue  # orphaned
        result.append(msg)
    return result


def truncate_messages_smart(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    target_ratio: float = 0.8,
    model: str = "gpt-4",
) -> list[dict[str, Any]]:
    """Token-budget truncation with structural safety.

    Pipeline::

        messages
          → token truncation (keep system + newest that fit)
          → align to nearest user turn start
          → remove orphaned tool results without tool_call
          → re-align + re-check orphans after token cut

    Prevents two common context-corruption bugs:
      1. History starting mid-turn (assistant message without user context).
      2. Orphaned tool results whose tool_call was dropped.
    """
    if not messages:
        return []

    # 1. Token-budget truncation
    truncated = truncate_messages(messages, max_tokens=max_tokens, target_ratio=target_ratio, model=model)

    # 2. Preserve system message(s) at the head, then align to user turn
    system_head: list[dict[str, Any]] = []
    body_start = 0
    for m in truncated:
        if m.get("role") == "system":
            system_head.append(m)
            body_start += 1
        else:
            break

    body = truncated[body_start:]
    if body:
        turn_start = _find_user_turn_start(body)
        if turn_start > 0:
            body = body[turn_start:]

    truncated = system_head + body

    # 3. Remove orphaned tool results
    truncated = _remove_orphaned_tool_results(truncated)

    # 4. If token cut left us starting with assistant/tool (no system, no user),
    #    fall back to a wider search in the original list
    if truncated and truncated[0].get("role") not in ("user", "system"):
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                truncated = messages[i:i + len(truncated)]
                truncated = _remove_orphaned_tool_results(truncated)
                break

    return truncated
