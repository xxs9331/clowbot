from __future__ import annotations

from typing import Any

from utils.intent import (
    INTENT_NONE,
    INTENT_REMIND,
    INTENT_TODO,
    INTENT_TODO_DONE,
    INTENT_TODO_NEXT,
    INTENT_TODO_NOT_DONE,
    detect_intent,
)


class RuleIntentClassifier:
    """规则意图分类器：仅 detect_intent，零 LLM 调用。"""

    def __init__(self, **_: Any):
        pass

    @staticmethod
    def _map_rule_intent(text: str) -> dict[str, Any]:
        intent, data = detect_intent(text)
        if intent == INTENT_NONE:
            return {}
        if intent == INTENT_TODO:
            return {"intent": "todo_add", "slots": {"text": data}, "confidence": 1.0}
        if intent == INTENT_REMIND:
            hhmm, _, remind_text = data.partition("：")
            remind_text = remind_text.strip() or data
            return {
                "intent": "remind_add",
                "slots": {"hhmm": hhmm.strip(), "text": remind_text},
                "confidence": 1.0,
            }
        if intent == INTENT_TODO_DONE:
            return {"intent": "todo_done", "slots": {}, "confidence": 1.0}
        if intent == INTENT_TODO_NEXT:
            return {"intent": "todo_next", "slots": {}, "confidence": 1.0}
        if intent == INTENT_TODO_NOT_DONE:
            return {"intent": "todo_not_done", "slots": {}, "confidence": 1.0}
        return {}

    async def classify(
        self, *, user_id: str, text: str, queue_snapshot: list[str]
    ) -> dict[str, Any]:
        _ = user_id, queue_snapshot
        return self._map_rule_intent(text)


# 兼容旧命名
FlashIntentClassifier = RuleIntentClassifier
