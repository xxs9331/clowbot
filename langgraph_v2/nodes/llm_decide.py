from __future__ import annotations

from utils.flow_log import log_flow_event
from utils.node_log import log_node_entry

from ..state import ClawBotState


async def llm_decide(state: ClawBotState, deps) -> ClawBotState:
    log_node_entry(state, "llm_decide")
    if state.get("decision"):
        return {}
    d = await deps.llm.structured_decide(
        user_id=str(state.get("from_user") or ""),
        text=str(state.get("text") or ""),
        queue_snapshot=list(state.get("queue_snapshot") or []),
        intent_hint=dict(state.get("intent_hint") or {}),
        msg_trace=str(state.get("msg_trace") or ""),
    )
    log_flow_event(
        stage="graph",
        route="llm_decide_output",
        from_user=str(state.get("from_user") or ""),
        extra={
            "msg_trace": state.get("msg_trace"),
            "tool": d.tool,
            "reply_len": len(str(d.reply or "")),
        },
    )
    return {"decision": {"tool": d.tool, "payload": d.payload, "reply": d.reply}}
