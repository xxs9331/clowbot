"""生活记录 Coach：把已发生事件按分类追加到 `## 📝 记录` 节。

写入由 record-coach SKILL 完成，本模块只负责拼 prompt + 调 ACP + 回微信。
"""

from __future__ import annotations

import re
from datetime import datetime

from config import _log_reasoning
from handlers.dispatcher import register_tool_handler
from utils.coach_tools import OUTPUT_WRITE_CONFIRM, build_coach_write_prompt
from utils.flow_log import log_flow_event
from utils.log_sync import append_to_markdown_section, get_log_path
from utils.time_utils import time_str
from utils.timeline_compact import compact_timeline_line
from utils.tool_names import DOMAIN_RECORD, TOOL_RECORD_ADD

_EVENT_HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _effective_hhmm(payload: dict) -> str:
    """用户/模型给出的发生钟点；合法 HH:MM 则用，否则回退当前时刻。"""
    raw = str(payload.get("event_hhmm") or "").strip()
    if not raw or not _EVENT_HHMM_RE.match(raw):
        return time_str()
    parts = raw.split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return time_str()
    if h < 0 or h > 23 or m < 0 or m > 59:
        return time_str()
    return f"{h:02d}:{m:02d}"


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
        clock_hm = _effective_hhmm(payload)
        v = self.cfg["vault"]

        # === Python 直写（优先） ===
        log_path = get_log_path(v["root"], v["daily_log_dir"])
        line = f"- [x] {clock_hm} {text} （{category}）"
        try:
            append_to_markdown_section(log_path, "## 📝 记录", line)
            await self.wx.send_text(
                reply or f"已记录 {category}：{text}", from_user, context_token
            )
            try:
                from utils.timeline_sync import (
                    slot_for_hhmm,
                    timeline_enabled,
                    upsert_timeline_slot,
                )

                if timeline_enabled(self.cfg):
                    if event_date:
                        try:
                            tdt = datetime.strptime(event_date, "%Y-%m-%d")
                        except ValueError:
                            tdt = datetime.now()
                    else:
                        tdt = datetime.now()
                    slot = slot_for_hhmm(clock_hm, tdt)
                    tl_raw = str(payload.get("timeline_line") or "").strip()
                    line_src = tl_raw if tl_raw else (f"{category}·{text}" if category else text)
                    line = await compact_timeline_line(
                        self.acp,
                        self.cfg,
                        self.unified_session_id,
                        line_src,
                        trace_tag="record_timeline_compact",
                    )
                    if line:
                        upsert_timeline_slot(self.cfg, slot, line, dt=tdt)
            except Exception as e:
                log_flow_event(
                    stage="timeline",
                    route="write_fail",
                    user_text=(text or "")[:120],
                    from_user=from_user,
                    extra={"source": "record_coach_dual_write", "error": str(e)[:200]},
                )
            return True
        except Exception:
            pass

        # === 原有 LLM 路径（保留） ===
        prompt = build_coach_write_prompt(
            DOMAIN_RECORD,
            vault_root=v["root"],
            daily_log_dir=v["daily_log_dir"],
            payload={
                "op": "add",
                "text": text,
                "category": category,
                "now_hhmm": clock_hm,
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
        try:
            from utils.timeline_sync import (
                slot_for_hhmm,
                timeline_enabled,
                upsert_timeline_slot,
            )

            if timeline_enabled(self.cfg):
                if event_date:
                    try:
                        tdt = datetime.strptime(event_date, "%Y-%m-%d")
                    except ValueError:
                        tdt = datetime.now()
                else:
                    tdt = datetime.now()
                slot = slot_for_hhmm(clock_hm, tdt)
                tl_raw = str(payload.get("timeline_line") or "").strip()
                line_src = tl_raw if tl_raw else (f"{category}·{text}" if category else text)
                line = await compact_timeline_line(
                    self.acp,
                    self.cfg,
                    self.unified_session_id,
                    line_src,
                    trace_tag="record_timeline_compact",
                )
                if line:
                    upsert_timeline_slot(self.cfg, slot, line, dt=tdt)
        except Exception as e:
            log_flow_event(
                stage="timeline",
                route="write_fail",
                user_text=(text or "")[:120],
                from_user=from_user,
                extra={"source": "record_coach_dual_write", "error": str(e)[:200]},
            )
        return True


register_tool_handler(TOOL_RECORD_ADD, RecordCoachMixin._coach_record_add)
