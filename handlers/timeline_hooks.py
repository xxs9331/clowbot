"""时间轴：睡觉（显式）、checkin 用户回填、与主路由前置拦截。

「起床」由 Handler 在每日首条微信时自动切 active，不在此写时间轴。"""

from __future__ import annotations

import time
from datetime import datetime

from utils.flow_log import log_flow_event
from utils.timeline_compact import compact_timeline_line
from utils.timeline_state import get_checkin_expect, get_state, pop_checkin_expect, set_state
from utils.timeline_sync import (
    get_slot_body,
    timeline_enabled,
    upsert_timeline_slot,
    slot_at,
)

# checkin 回填：已有内容时须显式追加意图（与 scheduler 提示一致）
_CHECKIN_APPEND_LEGACY = "追加到时间轴"
_CHECKIN_APPEND_PREFIX = "追加"


def _normalize_checkin_meta_token(t: str) -> str:
    """整句去空白与常见句末标点，用于与元指令表精确匹配。"""
    return (t or "").strip().rstrip("。！？!?.").strip()


def _checkin_meta_bypass_phrases(cfg: dict) -> frozenset[str]:
    """默认 + ``timeline.checkin_meta_bypass_phrases`` 合并（均已规范化）。"""
    base = {
        "查看时间轴",
        "看时间轴",
        "查时间轴",
        "查看日志",
        "看日志",
        "查看日记",
        "查看记录",
        "忽略",
        "稍后",
        "等会",
    }
    out = {_normalize_checkin_meta_token(x) for x in base if _normalize_checkin_meta_token(x)}
    tl = cfg.get("timeline") or {}
    extra = tl.get("checkin_meta_bypass_phrases")
    if isinstance(extra, list):
        for x in extra:
            if isinstance(x, str):
                nx = _normalize_checkin_meta_token(x)
                if nx:
                    out.add(nx)
    return frozenset(out)


def _is_checkin_meta_query(cfg: dict, body_raw: str) -> bool:
    """签到回填窗口内：纯查看/忽略类短句不写时间轴，交给主路由。"""
    n = _normalize_checkin_meta_token(body_raw)
    if not n:
        return False
    return n in _checkin_meta_bypass_phrases(cfg)


def _parse_checkin_reply_text(raw: str) -> tuple[str, bool]:
    """``(正文, 是否含显式追加意图)``；去掉标记并压空白。

    口径：以「追加」开头，或句中含旧版「追加到时间轴」。
    """
    t = (raw or "").strip()
    if not t:
        return "", False
    if _CHECKIN_APPEND_LEGACY in t:
        collapsed = " ".join(t.replace(_CHECKIN_APPEND_LEGACY, " ").split())
        return collapsed.strip(), True
    if t.startswith(_CHECKIN_APPEND_PREFIX):
        rest = t[len(_CHECKIN_APPEND_PREFIX) :].lstrip(" \t　：:，,、")
        return rest.strip(), True
    return t, False


class TimelineHooksMixin:
    async def _compact_checkin_reply(self, raw: str) -> str:
        """checkin 回填正文：按 timeline.compact_enabled 决定是否走 ACP 短版。"""
        return await compact_timeline_line(
            self.acp,
            self.cfg,
            self.unified_session_id,
            raw,
            trace_tag="checkin_compact",
        )

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

        body_raw = (text or "").strip()
        if not body_raw:
            return False

        if _is_checkin_meta_query(self.cfg, body_raw):
            log_flow_event(
                stage="timeline",
                route="checkin_skip_meta_query",
                user_text=body_raw,
                from_user=from_user,
                extra={"slot": slot},
            )
            return False

        body_clean, explicit_append = _parse_checkin_reply_text(body_raw)
        tl = self.cfg.get("timeline") or {}
        allow_append_when_filled = bool(tl.get("checkin_write_if_filled", False))
        existing = get_slot_body(self.cfg, slot, dt=tdt)

        if existing and not allow_append_when_filled and not explicit_append:
            log_flow_event(
                stage="timeline",
                route="checkin_skip_filled_no_append",
                user_text=body_raw,
                from_user=from_user,
                extra={"slot": slot, "existing": existing[:120]},
            )
            await self.wx.send_text(
                f"该格 {slot} 已有记录；如需追加请以「追加」开头写正文（例：追加 吃了药）。",
                from_user,
                context_token,
            )
            pop_checkin_expect(self.cfg, from_user)
            return True

        if existing and not allow_append_when_filled and explicit_append and not body_clean:
            await self.wx.send_text(
                "请写出要追加到该格的正文～",
                from_user,
                context_token,
            )
            return True

        source_for_slot = (
            body_clean if explicit_append else body_raw
        ).strip()
        if not source_for_slot:
            return False

        pop_checkin_expect(self.cfg, from_user)
        compact_body = await self._compact_checkin_reply(source_for_slot)
        if not compact_body:
            await self.wx.send_text(
                "压缩后为空，本条未写入时间轴。", from_user, context_token
            )
            return True

        upsert_timeline_slot(self.cfg, slot, compact_body, dt=tdt)
        await self.wx.send_text(
            f"已记到时间轴 {slot}～\n{compact_body}",
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
