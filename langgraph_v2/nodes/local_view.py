from __future__ import annotations

from utils.node_log import log_node_entry

from ..state import ClawBotState


async def local_view(state: ClawBotState, deps) -> ClawBotState:
    log_node_entry(state, "local_view")
    cmd = str(state.get("command_kind") or "")
    view_map = {
        "待办": "todo",
        "代办": "todo",
        "提醒": "remind",
        "记录": "record",
        "日志": "log",
        "简报": "brief",
        "找": "record_recent",
        "时间轴": "timeline",
    }
    cmd = {
        "查看待办": "待办",
        "看待办": "待办",
        "查看代办": "代办",
        "看代办": "代办",
        "查看提醒": "提醒",
        "看提醒": "提醒",
        "查看记录": "记录",
        "看记录": "记录",
        "查看日志": "日志",
        "看日志": "日志",
        "查看简报": "简报",
        "看简报": "简报",
    }.get(cmd, cmd)
    if cmd in view_map:
        body = await deps.vault.read_view(kind=view_map[cmd])
        msg = str(body or "").strip()
        return {"handled": True, "reply": msg, "tool_result": msg}
    return {"handled": False}
