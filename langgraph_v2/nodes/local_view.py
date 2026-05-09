from __future__ import annotations

from ..state import ClawBotState


async def local_view(state: ClawBotState, deps) -> ClawBotState:
    cmd = str(state.get("command_kind") or "")
    if cmd in ("待办", "提醒", "记录", "时间轴"):
        view_map = {
            "待办": "todo",
            "提醒": "remind",
            "记录": "record",
            "时间轴": "timeline",
        }
        body = await deps.vault.read_view(kind=view_map[cmd])
        msg = str(body or "").strip()
        return {"handled": True, "reply": msg, "tool_result": msg}
    return {"handled": False}

