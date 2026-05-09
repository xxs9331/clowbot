from __future__ import annotations

from ..services import DomainServices
from ..state import ClawBotState


async def execute(state: ClawBotState, services: DomainServices) -> ClawBotState:
    decision = state.get("decision") or {}
    tool = str(decision.get("tool") or "none")
    payload = decision.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    handled, out = await services.execute(
        user_id=str(state.get("from_user") or ""),
        tool=tool,
        payload=payload,
    )
    return {"handled": handled, "tool_result": str(out or "")}

