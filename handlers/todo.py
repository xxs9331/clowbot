"""待办：决策层 tool + payload；执行层映射到 coach 逻辑；Vault 由 read/write_todo_section 交给大模型 + todo-coach。"""

import json
from datetime import datetime
from typing import Any

from acp.opencode_client import build_system_prompt, build_todo_coach_skill_binding
from config import _log_reasoning
from utils.todo_vault_tools import (
    OUTPUT_SYNC_QUEUE_JSON,
    OUTPUT_WRITE_CONFIRM,
    TOOL_READ_TODO_SECTION,
    TOOL_WRITE_TODO_SECTION,
    build_todo_vault_tool_prompt,
)

# ─── 统一决策 tool 名（与 Vault 工具 read_todo_section / write_todo_section 区分）───
TOOL_LIFE_LOG = "life_log"
TOOL_DECISION_NONE = "none"
TOOL_MERGE_NEW_ITEMS = "merge_new_items"
TOOL_DONE_CURRENT = "done_current"
TOOL_NOT_DONE = "not_done"
TOOL_NEXT = "next"
TOOL_REORDER = "reorder"
TOOL_REORDER_CONFIRM = "reorder_confirm"
TOOL_SKIP_CURRENT = "skip_current"
TOOL_ABANDON_CURRENT = "abandon_current"

_COACH_TOOLS = frozenset(
    {
        TOOL_MERGE_NEW_ITEMS,
        TOOL_DONE_CURRENT,
        TOOL_NOT_DONE,
        TOOL_NEXT,
        TOOL_REORDER,
        TOOL_REORDER_CONFIRM,
        TOOL_SKIP_CURRENT,
        TOOL_ABANDON_CURRENT,
    }
)


def _coalesce_unified_decision(decision: dict) -> tuple[str, dict[str, Any], str]:
    """解析 LLM JSON：tool + payload + reply；兼容顶层 tasks/reorder 误入 payload。"""
    tool = str(decision.get("tool", TOOL_DECISION_NONE) or TOOL_DECISION_NONE).strip().lower()
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


class TodoMixin:
    def _set_todo_queue(self, user_id: str, tasks: list[str]):
        self._todo_queues[user_id] = {"tasks": tasks, "idx": 0}

    def _get_current_queue_task(self, user_id: str) -> str:
        state = self._todo_queues.get(user_id)
        if not state:
            return ""
        idx = state.get("idx", 0)
        tasks = state.get("tasks", [])
        if idx >= len(tasks):
            return ""
        return tasks[idx]

    def _advance_queue_task(self, user_id: str) -> str:
        state = self._todo_queues.get(user_id)
        if not state:
            return ""
        state["idx"] = state.get("idx", 0) + 1
        next_task = self._get_current_queue_task(user_id)
        if not next_task:
            self._todo_queues.pop(user_id, None)
        return next_task

    def _get_remaining_queue_tasks(self, user_id: str) -> list[str]:
        state = self._todo_queues.get(user_id)
        if not state:
            return []
        idx = state.get("idx", 0)
        tasks = state.get("tasks", [])
        return tasks[idx:] if idx < len(tasks) else []

    def _todo_vault_tool_message(
        self,
        tool_id: str,
        payload: dict,
        output_contract: str,
    ) -> str:
        v = self.cfg["vault"]
        return build_todo_vault_tool_prompt(
            build_system_prompt(v["root"], v["daily_log_dir"]),
            v["root"],
            v["daily_log_dir"],
            tool_id,
            payload,
            output_contract,
        )

    async def _sync_todo_queue_from_vault(self, user_id: str, fallback_tasks: list[str]) -> None:
        msg = self._todo_vault_tool_message(
            TOOL_READ_TODO_SECTION,
            {},
            OUTPUT_SYNC_QUEUE_JSON,
        )
        reply, _ = await self.acp.prompt(
            self.session_id, msg, trace_tag="vault_sync_queue"
        )
        data = self._extract_json_object(reply)
        flat = data.get("queue_flat") if isinstance(data, dict) else None
        if isinstance(flat, list) and flat:
            tasks = [str(x).strip() for x in flat if str(x).strip()]
            if tasks:
                self._set_todo_queue(user_id, tasks)
                return
        self._set_todo_queue(user_id, fallback_tasks)

    async def _vault_tool_rewrite_flat(self, ordered_tasks: list[str]) -> None:
        if not ordered_tasks:
            return
        msg = self._todo_vault_tool_message(
            TOOL_WRITE_TODO_SECTION,
            {"op": "rewrite_section_flat", "flat_order": ordered_tasks},
            OUTPUT_WRITE_CONFIRM,
        )
        await self.acp.prompt(self.session_id, msg, trace_tag="vault_reorder_todo")

    async def _vault_tool_remove_subitem(self, label: str) -> None:
        msg = self._todo_vault_tool_message(
            TOOL_WRITE_TODO_SECTION,
            {"op": "remove_subitem", "remove_label": label},
            OUTPUT_WRITE_CONFIRM,
        )
        await self.acp.prompt(self.session_id, msg, trace_tag="vault_abandon_item")

    async def _llm_unified_decide(self, user_id: str, text: str) -> dict:
        """只输出 JSON：tool + payload + reply（待办 coach 或 life_log / none）。"""
        current_task = self._get_current_queue_task(user_id)
        remaining = self._get_remaining_queue_tasks(user_id)
        pending_reorder = self._pending_reorders.get(user_id, [])
        vault_root = self.cfg["vault"]["root"]
        skill_ctx = build_todo_coach_skill_binding(vault_root)
        prompt = (
            f"{skill_ctx}\n\n"
            "你是微信个人助手。待办话术与意图划分以 todo-coach 为准。\n"
            "只输出 JSON，不要其它内容。\n"
            "字段：\n"
            f'- tool: 字符串，取值之一：'
            f'"{TOOL_LIFE_LOG}" | "{TOOL_DECISION_NONE}" | '
            f'"{TOOL_MERGE_NEW_ITEMS}" | "{TOOL_DONE_CURRENT}" | "{TOOL_NOT_DONE}" | "{TOOL_NEXT}" | '
            f'"{TOOL_REORDER}" | "{TOOL_REORDER_CONFIRM}" | "{TOOL_SKIP_CURRENT}" | "{TOOL_ABANDON_CURRENT}"\n'
            '- payload: 对象（见下）\n'
            '- reply: 可选，给用户的微信短句\n\n'
            "payload 约定：\n"
            f'- "{TOOL_MERGE_NEW_ITEMS}": {{"tasks": ["项1", ...]}}\n'
            f'- "{TOOL_REORDER}": {{"reorder": [...]}}，元素集合须与 remaining_tasks 相同，仅顺序可变\n'
            "- 其它 coach tool：通常为 {{}}\n\n"
            "分流：生活记录（体重/饮食/睡眠/快递/出行等）→ "
            f'tool="{TOOL_LIFE_LOG}"，payload 可为 {{}}；'
            "待办推进/完成/重排/跳过/放弃 → 对应 coach tool；不确定 → "
            f'tool="{TOOL_DECISION_NONE}"。\n\n'
            f"- current_task: {current_task or 'null'}\n"
            f"- remaining_tasks: {json.dumps(remaining, ensure_ascii=False)}\n"
            f"- pending_reorder: {json.dumps(pending_reorder, ensure_ascii=False)}\n"
            f"- user_message: {text}\n"
        )
        reply, _ = await self.acp.prompt(
            self.session_id, prompt, trace_tag="unified_decide"
        )
        decision = self._extract_json_object(reply)
        if not isinstance(decision, dict):
            return {"tool": TOOL_DECISION_NONE, "payload": {}, "reply": ""}
        return decision

    async def _execute_coach_tool(
        self,
        tool: str,
        payload: dict[str, Any],
        reply: str,
        from_user: str,
        context_token: str,
    ) -> bool:
        if tool == TOOL_MERGE_NEW_ITEMS:
            return await self._coach_merge_new_items(payload, reply, from_user, context_token)
        if tool == TOOL_DONE_CURRENT:
            return await self._coach_done_current(payload, reply, from_user, context_token)
        if tool == TOOL_NOT_DONE:
            return await self._coach_not_done(payload, reply, from_user, context_token)
        if tool == TOOL_NEXT:
            return await self._coach_next(payload, reply, from_user, context_token)
        if tool == TOOL_REORDER:
            return await self._coach_reorder(payload, reply, from_user, context_token)
        if tool == TOOL_REORDER_CONFIRM:
            return await self._coach_reorder_confirm(payload, reply, from_user, context_token)
        if tool == TOOL_SKIP_CURRENT:
            return await self._coach_skip_current(payload, reply, from_user, context_token)
        if tool == TOOL_ABANDON_CURRENT:
            return await self._coach_abandon_current(payload, reply, from_user, context_token)
        return False

    async def _coach_merge_new_items(
        self, payload: dict, reply: str, from_user: str, context_token: str
    ) -> bool:
        tasks = [str(t).strip() for t in (payload.get("tasks") or []) if str(t).strip()]
        if not tasks:
            return False
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        vault_reply = ""
        try:
            prompt = self._todo_vault_tool_message(
                TOOL_WRITE_TODO_SECTION,
                {"op": "merge_new_items", "new_items_ordered": tasks},
                OUTPUT_WRITE_CONFIRM,
            )
            vault_reply, _ = await self.acp.prompt(
                self.session_id, prompt, trace_tag="vault_append_todo"
            )
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
        self._pending_reorders.pop(from_user, None)
        await self._sync_todo_queue_from_vault(from_user, tasks)
        first_task = self._get_current_queue_task(from_user)
        base_ack = (reply or (vault_reply or "").strip() or f"记下了，这{len(tasks)}个我按顺序陪你做。")
        ack = base_ack
        if first_task:
            ack = f"{ack}\n先做：{first_task}，做完了吗？"
        await self.wx.send_text(ack, from_user, context_token)
        return True

    async def _coach_done_current(
        self, payload: dict, reply: str, from_user: str, context_token: str
    ) -> bool:
        current_task = self._get_current_queue_task(from_user)
        if not current_task:
            await self.wx.send_text(reply or "你现在没有进行中的待办。", from_user, context_token)
            return True
        state = self._todo_queues.get(from_user, {})
        total_count = len(state.get("tasks", []))
        idx = state.get("idx", 0)
        completed_count = min(idx + 1, total_count) if total_count > 0 else 0
        self._pending_reorders.pop(from_user, None)
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            now_hm = datetime.now().strftime("%H:%M")
            prompt = self._todo_vault_tool_message(
                TOOL_WRITE_TODO_SECTION,
                {
                    "op": "mark_progress",
                    "just_completed_item": current_task,
                    "completed_at_hhmm": now_hm,
                },
                OUTPUT_WRITE_CONFIRM,
            )
            await self.acp.prompt(
                self.session_id, prompt, trace_tag="vault_mark_done"
            )
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
        next_task = self._advance_queue_task(from_user)
        progress_text = f"（进度 {completed_count}/{total_count}）" if total_count > 0 else ""
        done_reply = reply or f"做得好，{current_task}已完成。{progress_text}"
        if next_task:
            done_reply = f"{done_reply}\n下一个：{next_task}，做完了吗？"
        await self.wx.send_text(done_reply, from_user, context_token)
        return True

    async def _coach_not_done(
        self, payload: dict, reply: str, from_user: str, context_token: str
    ) -> bool:
        current_task = self._get_current_queue_task(from_user)
        fallback = f"先做1分钟版本：{current_task}，做好再回我“好了”。" if current_task else "没问题，你先发几个待办我来排。"
        await self.wx.send_text(reply or fallback, from_user, context_token)
        return True

    async def _coach_next(
        self, payload: dict, reply: str, from_user: str, context_token: str
    ) -> bool:
        current_task = self._get_current_queue_task(from_user)
        fallback = f"你现在先做：{current_task}" if current_task else "当前没有进行中的短待办。"
        await self.wx.send_text(reply or fallback, from_user, context_token)
        return True

    async def _coach_reorder(
        self, payload: dict, reply: str, from_user: str, context_token: str
    ) -> bool:
        order = [str(t).strip() for t in (payload.get("reorder") or []) if str(t).strip()]
        remaining = self._get_remaining_queue_tasks(from_user)
        if not remaining or sorted(order) != sorted(remaining):
            await self.wx.send_text("顺序建议我收到了，但还不能安全改队列，请你再确认一次。", from_user, context_token)
            return True
        self._pending_reorders[from_user] = order
        ask = reply or f"我建议顺序：{' → '.join(order)}。按这个顺序更新吗？"
        await self.wx.send_text(ask, from_user, context_token)
        return True

    async def _coach_reorder_confirm(
        self, payload: dict, reply: str, from_user: str, context_token: str
    ) -> bool:
        order = self._pending_reorders.get(from_user, [])
        if not order:
            await self.wx.send_text(reply or "当前没有待确认的重排建议。", from_user, context_token)
            return True
        self._set_todo_queue(from_user, order)
        self._pending_reorders.pop(from_user, None)
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            await self._vault_tool_rewrite_flat(order)
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
        current_task = self._get_current_queue_task(from_user)
        confirm_reply = reply or "已按确认顺序更新。"
        if current_task:
            confirm_reply = f"{confirm_reply}\n先做：{current_task}，做完了吗？"
        await self.wx.send_text(confirm_reply, from_user, context_token)
        return True

    async def _coach_skip_current(
        self, payload: dict, reply: str, from_user: str, context_token: str
    ) -> bool:
        state = self._todo_queues.get(from_user)
        if not state:
            await self.wx.send_text(reply or "当前没有可跳过的待办。", from_user, context_token)
            return True
        tasks = state.get("tasks", [])
        idx = state.get("idx", 0)
        if idx >= len(tasks):
            await self.wx.send_text(reply or "当前没有可跳过的待办。", from_user, context_token)
            return True
        self._pending_reorders.pop(from_user, None)
        skipped_task = tasks.pop(idx)
        tasks.append(skipped_task)
        next_task = self._get_current_queue_task(from_user)
        skip_reply = reply or f"先跳过：{skipped_task}。"
        if next_task and next_task != skipped_task:
            skip_reply = f"{skip_reply}\n现在先做：{next_task}，做完了吗？"
        await self.wx.send_text(skip_reply, from_user, context_token)
        return True

    async def _coach_abandon_current(
        self, payload: dict, reply: str, from_user: str, context_token: str
    ) -> bool:
        state = self._todo_queues.get(from_user)
        if not state:
            await self.wx.send_text(reply or "当前没有可放弃的待办。", from_user, context_token)
            return True
        tasks = state.get("tasks", [])
        idx = state.get("idx", 0)
        if idx >= len(tasks):
            await self.wx.send_text(reply or "当前没有可放弃的待办。", from_user, context_token)
            return True
        self._pending_reorders.pop(from_user, None)
        abandoned_task = tasks.pop(idx)
        if tasks:
            state["idx"] = min(idx, len(tasks) - 1)
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            await self._vault_tool_remove_subitem(abandoned_task)
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
        if not tasks:
            self._todo_queues.pop(from_user, None)
            await self.wx.send_text(reply or f"已放弃：{abandoned_task}。当前没有进行中的待办。", from_user, context_token)
            return True
        next_task = self._get_current_queue_task(from_user)
        abandon_reply = reply or f"已放弃：{abandoned_task}。"
        if next_task:
            abandon_reply = f"{abandon_reply}\n现在先做：{next_task}，做完了吗？"
        await self.wx.send_text(abandon_reply, from_user, context_token)
        return True

    async def _apply_unified_decision(
        self,
        decision: dict,
        from_user: str,
        context_token: str,
        user_text: str = "",
    ) -> bool:
        tool, payload, reply = _coalesce_unified_decision(decision)

        if tool == TOOL_LIFE_LOG:
            vault = self.cfg["vault"]
            system_prefix = build_system_prompt(
                vault_root=vault["root"],
                daily_log_dir=vault["daily_log_dir"],
                project_dir=vault.get("project_dir", ""),
                task_dir=vault.get("task_dir", ""),
            )
            out, reasoning = await self.acp.prompt(
                self.session_id,
                f"{system_prefix}\n用户发来：「{user_text}」",
                trace_tag="unified_life_log",
            )
            if reasoning:
                print(f"[Bot] 🧠 {reasoning[:200]}")
                _log_reasoning(user_text, reasoning)
            out = (out or "").strip()[: self.cfg["bot"].get("max_reply_length", 2000)]
            await self.wx.send_text(out or "已记录", from_user, context_token)
            return True

        if tool == TOOL_DECISION_NONE:
            if reply:
                await self.wx.send_text(reply, from_user, context_token)
                return True
            return False

        if tool in _COACH_TOOLS:
            return await self._execute_coach_tool(tool, payload, reply, from_user, context_token)

        if reply:
            await self.wx.send_text(reply, from_user, context_token)
            return True
        return False
