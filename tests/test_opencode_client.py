from __future__ import annotations

import asyncio

from acp.opencode_client import OpenCodeACP


def test_build_prompt_params_includes_max_tokens_by_default():
    acp = OpenCodeACP(max_tokens=500)
    params = acp._build_prompt_params("sid-1", [{"type": "text", "text": "hi"}])
    assert params["sessionId"] == "sid-1"
    assert params["maxTokens"] == 500


def test_build_prompt_params_omits_max_tokens_when_disabled():
    acp = OpenCodeACP(max_tokens=0)
    params = acp._build_prompt_params("sid-2", [{"type": "text", "text": "hi"}])
    assert params["sessionId"] == "sid-2"
    assert "maxTokens" not in params


def test_merge_stream_prefix_prefers_longer_result():
    rpc = {"result": {"parts": [{"type": "text", "text": "已记录到5月3日（事务）：她也改签"}]}}
    reply, meta = OpenCodeACP._merge_stream_and_result_reply(
        "已记录到5月3日（事务）：她也", rpc
    )
    assert reply == "已记录到5月3日（事务）：她也改签"
    assert meta["reply_selected_source"] == "merged"


def test_merge_equal_uses_stream_source():
    rpc = {"result": {"parts": [{"type": "text", "text": "同句"}]}}
    reply, meta = OpenCodeACP._merge_stream_and_result_reply("同句", rpc)
    assert reply == "同句"
    assert meta["reply_selected_source"] == "stream"


def test_merge_conflict_prefers_result_text():
    rpc = {"result": {"parts": [{"type": "text", "text": "结果侧完整句"}]}}
    reply, meta = OpenCodeACP._merge_stream_and_result_reply("流式半截完全不同", rpc)
    assert reply == "结果侧完整句"
    assert meta["reply_merge_conflict"] is True
    assert meta["reply_selected_source"] == "result"


def test_project_obj_strips_extra_keys_when_additional_properties_false():
    acp = OpenCodeACP()
    schema = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": ["none"]},
            "payload": {"type": "object"},
        },
        "required": ["tool", "payload"],
        "additionalProperties": False,
    }
    raw = {"tool": "none", "payload": {}, "reply": "should be stripped"}
    projected = acp._project_obj_to_schema_properties(raw, schema)
    assert projected == {"tool": "none", "payload": {}}
    assert acp._validate_schema_obj(projected, schema) is True


def test_prompt_structured_attaches_spill_when_reply_extra_key():
    acp = OpenCodeACP()

    async def _stub_prompt(_sid, _msg, *, trace_tag="x"):
        return '{"tool":"none","payload":{},"reply":"  复用这句  "}', ""

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
    out = asyncio.run(
        acp.prompt_structured("sid", "msg", json_schema=schema, retry_count=1, trace_tag="t")
    )
    assert out == {
        "tool": "none",
        "payload": {},
        OpenCodeACP.STRUCTURED_DECISION_SPILL_REPLY_KEY: "复用这句",
    }
