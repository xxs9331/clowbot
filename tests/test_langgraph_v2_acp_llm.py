from __future__ import annotations

import asyncio

from acp.opencode_client import OpenCodeACP
from langgraph_v2.adapters.acp_llm import ACPStructuredLLM, UnifiedDecideLLM


def _run(coro):
    return asyncio.run(coro)


class _FakeACP:
    def __init__(self):
        self.structured_calls: list[dict] = []
        self.prompt_calls: list[dict] = []
        self.structured_results: list[dict | None] = []
        self.prompt_results: list[str] = []

    async def prompt_structured(
        self,
        session_id: str,
        message: str,
        *,
        json_schema: dict,
        retry_count: int = 3,
        trace_tag: str = "",
    ):
        self.structured_calls.append(
            {
                "session_id": session_id,
                "message": message,
                "json_schema": json_schema,
                "retry_count": retry_count,
                "trace_tag": trace_tag,
            }
        )
        if self.structured_results:
            return self.structured_results.pop(0)
        return None

    async def prompt(self, session_id: str, message: str, *, trace_tag: str = "prompt"):
        self.prompt_calls.append(
            {"session_id": session_id, "message": message, "trace_tag": trace_tag}
        )
        if self.prompt_results:
            return self.prompt_results.pop(0), ""
        return "", ""


def test_acp_structured_llm_passthrough():
    acp = _FakeACP()
    acp.structured_results = [{"tool": "none", "payload": {}, "reply": "ok"}]
    llm = ACPStructuredLLM(acp, "sid-u")
    out = _run(
        llm.prompt_structured(
            prompt="hello", schema={"type": "object"}, retry=5
        )
    )
    assert out == {"tool": "none", "payload": {}, "reply": "ok"}
    assert len(acp.structured_calls) == 1
    c = acp.structured_calls[0]
    assert c["session_id"] == "sid-u"
    assert c["retry_count"] == 5
    assert c["json_schema"] == {"type": "object"}


def test_unified_decide_combined_success():
    acp = _FakeACP()
    acp.structured_results = [{"tool": "none", "payload": {}, "reply": "直接回复"}]
    llm = UnifiedDecideLLM(acp=acp, session_id="sid-u", retry_count=2)
    out = _run(llm.structured_decide(user_id="u1", text="你好", queue_snapshot=[]))
    assert out.tool == "none"
    assert out.payload == {}
    assert out.reply == "直接回复"
    assert len(acp.structured_calls) == 1
    assert not acp.prompt_calls


def test_unified_decide_degrade_decision_only_plus_reply():
    acp = _FakeACP()
    acp.structured_results = [
        None,
        {"tool": "record.add", "payload": {"text": "体重72kg", "category": "身体"}},
    ]
    acp.prompt_results = ["已记录。"]
    llm = UnifiedDecideLLM(acp=acp, session_id="sid-u", retry_count=2)
    out = _run(llm.structured_decide(user_id="u1", text="记一下体重", queue_snapshot=[]))
    assert out.tool == "record.add"
    assert out.payload["text"] == "体重72kg"
    assert out.reply == "已记录。"
    assert len(acp.structured_calls) == 2
    assert acp.prompt_calls
    assert acp.prompt_calls[0]["trace_tag"] == "v2_unified_reply"


def test_unified_decide_decision_only_spill_for_none():
    acp = _FakeACP()
    acp.structured_results = [
        None,
        {
            "tool": "none",
            "payload": {},
            OpenCodeACP.STRUCTURED_DECISION_SPILL_REPLY_KEY: "这是 spill",
        },
    ]
    llm = UnifiedDecideLLM(acp=acp, session_id="sid-u", retry_count=2)
    out = _run(llm.structured_decide(user_id="u1", text="随便聊聊", queue_snapshot=[]))
    assert out.tool == "none"
    assert out.reply == "这是 spill"
    assert not acp.prompt_calls


def test_unified_decide_includes_intent_hint_block():
    acp = _FakeACP()
    acp.structured_results = [{"tool": "none", "payload": {}, "reply": "ok"}]
    llm = UnifiedDecideLLM(acp=acp, session_id="sid-u", retry_count=2)
    out = _run(
        llm.structured_decide(
            user_id="u1",
            text="加个待办：喝水",
            queue_snapshot=[],
            intent_hint={"intent": "todo_add", "confidence": 0.9},
        )
    )
    assert out.tool == "none"
    assert acp.structured_calls
    msg = acp.structured_calls[0]["message"]
    assert "intent_hint_json" in msg
    assert "todo_add" in msg
