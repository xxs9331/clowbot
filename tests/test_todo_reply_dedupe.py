from __future__ import annotations

from handlers.coaches.todo import TodoCoachMixin


def test_has_next_prompt_true_when_reply_has_next():
    s = "✅ 小说完成 (2/2)。下一组：上药 (1/5)，做完了吗？ 下一个：上药，做完了吗？"
    assert TodoCoachMixin._has_next_prompt(s) is True


def test_has_next_prompt_true_when_reply_has_downstream_prompt():
    s = "先做：上药，做完了吗？"
    assert TodoCoachMixin._has_next_prompt(s) is True


def test_has_next_prompt_false_for_plain_ack():
    s = "做得好，已完成。"
    assert TodoCoachMixin._has_next_prompt(s) is False

