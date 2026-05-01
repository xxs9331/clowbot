"""待办队列与 LLM 决策执行"""

import json

from acp.opencode_client import build_system_prompt
from config import _log_reasoning


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

    async def _llm_todo_decide(self, user_id: str, text: str) -> dict:
        """让大模型统筹待办动作，返回结构化决策"""
        current_task = self._get_current_queue_task(user_id)
        remaining = self._get_remaining_queue_tasks(user_id)
        pending_reorder = self._pending_reorders.get(user_id, [])
        prompt = (
            "你是微信待办统筹助手。请根据用户消息和当前待办状态，输出 JSON 决策，不要输出其他内容。\n"
            "动作 action 仅允许：add_batch|done_current|not_done|next|reorder|reorder_confirm|none\n"
            "字段：\n"
            '- action: 字符串\n'
            '- tasks: 字符串数组（仅 add_batch 用）\n'
            '- reorder: 字符串数组（仅 reorder 用，必须是 remaining_tasks 的重排）\n'
            '- reply: 给用户的微信短句（1句）\n'
            f"- current_task: {current_task or 'null'}\n"
            f"- remaining_tasks: {json.dumps(remaining, ensure_ascii=False)}\n"
            f"- pending_reorder: {json.dumps(pending_reorder, ensure_ascii=False)}\n"
            f"- user_message: {text}\n"
            "规则：\n"
            "1) 能理解为完成当前任务时，用 done_current。\n"
            "2) 用户问先做哪个，用 next。\n"
            "3) 用户一次说多个事项，用 add_batch，并给具体任务名。\n"
            "4) 用户在讨论顺序时，用 reorder，并给建议顺序。\n"
            "5) 仅当用户明确确认（如“按这个来/就这个顺序/确认”）且存在pending_reorder时，用 reorder_confirm。\n"
            "6) 不确定时返回 none，并给简短reply。\n"
        )
        reply, _ = await self.acp.prompt(self.session_id, prompt)
        decision = self._extract_json_object(reply)
        if not isinstance(decision, dict):
            return {"action": "none", "reply": ""}
        return decision

    async def _llm_unified_decide(self, user_id: str, text: str) -> dict:
        """统一决策：待办统筹 or 生活日志记录"""
        current_task = self._get_current_queue_task(user_id)
        remaining = self._get_remaining_queue_tasks(user_id)
        pending_reorder = self._pending_reorders.get(user_id, [])
        prompt = (
            "你是微信个人助手。请根据用户消息输出 JSON 决策，不要输出其他内容。\n"
            "字段：\n"
            '- action: "todo" | "life" | "none"\n'
            '- sub: 子动作（仅 action=todo 时）: add_batch|done_current|not_done|next|reorder|reorder_confirm\n'
            '- tasks: 字符串数组（add_batch 用）\n'
            '- reorder: 字符串数组（reorder 用）\n'
            '- reply: 给用户的微信短句\n'
            f"- current_task: {current_task or 'null'}\n"
            f"- remaining_tasks: {json.dumps(remaining, ensure_ascii=False)}\n"
            f"- pending_reorder: {json.dumps(pending_reorder, ensure_ascii=False)}\n"
            f"- user_message: {text}\n"
            "规则：\n"
            "1) 涉及待办推进/完成/重排/查看队列 → action=todo。\n"
            "2) 生活记录（体重/饮食/睡眠/快递/出行）→ action=life。\n"
            "3) 不确定 → action=none。\n"
            "4) 有活跃待办队列时，优先走 todo。\n"
        )
        reply, _ = await self.acp.prompt(self.session_id, prompt)
        decision = self._extract_json_object(reply)
        if not isinstance(decision, dict):
            return {"action": "none", "reply": ""}
        return decision

    async def _apply_llm_todo_decision(self, decision: dict, from_user: str, context_token: str) -> bool:
        """执行模型决策。返回是否已处理该消息。"""
        action = str(decision.get("action", "none") or "none").strip().lower()
        if action == "none":
            return False

        reply = str(decision.get("reply", "")).strip()
        if action == "add_batch":
            tasks = [str(t).strip() for t in (decision.get("tasks") or []) if str(t).strip()]
            if not tasks:
                return False
            await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
            try:
                vault = self.cfg["vault"]
                system_prefix = build_system_prompt(
                    vault_root=vault["root"],
                    daily_log_dir=vault["daily_log_dir"],
                )
                append_lines = "\n".join([f"- [ ] {item}" for item in tasks])
                prompt = (
                    f"{system_prefix}\n"
                    f"用户要添加待办，条目如下：\n{append_lines}\n"
                    f"请在日志的「## 📋 待办」节按顺序追加这些行：\n{append_lines}\n"
                    f"如果该节不存在则创建。只回复确认信息。"
                )
                await self.acp.prompt(self.session_id, prompt)
            finally:
                await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
            self._set_todo_queue(from_user, tasks)
            self._pending_reorders.pop(from_user, None)
            first_task = self._get_current_queue_task(from_user)
            ack = reply or f"记下了，这{len(tasks)}个我按顺序陪你做。"
            if first_task:
                ack = f"{ack}\n先做：{first_task}，做完了吗？"
            await self.wx.send_text(ack, from_user, context_token)
            return True

        if action == "done_current":
            current_task = self._get_current_queue_task(from_user)
            if not current_task:
                await self.wx.send_text(reply or "你现在没有进行中的待办。", from_user, context_token)
                return True
            self._pending_reorders.pop(from_user, None)
            await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
            try:
                vault = self.cfg["vault"]
                system_prefix = build_system_prompt(
                    vault_root=vault["root"],
                    daily_log_dir=vault["daily_log_dir"],
                )
                prompt = (
                    f"{system_prefix}\n"
                    f"用户完成了待办：「{current_task}」\n"
                    f"请在日志的「## 📋 待办」节中找到对应条目，将 - [ ] 改为 - [x]，并追加 ✅HH:MM。\n"
                    f"只回复一句确认。"
                )
                await self.acp.prompt(self.session_id, prompt)
            finally:
                await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
            next_task = self._advance_queue_task(from_user)
            done_reply = reply or f"做得好，{current_task}已完成。"
            if next_task:
                done_reply = f"{done_reply}\n下一个：{next_task}，做完了吗？"
            await self.wx.send_text(done_reply, from_user, context_token)
            return True

        if action == "not_done":
            current_task = self._get_current_queue_task(from_user)
            fallback = f"先做1分钟版本：{current_task}，做好再回我“好了”。" if current_task else "没问题，你先发几个待办我来排。"
            await self.wx.send_text(reply or fallback, from_user, context_token)
            return True

        if action == "next":
            current_task = self._get_current_queue_task(from_user)
            fallback = f"你现在先做：{current_task}" if current_task else "当前没有进行中的短待办。"
            await self.wx.send_text(reply or fallback, from_user, context_token)
            return True

        if action == "reorder":
            order = [str(t).strip() for t in (decision.get("reorder") or []) if str(t).strip()]
            remaining = self._get_remaining_queue_tasks(from_user)
            if not remaining or sorted(order) != sorted(remaining):
                await self.wx.send_text("顺序建议我收到了，但还不能安全改队列，请你再确认一次。", from_user, context_token)
                return True
            self._pending_reorders[from_user] = order
            ask = reply or f"我建议顺序：{' → '.join(order)}。按这个顺序更新吗？"
            await self.wx.send_text(ask, from_user, context_token)
            return True

        if action == "reorder_confirm":
            order = self._pending_reorders.get(from_user, [])
            if not order:
                await self.wx.send_text(reply or "当前没有待确认的重排建议。", from_user, context_token)
                return True
            self._set_todo_queue(from_user, order)
            self._pending_reorders.pop(from_user, None)
            current_task = self._get_current_queue_task(from_user)
            confirm_reply = reply or "已按确认顺序更新。"
            if current_task:
                confirm_reply = f"{confirm_reply}\n先做：{current_task}，做完了吗？"
            await self.wx.send_text(confirm_reply, from_user, context_token)
            return True

        return False

    async def _apply_unified_decision(
        self,
        decision: dict,
        from_user: str,
        context_token: str,
        user_text: str = "",
    ) -> bool:
        """执行统一决策：todo 复用现有执行器，life 直接写入生活日志。"""
        action = str(decision.get("action", "none") or "none").strip().lower()

        if action == "todo":
            todo_decision = {
                "action": decision.get("sub", "none"),
                "tasks": decision.get("tasks"),
                "reorder": decision.get("reorder"),
                "reply": decision.get("reply", ""),
            }
            return await self._apply_llm_todo_decision(todo_decision, from_user, context_token)

        if action == "life":
            vault = self.cfg["vault"]
            system_prefix = build_system_prompt(
                vault_root=vault["root"],
                daily_log_dir=vault["daily_log_dir"],
                project_dir=vault.get("project_dir", ""),
                task_dir=vault.get("task_dir", ""),
            )
            reply, reasoning = await self.acp.prompt(
                self.session_id, f"{system_prefix}\n用户发来：「{user_text}」"
            )
            if reasoning:
                print(f"[Bot] 🧠 {reasoning[:200]}")
                _log_reasoning(user_text, reasoning)
            reply = (reply or "").strip()[: self.cfg["bot"].get("max_reply_length", 2000)]
            await self.wx.send_text(reply or "已记录", from_user, context_token)
            return True

        return False
