from __future__ import annotations

from typing import Any, TypedDict


class ClawBotState(TypedDict, total=False):
    # Input
    text: str
    image_base64: str
    image_mime: str
    from_user: str
    context_token: str
    agent_mode: bool

    # Intermediate
    msg_trace: str
    command_kind: str
    intent_hint: dict[str, Any]
    queue_snapshot: list[str]
    decision: dict[str, Any]
    handled: bool
    tool_result: str
    error: str

    # Output
    reply: str
    wx_out: list[str]
