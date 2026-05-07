"""提醒 Coach：在 `## ⏰ 提醒` 节追加 `- [ ] HH:MM：内容`。

写完由 utils/refresh_hooks 在 dispatcher 末尾自动唤醒 reminder 调度器，
本模块不再需要手动调 notify_reminder_refresh()。
"""

from __future__ import annotations

from handlers.dispatcher import register_tool_handler
from utils.coach_tools import OUTPUT_WRITE_CONFIRM, build_coach_write_prompt
from utils.log_sync import append_to_markdown_section, get_log_path
from utils.tool_names import DOMAIN_REMIND, TOOL_REMIND_ADD


class RemindCoachMixin:
    async def _coach_remind_add(
        self,
        payload: dict,
        reply: str,
        from_user: str,
        context_token: str,
        user_text: str = "",
    ) -> bool:
        text = str(payload.get("text") or "").strip()
        hhmm = str(payload.get("hhmm") or "").strip()
        if not text or not hhmm:
            await self.wx.send_text(
                reply or "几点叫你？请补一个时间。", from_user, context_token
            )
            return True
        event_date = str(payload.get("event_date") or "").strip()
        v = self.cfg["vault"]

        # === Python 直写（优先） ===
        log_path = get_log_path(v["root"], v["daily_log_dir"])
        line = f"- [ ] {hhmm}：{text}"
        try:
            append_to_markdown_section(log_path, "## ⏰ 提醒", line)
            await self.wx.send_text(
                reply or f"⏰ 已设提醒：{hhmm} {text}", from_user, context_token
            )
            return True
        except Exception:
            pass

        # === 原有 LLM 路径（保留） ===
        prompt = build_coach_write_prompt(
            DOMAIN_REMIND,
            vault_root=v["root"],
            daily_log_dir=v["daily_log_dir"],
            payload={
                "op": "add",
                "text": text,
                "hhmm": hhmm,
                **({"event_date": event_date} if event_date else {}),
            },
            output_contract=OUTPUT_WRITE_CONFIRM,
        )
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            out, _ = await self.acp.prompt(
                self.remind_session_id, prompt, trace_tag="vault_remind_add"
            )
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
        out = (out or "").strip()[: self.cfg["bot"].get("max_reply_length", 2000)]
        await self.wx.send_text(
            reply or out or f"⏰ 已记录提醒：{hhmm}：{text}", from_user, context_token
        )
        return True


register_tool_handler(TOOL_REMIND_ADD, RemindCoachMixin._coach_remind_add)
