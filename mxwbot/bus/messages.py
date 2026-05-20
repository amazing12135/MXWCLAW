"""Message bus data structures.

Defines the three core message types that flow through the system:

  InboundMessage  – from Channel into the Bus / LoopPool
  OutboundMessage – from Loop / Runner out to Channel
  StreamDelta     – incremental LLM output pushed to streaming channels
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class InboundMessage:
    """A message that arrived from an external channel.

    Attributes:
        idempotency_key: ``f"{channel}:{chat_id}:{platform_msg_id}"``,
            used by SessionManager to detect and drop duplicates.
        ref_request_id: When msg_type is ``confirmation_response``, this
            links back to the original ``OutboundMessage.request_id``.
    """

    channel: str
    chat_id: str
    content: str
    id: str = field(default_factory=_new_id)
    msg_type: str = "message"  # "message" | "confirmation_response"
    idempotency_key: str = ""
    ref_request_id: str | None = None
    timestamp: datetime = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            self.idempotency_key = f"{self.channel}:{self.chat_id}:{self.id}"


@dataclass
class OutboundMessage:
    """A message that should be delivered to an external channel.

    Attributes:
        msg_type: ``message``, ``confirmation_request``, or ``stream``.
        reply_to: The id of the InboundMessage this is responding to.
            This is the sole identity link — OutboundMessage has no
            independent ``id`` because it always exists in relation to
            the inbound message it answers.
        request_id: For confirmation_request — used to match the response.
        risk_level: For confirmation_request — ``readonly``, ``write``, or
            ``shell``.
        timeout_seconds: How long to wait for a confirmation response before
            treating it as denied.
        fallback_prompt: Text shown on channels that cannot render interactive
            buttons.
        media: File paths or URLs to send as attachments.
            Channel 在 ``_on_before_send`` 钩子中处理。支持本地路径、
            ``file://`` URI 和 ``http(s)://`` URL。
    """

    channel: str
    chat_id: str
    content: str
    msg_type: str = "message"  # "message" | "confirmation_request" | "stream"
    reply_to: str | None = None
    request_id: str | None = None
    risk_level: str | None = None
    timeout_seconds: int | None = None
    fallback_prompt: str | None = None
    media: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamDelta:
    """A single incremental chunk of a streaming LLM response.

    Attributes:
        seq: Monotonic sequence number — receivers use it to detect gaps.
        is_end: True when the stream has finished normally.
        error: Non-null when the stream terminated with an error.
            The ``delta`` field should be empty in this case.
    """

    stream_id: str
    channel: str
    chat_id: str
    delta: str = ""
    seq: int = 0
    is_end: bool = False
    error: str | None = None
    timestamp: datetime = field(default_factory=_utc_now)
