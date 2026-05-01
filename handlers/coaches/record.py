"""生活记录 Coach：把已发生事件按分类追加到 `## 📝 记录` 节。

写入由 record-coach SKILL 完成，本模块只负责拼 prompt + 调 ACP + 回微信。
"""

from __future__ import annotations

from config import _log_reasoning
from handlers.dispatcher import register_tool_handler
from utils.coach_tools import OUTPUT_WRITE_CONFIRM, build_coach_write_prompt
from utils.time_utils import time_str
from utils.tool_names import DOMAIN_RECORD, TOOL_RECORD_ADD


class RecordCoachMixin:
    async def _coach_record_add(
        self,
        payload: dict,
        reply: str,
        from_user: str,
        context_token: str,
        user_text: str = "",
    ) -> bool:
        text = str(payload.get("text") or user_text or "").strip()
        if not text:
            await self.wx.send_text(reply or "我没读懂这条记录。", from_user, context_token)
            return True
        category = str(payload.get("category") or "").strip() or "事务"
        event_date = str(payload.get("event_date") or "").strip()
        now_hm = time_str()
        v = self.cfg["vault"]
        prompt = build_coach_write_prompt(
            DOMAIN_RECORD,
            vault_root=v["root"],
            daily_log_dir=v["daily_log_dir"],
            payload={
                "op": "add",
                "text": text,
                "category": category,
                "now_hhmm": now_hm,
                **({"event_date": event_date} if event_date else {}),
            },
            output_contract=OUTPUT_WRITE_CONFIRM,
        )
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            out, reasoning = await self.acp.prompt(
                self.record_session_id, prompt, trace_tag="vault_record_add"
            )
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
        if reasoning:
            print(f"[Bot] 🧠 {reasoning[:200]}")
            _log_reasoning(user_text or text, reasoning)
        out = (out or "").strip()[: self.cfg["bot"].get("max_reply_length", 2000)]
        await self.wx.send_text(
            reply or out or f"已记录 {category}：{text}", from_user, context_token
        )
        return True


register_tool_handler(TOOL_RECORD_ADD, RecordCoachMixin._coach_record_add)
