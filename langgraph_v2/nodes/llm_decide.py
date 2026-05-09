from __future__ import annotations

from ..state import ClawBotState


async def llm_decide(state: ClawBotState, deps) -> ClawBotState:
    if state.get("decision"):
        return {}
    d = await deps.llm.structured_decide(
        user_id=str(state.get("from_user") or ""),
        text=str(state.get("text") or ""),
        queue_snapshot=list(state.get("queue_snapshot") or []),
        intent_hint=dict(state.get("intent_hint") or {}),
    )
    return {"decision": {"tool": d.tool, "payload": d.payload, "reply": d.reply}}
