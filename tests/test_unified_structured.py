from __future__ import annotations

import asyncio
import json

from acp.opencode_client import OpenCodeACP
from handlers.dispatcher import (
    UNIFIED_LEGACY_RAW_FALLBACK_MAX_CHARS,
    DispatcherMixin,
)
from utils.tool_names import TOOL_DECISION_NONE, TOOL_TODO_DONE_CURRENT


def _run(coro):
    return asyncio.run(coro)


class _FakeACP:
    def __init__(self, *, structured=None, prompt_replies=None, raise_on_reply=False):
        self.structured = structured
        self.prompt_replies = list(prompt_replies or [])
        self.raise_on_reply = raise_on_reply
        self.prompt_calls = []

    async def prompt_structured(self, *args, **kwargs):
        return self.structured

    async def prompt(self, sid, msg, *, trace_tag="x"):
        self.prompt_calls.append(trace_tag)
        if self.raise_on_reply and trace_tag == "unified_reply":
            raise RuntimeError("reply boom")
        if self.prompt_replies:
            return self.prompt_replies.pop(0), ""
        return "", ""


class _FakeDispatcher(DispatcherMixin):
    def __init__(self, acp):
        self.acp = acp
        self.cfg = {"opencode": {"structured_retry_count": 2}}
        self.unified_session_id = "sid-u"
        self._pending_reorders = {}

    def _get_current_queue_task(self, _uid):
        return ""

    def _get_remaining_queue_tasks(self, _uid):
        return []

    @staticmethod
    def _extract_json_object(text: str) -> dict:
        try:
            return json.loads(text)
        except Exception:
            return {}


def test_prompt_structured_retry_until_valid():
    acp = OpenCodeACP()
    replies = iter(
        [
            ('{"tool":"none","payload":"bad"}', ""),
            ('{"tool":"none","payload":{}}', ""),
        ]
    )

    async def _stub_prompt(_sid, _msg, *, trace_tag="x"):
        return next(replies)

    acp.prompt = _stub_prompt  # type: ignore[method-assign]
    schema = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": ["none"]},
            "payload": {"type": "object"},
        },
        "required": ["tool", "payload"],
        "additionalProperties": False,
    }
    out = _run(
        acp.prompt_structured(
            "sid",
            "msg",
            json_schema=schema,
            retry_count=2,
            trace_tag="t",
        )
    )
    assert out == {"tool": "none", "payload": {}}


def test_unified_structured_success_then_generate_reply():
    acp = _FakeACP(
        structured={"tool": TOOL_DECISION_NONE, "payload": {}},
        prompt_replies=["好的，去睡吧"],
    )
    h = _FakeDispatcher(acp)
    out = _run(h._llm_unified_decide("u1", "好困"))
    assert out["tool"] == TOOL_DECISION_NONE
    assert out["payload"] == {}
    assert out["reply"] == "好的，去睡吧"
    assert "unified_reply" in acp.prompt_calls


def test_unified_structured_fail_fallback_to_text_json():
    acp = _FakeACP(
        structured=None,
        prompt_replies=['{"tool":"none","payload":{},"reply":"fallback回复"}'],
    )
    h = _FakeDispatcher(acp)
    out = _run(h._llm_unified_decide("u1", "好困"))
    assert out == {"tool": "none", "payload": {}, "reply": "fallback回复"}
    assert "unified_decide_fallback" in acp.prompt_calls


def test_unified_reply_failure_does_not_break_action():
    acp = _FakeACP(
        structured={"tool": TOOL_TODO_DONE_CURRENT, "payload": {}},
        raise_on_reply=True,
    )
    h = _FakeDispatcher(acp)
    out = _run(h._llm_unified_decide("u1", "做完了"))
    assert out["tool"] == TOOL_TODO_DONE_CURRENT
    assert out["payload"] == {}
    assert out["reply"] == ""


def test_unified_legacy_raw_body_fallback_when_unparseable_with_brace():
    """含 { 但无法解析成信封时，全文作 reply（上限 UNIFIED_LEGACY_RAW_FALLBACK_MAX_CHARS）。"""
    bad = "{没有tool字段，模型胡写一长段"
    acp = _FakeACP(structured=None, prompt_replies=[bad])
    h = _FakeDispatcher(acp)
    out = _run(h._llm_unified_decide("u1", "你好"))
    assert out["tool"] == TOOL_DECISION_NONE
    assert out["payload"] == {}
    assert out["reply"] == bad


def test_unified_legacy_raw_body_fallback_truncates():
    long_body = "{" + ("x" * (UNIFIED_LEGACY_RAW_FALLBACK_MAX_CHARS + 500))
    acp = _FakeACP(structured=None, prompt_replies=[long_body])
    h = _FakeDispatcher(acp)
    out = _run(h._llm_unified_decide("u1", "你好"))
    assert len(out["reply"]) == UNIFIED_LEGACY_RAW_FALLBACK_MAX_CHARS


def test_unified_legacy_no_raw_fallback_without_brace():
    acp = _FakeACP(structured=None, prompt_replies=["纯中文没有花括号"])
    h = _FakeDispatcher(acp)
    out = _run(h._llm_unified_decide("u1", "你好"))
    assert out["tool"] == TOOL_DECISION_NONE
    assert out["reply"] == ""
