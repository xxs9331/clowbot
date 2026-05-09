from __future__ import annotations

from ..state import ClawBotState


def route_after_image_router(state: ClawBotState) -> str:
    if str(state.get("image_base64") or "").strip():
        return "describe_img"
    return "pre_intent"

