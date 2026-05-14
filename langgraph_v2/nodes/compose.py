from __future__ import annotations

from utils.flow_log import log_flow_event
from utils.llm_reply_unescape import unescape_llm_visible_newlines
from utils.node_log import log_node_entry

from ..state import ClawBotState


async def compose(state: ClawBotState) -> ClawBotState:
    log_node_entry(state, "compose")
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
        from handlers.dispatcher import DispatcherMixin  # 延迟导入，避免 langgraph dev 仅装 CLI 时拉取 wechat/aiohttp

        reply = decision_reply or "收到。"
        reply = DispatcherMixin._sanitize_vague_none_reply(reply, tool, structured_trace=None)
        reply = unescape_llm_visible_newlines(reply)
        return {"reply": reply, "wx_out": [reply]}
    if decision_reply:
        dr = unescape_llm_visible_newlines(decision_reply)
        return {"reply": dr, "wx_out": [dr]}
    fallback = unescape_llm_visible_newlines("处理完成。")
    return {"reply": fallback, "wx_out": [fallback]}
