from __future__ import annotations

import uuid

from utils.node_log import log_node_entry

from ..state import ClawBotState


async def normalize(state: ClawBotState) -> ClawBotState:
    log_node_entry(state, "normalize")
    text = str(state.get("text") or "").strip()
    cmd = ""
    if text.startswith("/"):
        cmd = text[1:].strip()
    return {
        "text": text,
        "command_kind": cmd,
        "request_id": str(state.get("request_id") or ""),
        "msg_trace": str(state.get("msg_trace") or uuid.uuid4().hex[:12]),
        "handled": bool(state.get("handled", False)),
        "queue_snapshot": list(state.get("queue_snapshot") or []),
        "wx_out": list(state.get("wx_out") or []),
        "tool_result": str(state.get("tool_result") or ""),
        "error": str(state.get("error") or ""),
        "agent_mode": bool(state.get("agent_mode", False)),
        "image_base64": str(state.get("image_base64") or "").strip(),
        "image_mime": str(state.get("image_mime") or "").strip(),
        "intent_hint": dict(state.get("intent_hint") or {}),
    }
