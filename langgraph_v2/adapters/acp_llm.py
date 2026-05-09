from __future__ import annotations

import json
import re
from typing import Any

from acp.opencode_client import OpenCodeACP
from handlers.dispatcher import _coalesce_unified_decision, _load_unified_decide_prompts

from ..contracts import Decision


class ACPStructuredLLM:
    """Thin transport wrapper for ACP structured prompting."""

    def __init__(self, acp: OpenCodeACP, session_id: str):
        self._acp = acp
        self._session = session_id

    async def prompt_structured(
        self, *, prompt: str, schema: dict[str, Any], retry: int = 3
    ) -> dict[str, Any] | None:
        return await self._acp.prompt_structured(
            self._session,
            prompt,
            json_schema=schema,
            retry_count=int(retry or 3),
            trace_tag="v2_unified_structured",
        )


class UnifiedDecideLLM:
    """LLMProvider implementation with dispatcher-compatible degrade path."""

    def __init__(
        self,
        *,
        acp: OpenCodeACP,
        session_id: str,
        retry_count: int = 3,
    ):
        self._acp = acp
        self._session_id = session_id
        self._retry_count = int(retry_count or 3)
        self._transport = ACPStructuredLLM(acp, session_id)

    @staticmethod
    def _schema(include_reply: bool) -> dict[str, Any]:
        tools = [
            "todo.merge_new_items",
            "todo.done_current",
            "todo.not_done",
            "todo.next",
            "todo.reorder",
            "todo.reorder_confirm",
            "todo.skip_current",
            "todo.abandon_current",
            "record.add",
            "remind.add",
            "timeline.append",
            "none",
        ]
        props: dict[str, Any] = {
            "tool": {"type": "string", "enum": tools},
            "payload": {"type": "object"},
        }
        required = ["tool", "payload"]
        if include_reply:
            props["reply"] = {"type": "string"}
            required.append("reply")
        return {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }

    @staticmethod
    def _build_context_block(text: str, queue_snapshot: list[str]) -> str:
        return (
            f"- current_task: {queue_snapshot[0] if queue_snapshot else 'null'}\n"
            f"- remaining_tasks: {json.dumps(queue_snapshot, ensure_ascii=False)}\n"
            f"- pending_reorder: []\n"
            f"- user_message: {text}\n"
        )

    @staticmethod
    def _rules_block() -> str:
        return (
            "字段：\n"
            "- tool: 选择最贴近用户意图的动作；不确定时用 none\n"
            "- payload: 与 tool 匹配的对象；无参数时给 {}\n"
            "- reply: 给用户的自然中文短句（仅 combined 模式需要）\n"
        )

    def _build_combined_prompt(self, *, text: str, queue_snapshot: list[str]) -> str:
        combined, _, _, _ = _load_unified_decide_prompts()
        return combined.format(
            hint_block="",
            tool_hint="",
            rules=self._rules_block(),
            context_block=self._build_context_block(text, queue_snapshot),
        )

    def _build_decision_only_prompt(self, *, text: str, queue_snapshot: list[str]) -> str:
        _, decision_only, _, _ = _load_unified_decide_prompts()
        return decision_only.format(
            hint_block="",
            tool_hint="",
            rules=self._rules_block(),
            context_block=self._build_context_block(text, queue_snapshot),
        )

    async def _generate_reply(self, *, user_text: str, tool: str, payload: dict[str, Any]) -> str:
        _, _, reply_none_tmpl, reply_action_tmpl = _load_unified_decide_prompts()
        if tool == "none":
            msg = reply_none_tmpl.format(user_text=user_text)
        else:
            msg = reply_action_tmpl.format(
                user_text=user_text,
                tool=tool,
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
        reply, _ = await self._acp.prompt(
            self._session_id, msg, trace_tag="v2_unified_reply"
        )
        return str(reply or "").strip()

    @staticmethod
    def _extract_json_object(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    async def structured_decide(
        self, *, user_id: str, text: str, queue_snapshot: list[str]
    ) -> Decision:
        _ = user_id
        combined_prompt = self._build_combined_prompt(text=text, queue_snapshot=queue_snapshot)
        combined = await self._transport.prompt_structured(
            prompt=combined_prompt,
            schema=self._schema(include_reply=True),
            retry=self._retry_count,
        )
        if isinstance(combined, dict):
            d = dict(combined)
            d.pop(OpenCodeACP.STRUCTURED_TRACE_META_KEY, None)
            tool, payload, reply = _coalesce_unified_decision(d)
            return Decision(tool=tool, payload=payload, reply=reply)

        decision_prompt = self._build_decision_only_prompt(
            text=text, queue_snapshot=queue_snapshot
        )
        decision_only = await self._transport.prompt_structured(
            prompt=decision_prompt,
            schema=self._schema(include_reply=False),
            retry=self._retry_count,
        )
        if isinstance(decision_only, dict):
            d = dict(decision_only)
            d.pop(OpenCodeACP.STRUCTURED_TRACE_META_KEY, None)
            spill = str(d.pop(OpenCodeACP.STRUCTURED_DECISION_SPILL_REPLY_KEY, "") or "").strip()
            tool, payload, _ = _coalesce_unified_decision(d)
            if spill and tool == "none":
                return Decision(tool=tool, payload=payload, reply=spill)
            reply = await self._generate_reply(user_text=text, tool=tool, payload=payload)
            return Decision(tool=tool, payload=payload, reply=reply)

        fallback_prompt = (
            "你必须仅输出 JSON："
            '{"tool":"none","payload":{},"reply":"一句简短中文回复"}\n'
            f"用户原话：{text}"
        )
        raw_reply, _ = await self._acp.prompt(
            self._session_id, fallback_prompt, trace_tag="v2_unified_decide_fallback"
        )
        obj = self._extract_json_object(raw_reply or "")
        tool, payload, reply = _coalesce_unified_decision(obj if isinstance(obj, dict) else {})
        return Decision(tool=tool, payload=payload, reply=reply)

