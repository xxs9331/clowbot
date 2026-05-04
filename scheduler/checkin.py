"""半小时时间轴 checkin：与 reminders 独立，仅读 vault + AI 建议。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from utils.flow_log import log_flow_event
from utils.log_sync import get_log_path
from utils.timeline_state import (
    get_last_ping,
    get_state,
    set_checkin_expect,
    set_last_ping,
    set_state,
)
from utils.timeline_sync import (
    get_last_non_empty_slot,
    is_slot_empty,
    last_completed_slot_at_boundary,
    timeline_enabled,
    timeline_path,
)


def _ceil_to_next_half_hour(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    extra = 30 - (dt.minute % 30)
    if extra == 0:
        extra = 30
    return dt + timedelta(minutes=extra)


def _seconds_until_next_boundary() -> float:
    nxt = _ceil_to_next_half_hour(datetime.now())
    return max((nxt - datetime.now()).total_seconds(), 1.0)


def _snip_file(path: Path, max_chars: int) -> str:
    if not path.exists():
        return ""
    try:
        s = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(s) <= max_chars:
        return s
    return s[-max_chars:]


def _project_overview_path(handler) -> Path:
    v = handler.cfg.get("vault") or {}
    root = Path(str(v.get("root") or "").strip()).resolve()
    tl = handler.cfg.get("timeline") or {}
    rel = str(tl.get("project_overview_path") or "").strip()
    return (root / rel.replace("\\", "/")).resolve()


def _diary_path(handler, today: datetime) -> Path:
    v = handler.cfg.get("vault") or {}
    root = Path(str(v.get("root") or "").strip()).resolve()
    rel = str(v.get("diary_dir") or "").strip()
    y = today.strftime("%Y")
    m = today.strftime("%m")
    d = today.strftime("%Y-%m-%d")
    return root / rel.replace("\\", "/") / y / m / f"{d}.md"


async def _build_checkin_message(handler, *, slot: str, now_str: str) -> str:
    """读三文件摘要 + 项目总览，生成一条微信建议（失败则固定兜底）。"""
    cfg = handler.cfg
    today = datetime.now()
    vault = cfg.get("vault") or {}
    root = Path(str(vault.get("root") or "").strip()).resolve()

    tl = timeline_path(cfg, today)
    life = get_log_path(str(root), str(vault.get("daily_log_dir") or ""), today)
    diary = _diary_path(handler, today)
    overview = _project_overview_path(handler)

    ctx = {
        "time": now_str,
        "slot_checked": slot,
        "last_timeline_line": get_last_non_empty_slot(cfg, today),
        "timeline_tail": _snip_file(tl, 3500),
        "diary_tail": _snip_file(diary, 2000),
        "life_log_tail": _snip_file(life, 2000),
        "projects_tail": _snip_file(overview, 1200),
    }
    bot_cfg = cfg.get("bot") or {}
    max_len = int(bot_cfg.get("max_reply_length") or 2000)

    prompt = (
        "你是中文个人助理，负责「半小时状态 checkin」话术。\n"
        "输出契约：只输出 1 行中文微信消息，30～80 字为宜；不要 markdown；不要 JSON。\n"
        "目标：用户刚过去的半小时在时间轴该格是空的，请结合下面上下文，"
        "温和询问「过去半小时做了什么」，并给 1 条可执行小建议（可点名拖延项，但不要人身攻击）。\n"
        "不要写成「备忘录提醒」或「到点闹钟」语气；那是另一套系统。\n\n"
        f"当前时间：{ctx['time']}\n"
        f"检查的半格起点：{ctx['slot_checked']}\n"
        f"时间轴最近一条非空：{ctx['last_timeline_line']}\n\n"
        f"【时间轴节选】\n{ctx['timeline_tail']}\n\n"
        f"【日记节选】\n{ctx['diary_tail']}\n\n"
        f"【生活日志节选】\n{ctx['life_log_tail']}\n\n"
        f"【项目总览节选】\n{ctx['projects_tail']}\n\n"
        "请直接输出该行消息："
    )

    sid = getattr(handler, "unified_session_id", "") or getattr(handler, "session_id", "")
    if not sid or not getattr(handler, "acp", None):
        log_flow_event(
            stage="checkin",
            route="ai_suggest_fallback",
            user_text="",
            extra={"reason": "no_session"},
        )
        return (
            f"过去半小时（{slot} 起）时间轴还是空的～方便用一句话补一下在做什么吗？"
            "若刚在专注，也可以直接回「番茄中」。"
        )

    try:
        timeout = float((cfg.get("timeline") or {}).get("checkin_ai_timeout_sec") or 12.0)
    except (TypeError, ValueError):
        timeout = 12.0

    try:
        reply, _ = await asyncio.wait_for(
            handler.acp.prompt(sid, prompt, trace_tag="checkin_suggest"),
            timeout=timeout,
        )
        body = (reply or "").strip().replace("\n", " ")
        if not body:
            raise ValueError("empty reply")
        body = body[: min(max_len, 500)]
        log_flow_event(
            stage="checkin",
            route="ai_suggest_ok",
            user_text="",
            session_id=sid,
            extra={"preview": body[:120]},
        )
        return body
    except Exception as e:
        log_flow_event(
            stage="checkin",
            route="ai_suggest_fallback",
            user_text="",
            session_id=sid,
            extra={"error": str(e)[:200]},
        )
        return (
            f"过去半小时（从 {slot} ）时间轴还空着～简单回一句在忙什么？"
            "需要开番茄也可以说「开个番茄：xxx」。"
        )


async def checkin_loop(handler):
    """整点/半点对齐；仅 active；空格触发一次建议。"""
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_boundary())

            cfg = handler.cfg
            tl_cfg = cfg.get("timeline") or {}
            if not timeline_enabled(cfg) or not bool(tl_cfg.get("checkin_enabled")):
                await asyncio.sleep(30)
                continue

            now = datetime.now()
            if now.hour == 2 and now.minute < 30:
                if get_state(cfg) == "active":
                    set_state(cfg, "sleep")
                    log_flow_event(
                        stage="checkin",
                        route="auto_sleep_2am",
                        user_text="",
                        extra={"note": "forced_sleep"},
                    )
                await asyncio.sleep(60)
                continue

            if get_state(cfg) != "active":
                await asyncio.sleep(120)
                continue

            slot = last_completed_slot_at_boundary(now)
            d_iso = now.strftime("%Y-%m-%d")
            last_d, last_s = get_last_ping(cfg)
            if last_d == d_iso and last_s == slot:
                log_flow_event(
                    stage="checkin",
                    route="poll_skip_filled",
                    user_text="",
                    extra={"slot": slot, "note": "already_pinged"},
                )
                continue

            if not is_slot_empty(cfg, slot, now):
                log_flow_event(
                    stage="checkin",
                    route="poll_skip_filled",
                    user_text="",
                    extra={"slot": slot},
                )
                continue

            now_str = now.strftime("%H:%M")
            msg = await _build_checkin_message(handler, slot=slot, now_str=now_str)

            uid = getattr(handler.wx, "user_id", "") or ""
            if handler.wx._context_tokens:
                last_user, last_token = list(handler.wx._context_tokens.items())[-1]
                await handler.wx.send_text(msg, last_user, last_token)
                set_checkin_expect(handler.cfg, last_user, d_iso, slot)
            elif uid:
                await handler.wx.send_text(msg, uid, "")
                set_checkin_expect(handler.cfg, uid, d_iso, slot)
            else:
                log_flow_event(
                    stage="checkin",
                    route="poll_trigger_empty",
                    user_text="",
                    extra={"slot": slot, "error": "no_wx_target"},
                )
                continue

            set_last_ping(cfg, d_iso, slot)
            log_flow_event(
                stage="checkin",
                route="poll_trigger_empty",
                user_text="",
                extra={"slot": slot, "preview": msg[:80]},
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Bot] checkin 循环错误: {e}")
            log_flow_event(
                stage="checkin",
                route="ai_suggest_fallback",
                user_text="",
                extra={"loop_error": str(e)[:200]},
            )
            await asyncio.sleep(5)
