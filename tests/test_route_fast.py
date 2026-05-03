"""快通道单测：仅保留 meta clarify，其它句式应返回 None。"""

from __future__ import annotations

from utils.route_fast import build_fast_unified_decision
from utils.tool_names import TOOL_DECISION_NONE, TOOL_TODO_DONE_CURRENT


def _empty_queues():
    return {}


def _active_queue():
    return {"u1": {"tasks": ["写周报"], "idx": 0}}


def test_meta_clarify_when_active_queue():
    # 注意：触发 meta clarify 需要 (1) 有活跃队列 (2) 文本含 SKILL/skill/技能 等元词
    # 但不能再触发 INTENT_TODO（否则会被 todo 分支抢答），所以避免使用"待办"二字
    d = build_fast_unified_decision(
        "你这个怎么走 SKILL 的？", "u1", _active_queue()
    )
    assert d is not None
    assert d["tool"] == TOOL_DECISION_NONE
    assert "SKILL" in d["reply"] or "skill" in d["reply"]


def test_random_chat_returns_none():
    d = build_fast_unified_decision("今天天气真不错呀", "u1", _empty_queues())
    assert d is None


def test_todo_phrases_return_none():
    assert build_fast_unified_decision("做完了", "u1", _empty_queues()) is None
    assert build_fast_unified_decision("接下来做什么", "u1", _empty_queues()) is None
    assert build_fast_unified_decision("待办：买菜，做饭", "u1", _empty_queues()) is None


def test_meta_clarify_skipped_when_no_active_queue():
    # 同样避免 "待办" 字样让 todo 分支抢答；纯 meta 句在无队列时应返回 None
    d = build_fast_unified_decision("怎么走 SKILL？", "u1", _empty_queues())
    assert d is None


def test_done_shortcut_when_active_queue():
    d = build_fast_unified_decision("OK", "u1", _active_queue())
    assert d is not None
    assert d["tool"] == TOOL_TODO_DONE_CURRENT


def test_done_shortcut_skips_negated_zuo_wan():
    """「还没做完」含子串「做完」，不得误触 done_current。"""
    assert build_fast_unified_decision("还没做完，等会再做", "u1", _active_queue()) is None


def test_done_shortcut_still_matches_plain_zuo_wan():
    assert build_fast_unified_decision("刚做完", "u1", _active_queue()) is not None
