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

    async def _stub_prompt(_sid, _msg, *, trace_tag="x", prompt_extra=None):
        _ = prompt_extra
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

    async def _stub_prompt(_sid, _msg, *, trace_tag="x", prompt_extra=None):
        _ = prompt_extra
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


def test_prompt_structured_recovers_tool_payload_when_reply_truncated():
    """reply 被截断导致整段 JSON 非法时，仍可从原文恢复 remind.add + payload。"""
    acp = OpenCodeACP()
    broken = (
        '{"tool":"remind.add","payload":{"text":"下班回家","hhmm":"22:00",'
        '"event_date":"2026-05-03"},"reply":"好嘞 十点叫你'
    )

    async def _stub_prompt(_sid, _msg, *, trace_tag="x", prompt_extra=None):
        _ = prompt_extra
        return broken, ""

    acp.prompt = _stub_prompt  # type: ignore[method-assign]
    schema = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": ["none", "remind.add"]},
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
            retry_count=1,
            trace_tag="unified_decide_combined",
        )
    )
    assert out["tool"] == "remind.add"
    assert out["payload"]["hhmm"] == "22:00"
    assert out["payload"]["text"] == "下班回家"


def test_prompt_structured_never_merges_reasoning_into_reply():
    """即便 reasoning 含自然语言，也不能拼进用户可见 reply。"""
    acp = OpenCodeACP()
    final = '{"tool":"none","payload":{},"reply":"哟 爸爸终于来啦\\n今天有啥吩咐没"}'
    reasoning = (
        'The user message is "你好" (hello). This is a simple greeting/chat.\n'
        "So I should select none."
    )

    async def _stub_prompt(_sid, _msg, *, trace_tag="x", prompt_extra=None):
        _ = prompt_extra
        return final, reasoning

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
            retry_count=1,
            trace_tag="unified_decide_combined",
        )
    )
    assert out["tool"] == "none"
    assert out["reply"] == "哟 爸爸终于来啦\n今天有啥吩咐没"
    assert "The user message is" not in out["reply"]


def test_sanitize_vague_none_reply_appends_hint_when_no_evidence():
    from handlers.dispatcher import DispatcherMixin

    raw = "就这个目录 刚给你列过了 还有啥想看的"
    out = DispatcherMixin._sanitize_vague_none_reply(raw, "none")
    assert "重发完整列表" in out
    assert "`9417ccd`" in DispatcherMixin._sanitize_vague_none_reply(
        "刚列过\n- `9417ccd` feat: x", "none"
    )


def test_collect_prompt_response_handles_agent_requests():
    acp = OpenCodeACP()
    seen: list[dict] = []

    async def _stub_handle(msg):
        seen.append(msg)

    acp._handle_agent_request = _stub_handle  # type: ignore[method-assign]
    async def _run():
        await acp._response_queue.put(
            {"jsonrpc": "2.0", "id": 7, "method": "fs/list_directory", "params": {"path": "."}}
        )
        await acp._response_queue.put(
            {"jsonrpc": "2.0", "id": 99, "result": {"parts": [{"type": "text", "text": "ok"}]}}
        )
        return await acp._collect_prompt_response(99, session_id="sid", timeout=2)

    out = asyncio.run(_run())
    assert out["saw_final_response"] is True
    assert len(seen) == 1
    assert seen[0]["method"] == "fs/list_directory"


def test_permission_confirm_mode_waits_for_resolution():
    acp = OpenCodeACP()
    acp.set_session_permission_mode("sid-agent", "confirm")
    acp.set_session_context("sid-agent", {"from_user": "u1"})

    writes: list[dict] = []
    acp._write = lambda msg: writes.append(msg)  # type: ignore[method-assign]

    async def _stub_handler(_req):
        return "defer"

    acp.set_permission_request_handler(_stub_handler)

    async def _run():
        t = asyncio.create_task(
            acp._handle_agent_request(
                {
                    "id": 321,
                    "method": "session/request_permission",
                    "params": {"sessionId": "sid-agent", "reason": "danger"},
                }
            )
        )
        await asyncio.sleep(0.05)
        assert acp.has_pending_permission_request(321) is True
        assert acp.resolve_permission_request(321, approved=True) is True
        await t

    asyncio.run(_run())
    assert writes[-1]["id"] == 321
    assert writes[-1]["result"]["outcome"]["outcome"] == "approved"


def test_create_session_passes_configured_mcp_servers():
    acp = OpenCodeACP(mcp_servers=[{"name": "demo"}])
    seen: list[dict] = []

    async def _stub_send_and_recv(method, params=None, timeout=120):  # noqa: ARG001
        seen.append({"method": method, "params": params})
        if method == "session/new":
            return {"result": {"sessionId": "sid-x"}}
        return {"result": {}}

    async def _stub_set_model(_sid, _model):
        return None

    acp._send_and_recv = _stub_send_and_recv  # type: ignore[method-assign]
    acp._set_model = _stub_set_model  # type: ignore[method-assign]
    sid = asyncio.run(acp.create_session())
    assert sid == "sid-x"
    assert seen[0]["method"] == "session/new"
    assert seen[0]["params"]["mcpServers"] == [{"name": "demo"}]


def test_create_session_passes_configured_mcp_servers():
    acp = OpenCodeACP(mcp_servers=[{"name": "demo"}])
    seen: list[dict] = []

    async def _stub_send_and_recv(method, params=None, timeout=120):  # noqa: ARG001
        seen.append({"method": method, "params": params})
        if method == "session/new":
            return {"result": {"sessionId": "sid-x"}}
        return {"result": {}}

    async def _stub_set_model(_sid, _model):
        return None

    acp._send_and_recv = _stub_send_and_recv  # type: ignore[method-assign]
    acp._set_model = _stub_set_model  # type: ignore[method-assign]
    sid = asyncio.run(acp.create_session())
    assert sid == "sid-x"
    assert seen[0]["method"] == "session/new"
    assert seen[0]["params"]["mcpServers"] == [{"name": "demo"}]
