from __future__ import annotations

from ..state import ClawBotState


async def describe_img(state: ClawBotState, deps) -> ClawBotState:
    img = str(state.get("image_base64") or "").strip()
    if not img:
        return {}
    if deps.image_llm is None:
        return {"text": str(state.get("text") or "").strip() or "收到图片"}
    desc = await deps.image_llm.describe_image(
        image_base64=img,
        image_mime=str(state.get("image_mime") or "").strip(),
    )
    desc = str(desc or "").strip()
    if not desc:
        desc = "收到图片"
    caption = str(state.get("text") or "").strip()
    composed = f"[图片] {desc}"
    if caption:
        composed = f"{composed}\n附言：{caption}"
    return {"text": composed}

