"""intent_bridge 单测：intent → decision 映射，模板泛词应被拒绝。"""

from __future__ import annotations

import pytest

from utils.intent_bridge import intent_to_decision
from utils.tool_names import (
    TOOL_RECORD_ADD,
    TOOL_REMIND_ADD,
    TOOL_TODO_ABANDON_CURRENT,
    TOOL_TODO_DONE_CURRENT,
    TOOL_TODO_MERGE_NEW_ITEMS,
    TOOL_TODO_NEXT,
    TOOL_TODO_NOT_DONE,
    TOOL_TODO_SKIP_CURRENT,
)


def test_none_input_returns_none():
    assert intent_to_decision(None) is None
    assert intent_to_decision({}) is None


@pytest.mark.parametrize(
    "intent, expected_tool",
    [
        ("todo_done", TOOL_TODO_DONE_CURRENT),
        ("todo_next", TOOL_TODO_NEXT),
        ("todo_not_done", TOOL_TODO_NOT_DONE),
        ("todo_skip", TOOL_TODO_SKIP_CURRENT),
        ("todo_abandon", TOOL_TODO_ABANDON_CURRENT),
    ],
)
def test_simple_todo_intents(intent, expected_tool):
    d = intent_to_decision({"intent": intent, "slots": {}, "confidence": 0.9})
    assert d == {"tool": expected_tool, "payload": {}, "reply": ""}


def test_todo_add_with_template_name():
    d = intent_to_decision(
        {"intent": "todo_add", "slots": {"template_name": "下山流程模板"}, "confidence": 0.9}
    )
    assert d == {
        "tool": TOOL_TODO_MERGE_NEW_ITEMS,
        "payload": {"tasks": ["下山流程模板"]},
        "reply": "",
    }


@pytest.mark.parametrize("name", ["模板", "待办模板", "流程模板", "模版", "待办模版"])
def test_todo_add_rejects_generic_template_names(name):
    d = intent_to_decision(
        {"intent": "todo_add", "slots": {"template_name": name}, "confidence": 0.95}
    )
    assert d is None, f"应拒绝泛词模板名: {name}"


def test_todo_add_with_text_only():
    d = intent_to_decision(
        {"intent": "todo_add", "slots": {"text": "买牛奶"}, "confidence": 0.85}
    )
    assert d["tool"] == TOOL_TODO_MERGE_NEW_ITEMS
    assert d["payload"] == {"tasks": ["买牛奶"]}


def test_todo_add_empty_slots_returns_none():
    d = intent_to_decision({"intent": "todo_add", "slots": {}, "confidence": 0.9})
    assert d is None


def test_remind_add_full_slots():
    d = intent_to_decision(
        {
            "intent": "remind_add",
            "slots": {"text": "喝水", "hhmm": "21:30"},
            "confidence": 0.9,
        }
    )
    assert d == {
        "tool": TOOL_REMIND_ADD,
        "payload": {"text": "喝水", "hhmm": "21:30"},
        "reply": "",
    }


@pytest.mark.parametrize(
    "slots",
    [
        {"text": "喝水"},  # 缺时间
        {"hhmm": "21:30"},  # 缺内容
        {},
    ],
)
def test_remind_add_missing_slots_returns_none(slots):
    d = intent_to_decision({"intent": "remind_add", "slots": slots, "confidence": 0.9})
    assert d is None


def test_record_add_with_category():
    d = intent_to_decision(
        {
            "intent": "record_add",
            "slots": {"text": "体重 78kg", "category": "身体"},
            "confidence": 0.92,
        }
    )
    assert d["tool"] == TOOL_RECORD_ADD
    assert d["payload"]["text"] == "体重 78kg"
    assert d["payload"]["category"] == "身体"


def test_record_add_without_category():
    d = intent_to_decision(
        {"intent": "record_add", "slots": {"text": "快递取了"}, "confidence": 0.85}
    )
    assert d["tool"] == TOOL_RECORD_ADD
    assert d["payload"] == {"text": "快递取了"}


def test_record_add_with_event_date():
    d = intent_to_decision(
        {
            "intent": "record_add",
            "slots": {
                "text": "她也改签了",
                "category": "事务",
                "event_date": "2026-05-02",
            },
            "confidence": 0.88,
        }
    )
    assert d["tool"] == TOOL_RECORD_ADD
    assert d["payload"]["text"] == "她也改签了"
    assert d["payload"]["category"] == "事务"
    assert d["payload"]["event_date"] == "2026-05-02"


def test_record_add_with_event_hhmm_slot():
    d = intent_to_decision(
        {
            "intent": "record_add",
            "slots": {"text": "吃了弥宁", "category": "身体", "event_hhmm": "15:00"},
            "confidence": 0.9,
        }
    )
    assert d["tool"] == TOOL_RECORD_ADD
    assert d["payload"]["event_hhmm"] == "15:00"


def test_record_add_slots_hhmm_maps_to_event_hhmm():
    d = intent_to_decision(
        {
            "intent": "record_add",
            "slots": {"text": "午饭", "hhmm": "12:30"},
            "confidence": 0.9,
        }
    )
    assert d["tool"] == TOOL_RECORD_ADD
    assert d["payload"]["event_hhmm"] == "12:30"
    assert "hhmm" not in d["payload"]


def test_query_intents_yield_to_unified():
    assert intent_to_decision({"intent": "query_todo", "slots": {}, "confidence": 0.9}) is None
    assert intent_to_decision({"intent": "query_remind", "slots": {}, "confidence": 0.9}) is None


def test_none_intent_returns_none():
    assert intent_to_decision({"intent": "none", "slots": {}, "confidence": 0.3}) is None


def test_todo_reorder_yields_to_unified():
    # reorder 列表小模型给不全，让 unified 处理
    assert intent_to_decision({"intent": "todo_reorder", "slots": {}, "confidence": 0.9}) is None


def test_unknown_intent_returns_none():
    assert intent_to_decision({"intent": "totally_unknown", "slots": {}, "confidence": 0.99}) is None


def test_slots_non_dict_treated_as_empty():
    d = intent_to_decision({"intent": "todo_done", "slots": "not_a_dict", "confidence": 0.9})
    assert d is not None and d["tool"] == TOOL_TODO_DONE_CURRENT
