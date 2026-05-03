"""路由记忆与意图短路保护单测（无 ACP）。"""

from __future__ import annotations

from handlers.base import Handler


def test_block_record_add_when_global_short_circuit_disabled():
    obj = {"intent": "record_add", "slots": {"text": "任意"}, "confidence": 0.99}
    assert Handler._block_intent_short_circuit(obj, "任意", {"record_high_conf_short_circuit": False}) is True


def test_block_record_add_short_message_when_short_circuit_enabled():
    obj = {"intent": "record_add", "slots": {"text": "短"}, "confidence": 0.99}
    assert (
        Handler._block_intent_short_circuit(obj, "今天吃了面", {"record_high_conf_short_circuit": True})
        is True
    )


def test_no_block_record_add_when_event_date_present():
    obj = {
        "intent": "record_add",
        "slots": {"text": "她也改签了", "event_date": "2026-05-02"},
        "confidence": 0.99,
    }
    assert (
        Handler._block_intent_short_circuit(obj, "她也改签了", {"record_high_conf_short_circuit": True})
        is False
    )


def test_no_block_non_record_intent():
    obj = {"intent": "todo_done", "slots": {}, "confidence": 0.99}
    assert Handler._block_intent_short_circuit(obj, "搞定", {"record_high_conf_short_circuit": False}) is False
