from __future__ import annotations

from utils.intent import INTENT_NONE, INTENT_TODO, detect_intent


def test_detect_intent_quoted_ji_yi_xia_is_none():
    text = "我没看懂这条要怎么记。要我把它当作生活记录写进今日日记吗？回「记一下」即可。"
    intent, data = detect_intent(text)
    assert intent == INTENT_NONE
    assert data == ""


def test_detect_intent_command_style_ji_yi_xia_is_todo():
    intent, data = detect_intent("记一下：明天带身份证")
    assert intent == INTENT_TODO
    assert data == "明天带身份证"
