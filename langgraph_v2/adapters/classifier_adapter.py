from __future__ import annotations

from typing import Any

from utils.intent import INTENT_REMIND, INTENT_TODO, detect_intent
from utils.intent_llm import classify_intent


class FlashIntentClassifier:
    """对齐现有 intent_llm 的分类适配器，失败时回退规则意图。"""

    def __init__(self, *, acp: Any, model: str | None = None, timeout: float = 5.0):
        self._acp = acp
        self._model = model
        self._timeout = float(timeout)

    @staticmethod
    def _queue_state(queue_snapshot: list[str]) -> dict[str, Any]:
        tasks = [str(x).strip() for x in (queue_snapshot or []) if str(x).strip()]
        return {"active": bool(tasks), "tasks": tasks}

    @staticmethod
    def _map_rule_intent(text: str) -> dict[str, Any]:
        intent, data = detect_intent(text)
        if intent == INTENT_TODO:
            return {"intent": "todo_add", "slots": {"text": data}, "confidence": 0.6}
        if intent == INTENT_REMIND:
            hhmm, _, remind_text = data.partition("：")
            remind_text = remind_text.strip() or data
            return {
                "intent": "remind_add",
                "slots": {"hhmm": hhmm.strip(), "text": remind_text},
                "confidence": 0.6,
            }
        return {}

    async def classify(
        self, *, user_id: str, text: str, queue_snapshot: list[str]
    ) -> dict[str, Any]:
        queue_state = self._queue_state(queue_snapshot)
        out = await classify_intent(
            self._acp,
            model=self._model,
            text=text,
            queue_state=queue_state,
            timeout=self._timeout,
            from_user=user_id,
        )
        if isinstance(out, dict):
            return out
        return self._map_rule_intent(text)
