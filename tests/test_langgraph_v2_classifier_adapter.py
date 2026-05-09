from __future__ import annotations

import asyncio

from langgraph_v2.adapters.classifier_adapter import RuleIntentClassifier


def _run(coro):
    return asyncio.run(coro)


def test_classifier_maps_rule_todo_and_remind():
    cls = RuleIntentClassifier()
    out_todo = _run(cls.classify(user_id="u1", text="加个待办 买牛奶", queue_snapshot=[]))
    assert out_todo["intent"] == "todo_add"
    out_remind = _run(cls.classify(user_id="u1", text="提醒我 7:30 开会", queue_snapshot=[]))
    assert out_remind["intent"] == "remind_add"


def test_classifier_maps_rule_todo_status_intents():
    cls = RuleIntentClassifier()
    out_done = _run(cls.classify(user_id="u1", text="做完了", queue_snapshot=[]))
    out_next = _run(cls.classify(user_id="u1", text="接下来做什么", queue_snapshot=[]))
    out_not_done = _run(cls.classify(user_id="u1", text="还没做", queue_snapshot=[]))
    assert out_done["intent"] == "todo_done"
    assert out_next["intent"] == "todo_next"
    assert out_not_done["intent"] == "todo_not_done"
