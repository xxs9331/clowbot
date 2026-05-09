from __future__ import annotations

from ..state import ClawBotState


async def pre_intent(state: ClawBotState, deps) -> ClawBotState:
    if deps.classifier is None:
        return {}
    text = str(state.get("text") or "").strip()
    if not text:
        return {}
    hint = await deps.classifier.classify(
        user_id=str(state.get("from_user") or ""),
        text=text,
        queue_snapshot=list(state.get("queue_snapshot") or []),
    )
    return {"intent_hint": hint if isinstance(hint, dict) else {}}

