"""时间轴：睡觉（显式）、checkin 用户回填、与主路由前置拦截。

「起床」由 Handler 在每日首条微信时自动切 active，不在此写时间轴。"""

from __future__ import annotations

import time
from datetime import datetime

from utils.flow_log import log_flow_event
from utils.timeline_state import get_checkin_expect, get_state, pop_checkin_expect, set_state
from utils.timeline_sync import (
    get_slot_body,
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

        if await self._maybe_consume_checkin_expect(text, from_user, context_token):
            return True

        return False

    async def _maybe_consume_checkin_expect(
        self, text: str, from_user: str, context_token: str
    ) -> bool:
        exp = get_checkin_expect(self.cfg, from_user)
        if not exp:
            return False
        try:
            ts = float(exp.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if time.time() - ts > 900:
            pop_checkin_expect(self.cfg, from_user)
            return False

        slot = str(exp.get("slot") or "").strip()
        day_s = str(exp.get("date") or "").strip()
        if not slot or not day_s:
            pop_checkin_expect(self.cfg, from_user)
            return False

        try:
            tdt = datetime.strptime(day_s, "%Y-%m-%d")
        except ValueError:
            pop_checkin_expect(self.cfg, from_user)
            return False

        body = (text or "").strip()
        if not body:
            return False

        pop_checkin_expect(self.cfg, from_user)
        tl = self.cfg.get("timeline") or {}
        allow_append_when_filled = bool(tl.get("checkin_write_if_filled", False))
        existing = get_slot_body(self.cfg, slot, dt=tdt)
        if existing and not allow_append_when_filled:
            log_flow_event(
                stage="timeline",
                route="checkin_skip_filled_no_append",
                user_text=body,
                from_user=from_user,
                extra={"slot": slot, "existing": existing[:120]},
            )
            await self.wx.send_text(
                f"该格 {slot} 已有记录；如需追加请明确说“追加到时间轴”。",
                from_user,
                context_token,
            )
            return True

        upsert_timeline_slot(self.cfg, slot, body, dt=tdt)
        await self.wx.send_text(
            f"已记到时间轴 {slot}～",
            from_user,
            context_token,
        )
        return True

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
            upsert_timeline_slot(self.cfg, slot, "🌙 睡觉", dt=now)
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
