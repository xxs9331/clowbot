"""intent_llm 单测：纯解析/规整，不真发 ACP。"""

from __future__ import annotations

import asyncio

import pytest

from utils.intent_llm import (
    INTENT_LABELS,
    _build_prompt,
    _extract_json,
    _normalize,
    classify_intent,
)


def test_extract_json_pure_object():
    obj = _extract_json('{"intent":"todo_done","slots":{},"confidence":0.9}')
    assert obj == {"intent": "todo_done", "slots": {}, "confidence": 0.9}


def test_extract_json_with_surrounding_text():
    s = "好的：\n```json\n{\"intent\":\"none\",\"slots\":{},\"confidence\":0.3}\n```"
    obj = _extract_json(s)
    assert obj is not None
    assert obj["intent"] == "none"


def test_extract_json_invalid_returns_none():
    assert _extract_json("not a json at all") is None
    assert _extract_json("") is None


def test_normalize_clamps_confidence_and_lowercases():
    out = _normalize({"intent": "TODO_DONE", "slots": {"x": "y"}, "confidence": 1.7})
    assert out == {"intent": "todo_done", "slots": {"x": "y"}, "confidence": 1.0}


def test_normalize_negative_confidence_to_zero():
    out = _normalize({"intent": "none", "slots": {}, "confidence": -0.5})
    assert out["confidence"] == 0.0


def test_normalize_unknown_intent_returns_none():
    assert _normalize({"intent": "wat", "slots": {}, "confidence": 0.9}) is None


def test_normalize_strips_empty_slots():
    out = _normalize(
        {"intent": "todo_add", "slots": {"a": "  ", "b": "x"}, "confidence": "0.4"}
    )
    assert out is not None
    assert out["slots"] == {"b": "x"}
    assert out["confidence"] == 0.4


def test_intent_labels_complete():
    expected = {
        "todo_add",
        "todo_done",
        "todo_next",
        "todo_not_done",
        "todo_skip",
        "todo_abandon",
        "todo_reorder",
        "remind_add",
        "record_add",
        "query_todo",
        "query_remind",
        "none",
    }
    assert set(INTENT_LABELS) == expected


def test_build_prompt_contains_user_message_and_examples():
    p = _build_prompt("叫我九点半喝水", queue_state={"active": False})
    assert "叫我九点半喝水" in p
    assert "intent" in p
    assert "template_name" in p
    assert "泛词" in p


# ─── classify_intent: 用 stub acp 验证三条主路径 ───


class _StubACP:
    def __init__(self, reply: str, model: str = "stub-model"):
        self._reply = reply
        self.model = model
        self._created = 0

    async def create_session(self, model=None):
        self._created += 1
        return f"sid-{self._created}"

    async def prompt(self, sid, msg, *, trace_tag="x"):
        return self._reply, ""


def _run(coro):
    return asyncio.run(coro)


def test_classify_intent_high_confidence_round_trip():
    acp = _StubACP('{"intent":"todo_add","slots":{"template_name":"上山模板"},"confidence":0.92}')
    out = _run(classify_intent(acp, model=None, text="添加上山模板待办"))
    assert out == {
        "intent": "todo_add",
        "slots": {"template_name": "上山模板"},
        "confidence": 0.92,
    }


def test_classify_intent_invalid_json_returns_none():
    acp = _StubACP("我不知道什么是 JSON")
    out = _run(classify_intent(acp, model=None, text="你好"))
    assert out is None


def test_classify_intent_session_reused():
    acp = _StubACP('{"intent":"none","slots":{},"confidence":0.3}')

    async def _case():
        await classify_intent(acp, model=None, text="一")
        await classify_intent(acp, model=None, text="二")

    _run(_case())
    assert acp._created == 1, "intent session 应在同一 acp 上复用"


def test_classify_intent_empty_text_returns_none():
    acp = _StubACP('{"intent":"none","slots":{},"confidence":0.3}')
    assert _run(classify_intent(acp, model=None, text="")) is None
    assert _run(classify_intent(acp, model=None, text="   ")) is None


def test_classify_intent_none_acp_returns_none():
    assert _run(classify_intent(None, model=None, text="hi")) is None


@pytest.mark.parametrize(
    "raw_reply, expect_intent",
    [
        ('  {"intent":"todo_done","slots":{},"confidence":0.9}  ', "todo_done"),
        (
            "前缀\n```\n{\"intent\":\"remind_add\",\"slots\":{\"text\":\"喝水\",\"hhmm\":\"21:30\"},\"confidence\":0.9}\n```",
            "remind_add",
        ),
    ],
)
def test_classify_intent_tolerates_dirty_output(raw_reply, expect_intent):
    acp = _StubACP(raw_reply)
    out = _run(classify_intent(acp, model=None, text="任意"))
    assert out is not None and out["intent"] == expect_intent
