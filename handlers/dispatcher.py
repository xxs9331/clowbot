"""统一决策与分发层。

- _llm_unified_decide：让大模型把用户原文翻成 {tool, payload, reply} JSON
- _coalesce_unified_decision：把 LLM JSON 规整成 (tool, payload, reply) 三元组（含旧名映射、payload 野字段吸收）
- _apply_unified_decision：根据 tool 名查 _TOOL_HANDLERS 并执行；末尾统一调用 run_post_write_hooks

不持有业务状态；与 Coach Mixin 通过 method dispatcher 表协作（运行时 self 由 Handler 组合提供）。
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from utils.flow_log import log_flow_event
from utils.refresh_hooks import run_post_write_hooks
from utils.tool_names import (
    LEGACY_TOOL_ALIASES,  # noqa: F401  导入便于重导出
    TOOL_DECISION_NONE,
    TOOL_RECORD_ADD,
    TOOL_REMIND_ADD,
    TOOL_TODO_ABANDON_CURRENT,
    TOOL_TODO_DONE_CURRENT,
    TOOL_TODO_MERGE_NEW_ITEMS,
    TOOL_TODO_NEXT,
    TOOL_TODO_NOT_DONE,
    TOOL_TODO_REORDER,
    TOOL_TODO_REORDER_CONFIRM,
    TOOL_TODO_SKIP_CURRENT,
    normalize_tool_name,
)

# legacy 文本 JSON 解析仍失败时，若原文含「{」（多半在尝试输出 JSON），则将全文作为 reply 兜底，避免用户侧空白。
# 16K 字符量级：明显长于常见微信单条，又避免极端超长占用内存/日志。
UNIFIED_LEGACY_RAW_FALLBACK_MAX_CHARS = 16384


def _coalesce_unified_decision(decision: dict) -> tuple[str, dict[str, Any], str]:
    """LLM JSON -> (tool, payload, reply)。

    - tool：按 tool_names.normalize_tool_name 处理（小写 + 旧名映射）
    - payload：保证是 dict；若 LLM 把 tasks/reorder 写在了顶层而非 payload 里，自动吸收
    - reply：strip 后字符串
    """
    raw_tool = decision.get("tool", TOOL_DECISION_NONE) or TOOL_DECISION_NONE
    tool = normalize_tool_name(str(raw_tool))
    payload = decision.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    payload = dict(payload)
    if "tasks" in decision and "tasks" not in payload:
        t = decision.get("tasks")
        if isinstance(t, list):
            payload["tasks"] = t
    if "reorder" in decision and "reorder" not in payload:
        r = decision.get("reorder")
        if isinstance(r, list):
            payload["reorder"] = r
    reply = str(decision.get("reply", "") or "").strip()
    return tool, payload, reply


# 以 (handler, payload, reply, from_user, context_token, user_text) -> bool 为统一签名
_HandlerFn = Callable[[Any, dict, str, str, str, str], Awaitable[bool]]
_TOOL_HANDLERS: dict[str, _HandlerFn] = {}
_UNIFIED_TOOLS = (
    TOOL_TODO_MERGE_NEW_ITEMS,
    TOOL_TODO_DONE_CURRENT,
    TOOL_TODO_NOT_DONE,
    TOOL_TODO_NEXT,
    TOOL_TODO_REORDER,
    TOOL_TODO_REORDER_CONFIRM,
    TOOL_TODO_SKIP_CURRENT,
    TOOL_TODO_ABANDON_CURRENT,
    TOOL_RECORD_ADD,
    TOOL_REMIND_ADD,
    TOOL_DECISION_NONE,
)


def register_tool_handler(tool: str, fn: _HandlerFn) -> None:
    """coach 模块在 import 时调用，把自家 method 装进 dispatcher 表。

    用注册式而不是 import 式，避免 dispatcher → coaches → dispatcher 循环依赖。
    """
    _TOOL_HANDLERS[tool] = fn


def get_registered_tools() -> list[str]:
    return sorted(_TOOL_HANDLERS.keys())


class DispatcherMixin:
    """Handler 组合后即获得三域统一决策与分发能力。"""

    @staticmethod
    def _unified_decision_schema(*, include_reply: bool) -> dict:
        props: dict[str, Any] = {
            "tool": {"type": "string", "enum": list(_UNIFIED_TOOLS)},
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
    def _build_unified_context_block(
        *,
        current_task: str,
        remaining: list[str],
        pending_reorder: list[str],
        text: str,
    ) -> str:
        return (
            f"- current_task: {current_task or 'null'}\n"
            f"- remaining_tasks: {json.dumps(remaining, ensure_ascii=False)}\n"
            f"- pending_reorder: {json.dumps(pending_reorder, ensure_ascii=False)}\n"
            f"- user_message: {text}\n"
        )

    @staticmethod
    def _build_unified_legacy_prompt(hint_block: str, context_block: str) -> str:
        return (
            f"{hint_block}"
            "你是微信个人助手。待办话术与意图划分以 todo-coach 为准；生活记录以 record-coach 为准；"
            "提醒以 remind-coach 为准。你必须只输出一个合法 JSON 对象，不要其它内容。\n"
            "输出格式硬约束（必须全部满足）：\n"
            "1) 仅输出 1 行 JSON，对象根节点必须包含 tool、payload、reply 三个键\n"
            "2) 使用双引号，不要单引号，不要注释，不要 markdown，不要代码块\n"
            "3) reply 必须是 JSON 字符串；若需要换行，必须写成 \\\\n，禁止直接写真实换行\n"
            "4) 若不确定，输出 {\"tool\":\"none\",\"payload\":{},\"reply\":\"\"}\n"
            "5) 严禁在 JSON 前后输出任何说明文字\n"
            "请严格按这个骨架输出："
            "{\"tool\":\"<tool>\",\"payload\":{},\"reply\":\"<reply>\"}\n"
            "字段：\n"
            "- tool: 字符串，取值之一：\n"
            f'  待办：{TOOL_TODO_MERGE_NEW_ITEMS} | {TOOL_TODO_DONE_CURRENT} | {TOOL_TODO_NOT_DONE} | '
            f'{TOOL_TODO_NEXT} | {TOOL_TODO_REORDER} | {TOOL_TODO_REORDER_CONFIRM} | '
            f'{TOOL_TODO_SKIP_CURRENT} | {TOOL_TODO_ABANDON_CURRENT}\n'
            f'  生活记录：{TOOL_RECORD_ADD}\n'
            f'  设/加提醒：{TOOL_REMIND_ADD}\n'
            f'  不处理：{TOOL_DECISION_NONE}\n'
            "- payload: 对象（见下）\n"
            "- reply: 可选，给用户的微信短句\n\n"
            "payload 约定：\n"
            f'- "{TOOL_TODO_MERGE_NEW_ITEMS}": {{"tasks": ["项1", ...]}}\n'
            f'- "{TOOL_TODO_REORDER}": {{"reorder": [...]}}，元素集合须与 remaining_tasks 相同，仅顺序可变\n'
            f'- 其它 todo.*：通常为 {{}}\n'
            f'- "{TOOL_RECORD_ADD}": {{"text": "...", "category": "身体|运动|阅读|事务", "event_date": "YYYY-MM-DD?"}}\n'
            f'- "{TOOL_REMIND_ADD}": {{"text": "...", "hhmm": "HH:MM", "event_date": "YYYY-MM-DD?"}}\n\n'
            "分流：\n"
            f'- 已发生的生活事件（体重/饮食/睡眠/快递/出行已落实/已用药等）→ {TOOL_RECORD_ADD}\n'
            f'- 设提醒/叫我/别忘了+具体时间 → {TOOL_REMIND_ADD}\n'
            f'- 待办推进/完成/重排/跳过/放弃 → todo.*\n'
            f'- 纯闲聊/问助手状态 → {TOOL_DECISION_NONE}，reply 禁止假称正在查日记\n'
            f'- 查日记/记忆等只读需求由入口层处理，仍输出 {TOOL_DECISION_NONE}，reply 可提示用户用「查看记录」等，勿编造列表\n'
            f'- 其它不确定 → {TOOL_DECISION_NONE}\n\n'
            f"{context_block}"
        )

    async def _llm_unified_decide_action(
        self,
        user_id: str,
        text: str,
        *,
        intent_hint: dict | None = None,
    ) -> dict | None:
        current_task = self._get_current_queue_task(user_id)
        remaining = self._get_remaining_queue_tasks(user_id)
        pending_reorder = self._pending_reorders.get(user_id, [])
        context_block = self._build_unified_context_block(
            current_task=current_task,
            remaining=remaining,
            pending_reorder=pending_reorder,
            text=text,
        )
        hint_block = ""
        if isinstance(intent_hint, dict) and intent_hint:
            try:
                hint_block = (
                    "参考意图（来自小模型预分类，仅作参考，可推翻）：\n"
                    f"{json.dumps(intent_hint, ensure_ascii=False)}\n\n"
                )
            except Exception:
                hint_block = ""
        prompt = (
            f"{hint_block}"
            "你是微信个人助手。请只做动作决策，不生成给用户的话术。\n"
            "你必须仅输出一个 JSON 对象，字段只有 tool 与 payload。\n"
            "字段：\n"
            "- tool: 字符串，取值之一：\n"
            f'  待办：{TOOL_TODO_MERGE_NEW_ITEMS} | {TOOL_TODO_DONE_CURRENT} | {TOOL_TODO_NOT_DONE} | '
            f'{TOOL_TODO_NEXT} | {TOOL_TODO_REORDER} | {TOOL_TODO_REORDER_CONFIRM} | '
            f'{TOOL_TODO_SKIP_CURRENT} | {TOOL_TODO_ABANDON_CURRENT}\n'
            f'  生活记录：{TOOL_RECORD_ADD}\n'
            f'  设/加提醒：{TOOL_REMIND_ADD}\n'
            f'  不处理：{TOOL_DECISION_NONE}\n'
            "- payload: 对象（见下）\n\n"
            "payload 约定：\n"
            f'- "{TOOL_TODO_MERGE_NEW_ITEMS}": {{"tasks": ["项1", ...]}}\n'
            f'- "{TOOL_TODO_REORDER}": {{"reorder": [...]}}，元素集合须与 remaining_tasks 相同，仅顺序可变\n'
            f'- 其它 todo.*：通常为 {{}}\n'
            f'- "{TOOL_RECORD_ADD}": {{"text": "...", "category": "身体|运动|阅读|事务", "event_date": "YYYY-MM-DD?"}}\n'
            f'- "{TOOL_REMIND_ADD}": {{"text": "...", "hhmm": "HH:MM", "event_date": "YYYY-MM-DD?"}}\n\n'
            "分流规则：\n"
            f'- 已发生的生活事件（体重/饮食/睡眠/快递/出行已落实/已用药等）→ {TOOL_RECORD_ADD}\n'
            f'- 设提醒/叫我/别忘了+具体时间 → {TOOL_REMIND_ADD}\n'
            f'- 待办推进/完成/重排/跳过/放弃 → todo.*\n'
            f'- 纯闲聊、问助手在干嘛、评价/吐槽 → {TOOL_DECISION_NONE}（不得假称正在帮用户查日记或记忆）\n'
            f'- 读今日日记/查记忆/列最近几条记录等只读查询由消息层本地处理，若用户句子里已有「查看记录」「最近几条记忆」等触发词，仍选 {TOOL_DECISION_NONE}，不要编造查询结果\n'
            f'- 其它不确定 → {TOOL_DECISION_NONE}\n\n'
            f"{context_block}"
        )
        op_cfg = self.cfg.get("opencode", {}) or {}
        retry_count = int(op_cfg.get("structured_retry_count", 3) or 3)
        schema = self._unified_decision_schema(include_reply=False)
        decision = await self.acp.prompt_structured(
            self.unified_session_id,
            prompt,
            json_schema=schema,
            retry_count=retry_count,
            trace_tag="unified_decide_structured",
        )
        return decision

    async def _llm_generate_unified_reply(
        self,
        *,
        user_text: str,
        tool: str,
        payload: dict[str, Any],
    ) -> str:
        if tool == TOOL_DECISION_NONE:
            prompt = (
                "你是微信个人助手。根据用户原话回复 1～3 句自然、简短中文。\n"
                "硬约束：\n"
                "- 禁止用「等着我去翻」「我去查查」「快了快了」「别催」等假装正在查询、拖延交付的话术。\n"
                "- 若用户在要具体事实、清单、记忆/日记/记录内容，而你这里没有引用任何材料，"
                "应直接说明自己本轮拿不到日记正文，可请用户发「查看记录」或「找一下最近的三条记忆」"
                "这类话触发系统自动读取；不要承诺代查或演「正在翻」。\n"
                "- 纯闲聊、问你在做什么、吐槽等，正常接话即可。\n"
                "不要 JSON，不要解释，不要 markdown。\n"
                f"用户原话：{user_text}\n"
                "回复："
            )
        else:
            prompt = (
                "你是微信个人助手。动作已被系统接管执行，请输出一句简短确认文案。\n"
                "不要 JSON，不要解释，不要 markdown。\n"
                f"用户原话：{user_text}\n"
                f"已执行动作：tool={tool}, payload={json.dumps(payload, ensure_ascii=False)}\n"
                "回复："
            )
        reply, _ = await self.acp.prompt(
            self.unified_session_id, prompt, trace_tag="unified_reply"
        )
        return (reply or "").strip()

    async def _llm_unified_decide(
        self,
        user_id: str,
        text: str,
        *,
        intent_hint: dict | None = None,
    ) -> dict:
        """统一决策：structured 优先，文本 JSON 回退；回复文案与动作解耦。"""
        decision = await self._llm_unified_decide_action(
            user_id=user_id, text=text, intent_hint=intent_hint
        )
        if isinstance(decision, dict):
            log_flow_event(
                stage="route",
                route="unified_structured_ok",
                user_text=text,
                session_id=self.unified_session_id,
                extra={"decision": decision},
            )
            tool, payload, _ = _coalesce_unified_decision(decision)
            try:
                reply = await self._llm_generate_unified_reply(
                    user_text=text,
                    tool=tool,
                    payload=payload,
                )
            except Exception as e:  # noqa: BLE001
                log_flow_event(
                    stage="route",
                    route="reply_generate_fail",
                    user_text=text,
                    session_id=self.unified_session_id,
                    extra={"error": str(e)[:200], "tool": tool},
                )
                reply = ""
            return {"tool": tool, "payload": payload, "reply": reply}

        log_flow_event(
            stage="route",
            route="unified_structured_fail",
            user_text=text,
            session_id=self.unified_session_id,
            extra={"intent_hint": intent_hint},
        )
        # 回退旧路径：要求模型一次返回 {tool,payload,reply}
        current_task = self._get_current_queue_task(user_id)
        remaining = self._get_remaining_queue_tasks(user_id)
        pending_reorder = self._pending_reorders.get(user_id, [])
        context_block = self._build_unified_context_block(
            current_task=current_task,
            remaining=remaining,
            pending_reorder=pending_reorder,
            text=text,
        )
        hint_block = ""
        if isinstance(intent_hint, dict) and intent_hint:
            try:
                hint_block = (
                    "参考意图（来自小模型预分类，仅作参考，可推翻）：\n"
                    f"{json.dumps(intent_hint, ensure_ascii=False)}\n\n"
                )
            except Exception:
                hint_block = ""
        legacy_prompt = self._build_unified_legacy_prompt(hint_block, context_block)
        raw_reply, _ = await self.acp.prompt(
            self.unified_session_id, legacy_prompt, trace_tag="unified_decide_fallback"
        )
        legacy_decision = self._extract_json_object(raw_reply)
        raw_stripped = (raw_reply or "").strip()
        used_raw_body_fallback = False
        if not (isinstance(legacy_decision, dict) and legacy_decision):
            if raw_stripped and "{" in raw_stripped:
                legacy_decision = {
                    "tool": TOOL_DECISION_NONE,
                    "payload": {},
                    "reply": raw_stripped[:UNIFIED_LEGACY_RAW_FALLBACK_MAX_CHARS],
                }
                used_raw_body_fallback = True
            else:
                legacy_decision = {"tool": TOOL_DECISION_NONE, "payload": {}, "reply": ""}
        log_flow_event(
            stage="route",
            route="unified_text_fallback_used",
            user_text=text,
            session_id=self.unified_session_id,
            extra={
                "decision": legacy_decision,
                "raw_body_fallback": used_raw_body_fallback,
            },
        )
        return legacy_decision

    async def _apply_unified_decision(
        self,
        decision: dict,
        from_user: str,
        context_token: str,
        user_text: str = "",
    ) -> bool:
        """执行 dispatcher 表里 tool 名对应的 coach；末尾统一调用 post-write hooks。"""
        tool, payload, reply = _coalesce_unified_decision(decision)

        if tool == TOOL_DECISION_NONE:
            if reply:
                await self.wx.send_text(reply, from_user, context_token)
                return True
            return False

        handler = _TOOL_HANDLERS.get(tool)
        if handler is not None:
            handled = await handler(self, payload, reply, from_user, context_token, user_text)
            if handled:
                run_post_write_hooks(self, tool, payload)
            return handled

        # 未知 tool：尽量不让用户被静默丢弃
        if reply:
            await self.wx.send_text(reply, from_user, context_token)
            return True
        return False
