"""时间轴显式追加 Coach：自然语言触发 timeline.append。"""

from __future__ import annotations

import asyncio  # noqa: F401 — 规格要求 coach 模块保留 asyncio 入口
import re
from datetime import datetime

from handlers.dispatcher import register_tool_handler
from utils.flow_log import log_flow_event
from utils.timeline_compact import compact_timeline_line
from utils.timeline_sync import (
    ensure_timeline_file,
    slot_at,
    slot_for_hhmm,
    timeline_enabled,
    upsert_timeline_slot,
)
from utils.tool_names import TOOL_TIMELINE_APPEND

_SLOT_HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")


class TimelineAppendMixin:
    async def _coach_timeline_append(
        self,
        payload: dict,
        reply: str,
        from_user: str,
        context_token: str,
        user_text: str = "",
    ) -> bool:
        raw_text = str(payload.get("text") or user_text or "").strip()
        if not raw_text:
            await self.wx.send_text(reply or "要写的内容是哪一句？", from_user, context_token)
            return True

        if not timeline_enabled(self.cfg):
            await self.wx.send_text(
                reply or "当前未启用时间轴写入。", from_user, context_token
            )
            return True

        slot_raw = str(payload.get("slot") or "").strip()
        now = datetime.now()
        if slot_raw and _SLOT_HHMM_RE.match(slot_raw):
            slot = slot_for_hhmm(slot_raw, now)
        else:
            slot = slot_at(now)

        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            line = await compact_timeline_line(
                self.acp,
                self.cfg,
                self.unified_session_id,
                raw_text,
                trace_tag="timeline_append_compact",
            )
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)

        if not line:
            await self.wx.send_text("压缩后为空，请换一句试试。", from_user, context_token)
            return True

        ensure_timeline_file(self.cfg, now)
        ok = upsert_timeline_slot(self.cfg, slot, line, dt=now)
        if not ok:
            log_flow_event(
                stage="timeline",
                route="append_fail",
                user_text=raw_text[:120],
                from_user=from_user,
                extra={"slot": slot},
            )
            await self.wx.send_text(
                reply or "时间轴写入失败，请稍后再试。", from_user, context_token
            )
            return True

        head = (reply or "").strip() or f"已追加到时间轴 {slot}～"
        await self.wx.send_text(f"{head}\n{line}", from_user, context_token)
        return True


register_tool_handler(TOOL_TIMELINE_APPEND, TimelineAppendMixin._coach_timeline_append)
