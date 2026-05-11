"""OpenCode ACP 多模态图片描述（LangGraph describe_img 节点）。"""

from __future__ import annotations

import base64
import re

from acp.opencode_client import OpenCodeACP


class ACPImageDescriber:
    """每 Handler 实例一个；懒创建 vision session，与旧 ImageMixin 描述语义对齐。"""

    def __init__(self, acp: OpenCodeACP, multimodal_model: str) -> None:
        self._acp = acp
        self._model = (multimodal_model or "").strip() or "opencode-go/mimo-v2-omni"
        self._session_id: str | None = None

    @staticmethod
    def _pick_vision_desc(desc: str, reasoning: str) -> str:
        d = (desc or "").strip()
        if d:
            return d
        r = (reasoning or "").strip()
        if not r:
            return "无法识别图片内容"
        r = re.sub(r"^\s*我们被要求[^。\n]*[。\n]\s*", "", r, count=1).strip()
        return r or "无法识别图片内容"

    async def describe_image(self, *, image_base64: str, image_mime: str) -> str:
        try:
            raw = base64.b64decode(image_base64 or "")
        except Exception:
            raw = b""
        if not raw:
            return "收到图片"
        if not self._session_id:
            self._session_id = await self._acp.create_session(self._model)
        mime = (image_mime or "").strip() or "image/jpeg"
        desc, reasoning = await self._acp.prompt_with_image(
            self._session_id,
            "用简洁的中文描述这张图片的内容，只描述可见内容，不要推理。",
            raw,
            mime,
            trace_tag="vision_describe_graph",
            log_model=self._model,
        )
        out = self._pick_vision_desc(desc, reasoning)
        return out.strip() or "收到图片"
