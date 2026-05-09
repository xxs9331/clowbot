from __future__ import annotations

from ..state import ClawBotState


def route_after_commander(state: ClawBotState) -> str:
    if str(state.get("command_kind") or "").strip():
        return "local_view"
    return "fast_rule"

