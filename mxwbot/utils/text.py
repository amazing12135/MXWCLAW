"""文本处理工具函数。

- Think 标签清洗 (DeepSeek 风格 <think> 块)
- Token 估算 (tiktoken)
- 消息列表裁剪
- Assistant 回复清洗 (防止模型复读内部元数据)
"""

from __future__ import annotations

import re
from typing import Any

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Assistant replay artifacts — 模型可能从历史中学会复读的内部标记
_MESSAGE_TIME_PREFIX_RE = re.compile(r"^\[Message Time: [^\]]+\]\n?")
_LOCAL_IMAGE_BREADCRUMB_RE = re.compile(r"^\[image: (?:/|~)[^\]]+\]\s*$")
_TOOL_CALL_ECHO_RE = re.compile(r"^\s*(?:generate_image|message)\([^)]*\)\s*$")


def clean_think_tags(text: str) -> str:
    """移除 ``<think>...</think>`` 块。

    Args:
        text: 原始文本。

    Returns:
        清洗后的文本。
    """
    return _THINK_RE.sub("", text).strip()


def clean_assistant_replay_text(content: str) -> str:
    """移除 assistant 回复中的内部元数据标记，防止模型学会复读。

    处理三种模式：
      - ``[Message Time: ...]`` 时间戳前缀
      - ``[image: /path]`` 本地图片占位符行
      - ``generate_image(...)`` / ``message(...)`` 工具回显行

    这些字符串在运行时有用，但出现在 assistant 示例中会成为
    模型的负样本——教会模型在回复中复读这些标记。

    Args:
        content: assistant 回复的原始内容。

    Returns:
        清洗后的内容。
    """
    content = _MESSAGE_TIME_PREFIX_RE.sub("", content, count=1)
    lines = [
        line
        for line in content.splitlines()
        if not _LOCAL_IMAGE_BREADCRUMB_RE.match(line)
        and not _TOOL_CALL_ECHO_RE.match(line)
    ]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Token estimation (tiktoken)
# ---------------------------------------------------------------------------

# Lazy import so tiktoken is not required until actually used
_encoding_cache: dict[str, Any] = {}

# Fallback: ~4 chars per token for English text
_CHARS_PER_TOKEN_FALLBACK = 4


def estimate_tokens(text: str, model: str = "gpt-4") -> int:
    """Estimate the number of tokens in *text* for the given model.

    Uses tiktoken when available; falls back to a rough character-count
    heuristic otherwise.
    """
    if not text:
        return 0

    try:
        import tiktoken

        if model not in _encoding_cache:
            try:
                _encoding_cache[model] = tiktoken.encoding_for_model(model)
            except KeyError:
                _encoding_cache[model] = tiktoken.get_encoding("cl100k_base")
        enc = _encoding_cache[model]
        return len(enc.encode(text))
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback heuristic
    # Chinese / CJK chars are typically 1-2 tokens each in most tokenizers
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff")
    ascii_chars = len(text) - cjk
    return cjk + max(1, ascii_chars // _CHARS_PER_TOKEN_FALLBACK)


def truncate_messages(
    messages: list[dict[str, str]],
    max_tokens: int,
    model: str = "gpt-4",
) -> list[dict[str, str]]:
    """Keep the most recent messages that fit within *max_tokens*.

    Always retains the first message (typically the system prompt) and
    removes older messages from the middle when truncating.
    """
    if not messages:
        return []

    # Always keep system message (index 0)
    system_msg = messages[0]
    remaining = messages[1:]

    # Estimate total
    total = sum(estimate_tokens(m.get("content", ""), model) for m in messages)
    if total <= max_tokens:
        return list(messages)

    # Drop oldest non-system messages until we fit
    budget = max_tokens - estimate_tokens(system_msg.get("content", ""), model)
    kept: list[dict[str, str]] = []
    for msg in reversed(remaining):
        t = estimate_tokens(msg.get("content", ""), model)
        if t <= budget:
            kept.insert(0, msg)
            budget -= t
        else:
            break

    result = [system_msg] + kept
    # Never return empty — at minimum return system message
    return result if len(result) >= 1 else [system_msg]
