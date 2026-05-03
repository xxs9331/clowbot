from __future__ import annotations

import asyncio
import json

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


def test_json_brace_balanced_for_tool_args():
    assert OpenCodeACP._json_brace_balanced('{"a":1,"b":{"c":2}}') is True
    assert OpenCodeACP._json_brace_balanced('{"a":1') is False


def test_json_brace_balanced_ignores_braces_inside_string_values():
    inner = json.dumps({"x": 1}, separators=(",", ":"))
    outer = json.dumps({"a": inner}, ensure_ascii=False)
    assert OpenCodeACP._json_brace_balanced(outer) is True


def test_tool_call_name_and_args_extracts_common_fields():
    name, args = OpenCodeACP._tool_call_name_and_args(
        {"toolName": "fs/read_text_file", "arguments": '{"path":"a.md"}'}
    )
    assert name == "fs/read_text_file"
    assert args == '{"path":"a.md"}'


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
    assert out["tool"] == "none"
    assert out["payload"] == {}
    assert out[OpenCodeACP.STRUCTURED_DECISION_SPILL_REPLY_KEY] == "复用这句"
    out.pop(OpenCodeACP.STRUCTURED_TRACE_META_KEY, None)
    assert set(out.keys()) == {"tool", "payload", OpenCodeACP.STRUCTURED_DECISION_SPILL_REPLY_KEY}


def test_prompt_structured_merges_best_effort_when_final_vague():
    """首轮自然语言含提交列表，次轮合规 JSON 但 reply 空泛时，应合并进 reply。"""
    acp = OpenCodeACP()
    first = (
        "让我看看这个目录里有什么哦 这里有 git 仓库！\n\n"
        "- `9417ccd` feat: 增强请求处理 (14分钟前)\n"
        "- `aa1beac` feat: 增强配置 (35小时前)\n"
    )
    second = '{"tool":"none","payload":{},"reply":"就这个目录 刚给你列过了 还有啥想看的"}'

    async def _stub_prompt(_sid, _msg, *, trace_tag="x"):
        if trace_tag.endswith("#1"):
            return first, ""
        return second, ""

    acp.prompt = _stub_prompt  # type: ignore[method-assign]
    schema = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": ["none"]},
            "payload": {"type": "object"},
            "reply": {"type": "string"},
        },
        "required": ["tool", "payload", "reply"],
        "additionalProperties": False,
    }
    out = asyncio.run(
        acp.prompt_structured(
            "sid",
            "msg",
            json_schema=schema,
            retry_count=3,
            trace_tag="unified_decide_combined",
        )
    )
    assert out["tool"] == "none"
    assert "9417ccd" in out["reply"]
    assert "刚给你列过了" in out["reply"]
    tr = out.pop(OpenCodeACP.STRUCTURED_TRACE_META_KEY, None)
    assert tr is not None
    assert tr["structured_attempts"] == 2
    assert "merged" in tr["final_reply_source"]


def test_sanitize_vague_none_reply_appends_hint_when_no_evidence():
    from handlers.dispatcher import DispatcherMixin

    raw = "就这个目录 刚给你列过了 还有啥想看的"
    out = DispatcherMixin._sanitize_vague_none_reply(raw, "none")
    assert "重发完整列表" in out
    assert "`9417ccd`" in DispatcherMixin._sanitize_vague_none_reply(
        "刚列过\n- `9417ccd` feat: x", "none"
    )
