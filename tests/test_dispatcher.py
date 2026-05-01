"""dispatcher 决策规整与 hook 调用路径单测（不需要真 LLM / 真微信）。"""

from __future__ import annotations

import asyncio

import pytest

from handlers.dispatcher import (
    _coalesce_unified_decision,
    register_tool_handler,
)
from utils.refresh_hooks import register_post_write_hook, run_post_write_hooks
from utils.tool_names import (
    TOOL_DECISION_NONE,
    TOOL_RECORD_ADD,
    TOOL_REMIND_ADD,
    TOOL_TODO_DONE_CURRENT,
    TOOL_TODO_MERGE_NEW_ITEMS,
)


def test_coalesce_legacy_alias_to_namespaced():
    tool, payload, reply = _coalesce_unified_decision(
        {"tool": "merge_new_items", "tasks": ["a", "b"], "reply": " hi "}
    )
    assert tool == TOOL_TODO_MERGE_NEW_ITEMS
    assert payload == {"tasks": ["a", "b"]}
    assert reply == "hi"


def test_coalesce_absorbs_top_level_reorder():
    tool, payload, _ = _coalesce_unified_decision(
        {"tool": "todo.reorder", "reorder": ["x", "y"]}
    )
    assert tool == "todo.reorder"
    assert payload == {"reorder": ["x", "y"]}


def test_coalesce_payload_already_dict_is_kept():
    tool, payload, _ = _coalesce_unified_decision(
        {"tool": TOOL_RECORD_ADD, "payload": {"text": "走了 5km", "category": "运动"}}
    )
    assert tool == TOOL_RECORD_ADD
    assert payload["text"] == "走了 5km"
    assert payload["category"] == "运动"


def test_coalesce_empty_decision_to_none():
    tool, payload, reply = _coalesce_unified_decision({})
    assert tool == TOOL_DECISION_NONE
    assert payload == {}
    assert reply == ""


def test_coalesce_case_insensitive_and_legacy_life_log():
    tool, _, _ = _coalesce_unified_decision({"tool": "Life_Log"})
    assert tool == TOOL_RECORD_ADD


def test_run_post_write_hooks_invokes_for_remind_add():
    seen: list[tuple] = []

    class FakeHandler:
        def notify_reminder_refresh(self):
            seen.append(("refresh",))

    run_post_write_hooks(FakeHandler(), TOOL_REMIND_ADD, {"text": "喝水"})
    assert seen == [("refresh",)]


def test_run_post_write_hooks_silent_for_unknown_tool():
    run_post_write_hooks(object(), "nothing.here", {})


def test_register_tool_handler_smoketest_async_call():
    """注册一个临时 tool 名 + 异步 handler，验证 dispatcher 表能 await 它（不依赖 wechat）。"""

    flag = {"called": False}

    async def fake_handler(_self, payload, _reply, _from, _ctx, _user_text):
        flag["called"] = True
        flag["payload"] = payload
        return True

    register_tool_handler("test.echo", fake_handler)

    from handlers.dispatcher import _TOOL_HANDLERS

    h = _TOOL_HANDLERS["test.echo"]
    handled = asyncio.run(h(object(), {"x": 1}, "", "u", "ctx", ""))
    assert handled is True
    assert flag["called"] and flag["payload"] == {"x": 1}


def test_post_write_hook_swallows_exception():
    def boom(_h, _p):
        raise RuntimeError("boom")

    register_post_write_hook("test.boom", boom)
    run_post_write_hooks(object(), "test.boom", {})


def test_done_current_alias_recognized():
    tool, _, _ = _coalesce_unified_decision({"tool": "done_current"})
    assert tool == TOOL_TODO_DONE_CURRENT


@pytest.mark.parametrize(
    "raw",
    [None, "", "  ", 0],
)
def test_coalesce_falsy_tool_to_none(raw):
    tool, _, _ = _coalesce_unified_decision({"tool": raw})
    assert tool == TOOL_DECISION_NONE
