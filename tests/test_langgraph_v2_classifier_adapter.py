from __future__ import annotations

import asyncio

from langgraph_v2.adapters.classifier_adapter import FlashIntentClassifier


def _run(coro):
    return asyncio.run(coro)


class _StubACP:
    def __init__(self, reply: str):
        self._reply = reply
        self.model = "stub-model"
        self._created = 0

    async def create_session(self, model=None):
        _ = model
        self._created += 1
        return f"sid-{self._created}"

    async def prompt(self, sid, msg, *, trace_tag="x"):
        _ = sid, msg, trace_tag
        return self._reply, ""


def test_classifier_prefers_intent_llm_output():
    acp = _StubACP('{"intent":"todo_add","slots":{"text":"喝水"},"confidence":0.91}')
    cls = FlashIntentClassifier(acp=acp, model=None, timeout=2)
    out = _run(cls.classify(user_id="u1", text="添加待办喝水", queue_snapshot=[]))
    assert out["intent"] == "todo_add"
    assert out["slots"]["text"] == "喝水"


def test_classifier_fallback_maps_rule_todo_and_remind():
    acp = _StubACP("not-json")
    cls = FlashIntentClassifier(acp=acp, model=None, timeout=2)
    out_todo = _run(cls.classify(user_id="u1", text="加个待办 买牛奶", queue_snapshot=[]))
    assert out_todo["intent"] == "todo_add"
    out_remind = _run(cls.classify(user_id="u1", text="提醒我 7:30 开会", queue_snapshot=[]))
    assert out_remind["intent"] == "remind_add"
