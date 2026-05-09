from __future__ import annotations

from handlers.dispatcher import DispatcherMixin

from ..state import ClawBotState


async def compose(state: ClawBotState) -> ClawBotState:
    decision = state.get("decision") or {}
    tool = str(decision.get("tool") or "none")
    decision_reply = str(decision.get("reply") or "").strip()
    tool_result = str(state.get("tool_result") or "").strip()
    local_reply = str(state.get("reply") or "").strip()
    if local_reply:
        return {"reply": local_reply, "wx_out": [local_reply]}
    if tool_result:
        return {"reply": tool_result, "wx_out": [tool_result]}
    if tool == "none":
        reply = decision_reply or "我没看懂这条要怎么记。要我把它当作生活记录写进今日日志吗？"
        reply = DispatcherMixin._sanitize_vague_none_reply(reply, tool, structured_trace=None)
        return {"reply": reply, "wx_out": [reply]}
    if decision_reply:
        return {"reply": decision_reply, "wx_out": [decision_reply]}
    fallback = "处理完成。"
    return {"reply": fallback, "wx_out": [fallback]}

