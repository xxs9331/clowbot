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
        self._timeout_sec: float = 45.0

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

    @staticmethod
    def _looks_like_prompt_leak(reasoning: str) -> bool:
        """识别明显的提示词泄漏/跑偏内容：这种场景重建 session 再试一次。"""
        r = (reasoning or "").strip()
        if not r:
            return True
        markers = (
            "[MEMORY]",
            "Behavior_Instructions",
            "Phase 0 - Intent Gate",
            "No Status Updates",
            "只返回一个 JSON 对象",
            "JSON Schema",
            "请提供具体任务",
            "session setup",
        )
        if any(m in r for m in markers):
            return True
        return len(r) > 6000

    async def _ensure_session(self) -> str:
        sid = (self._session_id or "").strip()
        if sid:
            return sid
        sid = await self._acp.create_session(self._model)
        self._session_id = sid
        return sid

    async def describe_image(self, *, image_base64: str, image_mime: str) -> str:
        try:
            raw = base64.b64decode(image_base64 or "")
        except Exception:
            raw = b""
        if not raw:
            return "收到图片"
        mime = (image_mime or "").strip() or "image/jpeg"
        sid = await self._ensure_session()
        desc, reasoning = await self._acp.prompt_with_image(
            sid,
            "用简洁的中文描述这张图片的内容，只描述可见内容，不要推理。",
            raw,
            mime,
            trace_tag="vision_describe_graph",
            log_model=self._model,
            timeout_sec=self._timeout_sec,
        )
        if desc.strip() or not self._looks_like_prompt_leak(reasoning):
            out = self._pick_vision_desc(desc, reasoning)
            return out.strip() or "收到图片"

        # 首次超时/跑偏后，重建视觉会话再试一次，避免坏上下文持续污染。
        self._session_id = await self._acp.create_session(self._model)
        desc2, reasoning2 = await self._acp.prompt_with_image(
            self._session_id,
            "用简洁的中文描述这张图片的内容，只描述可见内容，不要推理。",
            raw,
            mime,
            trace_tag="vision_describe_graph_retry",
            log_model=self._model,
            timeout_sec=self._timeout_sec,
        )
        out2 = self._pick_vision_desc(desc2, reasoning2)
        return out2.strip() or "收到图片"
