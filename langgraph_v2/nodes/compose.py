from __future__ import annotations

from handlers.dispatcher import DispatcherMixin
from utils.flow_log import log_flow_event

from ..state import ClawBotState


async def compose(state: ClawBotState) -> ClawBotState:
    decision = state.get("decision") or {}
    if not decision or "tool" not in decision:
        log_flow_event(
            stage="graph",
            route="compose_decision_missing",
            user_text=str(state.get("text") or "")[:300],
            from_user=str(state.get("from_user") or ""),
            extra={
                "msg_trace": state.get("msg_trace"),
                "tool_result_preview": str(state.get("tool_result") or "")[:200],
                "reply_preview": str(state.get("reply") or "")[:200],
                "full_state_keys": sorted(list(state.keys())),
                "raw_decision_repr": repr(state.get("decision")),
            },
        )
    tool = str(decision.get("tool") or "none")
    decision_reply = str(decision.get("reply") or "").strip()
    tool_result = str(state.get("tool_result") or "").strip()
    local_reply = str(state.get("reply") or "").strip()
    if local_reply:
        return {"reply": local_reply, "wx_out": [local_reply]}
    if tool_result:
        return {"reply": tool_result, "wx_out": [tool_result]}
    if tool == "none":
        reply = decision_reply or "收到。"
        reply = DispatcherMixin._sanitize_vague_none_reply(reply, tool, structured_trace=None)
        return {"reply": reply, "wx_out": [reply]}
    if decision_reply:
        return {"reply": decision_reply, "wx_out": [decision_reply]}
    fallback = "处理完成。"
    return {"reply": fallback, "wx_out": [fallback]}
