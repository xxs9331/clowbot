"""时间轴：睡觉（显式）、checkin 用户回填、与主路由前置拦截。

「起床」由 Handler 在每日首条微信时自动切 active，不在此写时间轴。"""

from __future__ import annotations

from datetime import datetime

from utils.flow_log import log_flow_event
from utils.timeline_compact import compact_timeline_line
from utils.timeline_state import set_state
from utils.timeline_sync import (
    timeline_enabled,
    upsert_timeline_slot,
    slot_at,
)


class TimelineHooksMixin:
    async def _timeline_preprocess(
        self, text: str, from_user: str, context_token: str
    ) -> bool:
        """时间轴相关前置：已消费则返回 True（不再走主路由）。"""
        if not timeline_enabled(self.cfg):
            return False

        if await self._maybe_timeline_wake_sleep(text, from_user, context_token):
            return True

        return False

    async def _maybe_timeline_wake_sleep(
        self, text: str, from_user: str, context_token: str
    ) -> bool:
        t = (text or "").strip()
        if not t:
            return False

        sleep_markers = ("睡觉了", "睡了", "晚安", "准备睡了", "我先睡了", "去睡了")

        if any(m in t for m in sleep_markers):
            set_state(self.cfg, "sleep")
            now = datetime.now()
            slot = slot_at(now)
            line = await compact_timeline_line(
                self.acp,
                self.cfg,
                self.unified_session_id,
                "🌙 睡觉",
                trace_tag="sleep_compact",
            )
            if not line:
                line = "🌙 睡觉"
            upsert_timeline_slot(self.cfg, slot, line, dt=now)
            log_flow_event(
                stage="checkin",
                route="user_sleep",
                user_text=t,
                from_user=from_user,
                extra={"slot": slot},
            )
            await self.wx.send_text(
                "收到，已休眠 checkin～明天你发来第一条微信消息会自动再开启催办。",
                from_user,
                context_token,
            )
            return True

        return False
