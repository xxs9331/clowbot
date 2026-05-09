"""半小时时间轴 checkin：与 reminders 独立，仅读 vault + AI 建议。

每次轮询使用 slot_at(now) 获取当前半格，不再跳过已填格：
- 已填格 → 推送概括，用户可不回复；并清除该用户的 checkin_expect（避免上一轮空格催填残留导致误写）
- 空格 → 推送请求记录，设置 checkin_expect 等待用户回填
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from config import PACKAGE_ROOT
from utils.flow_log import log_flow_event
from utils.log_sync import get_log_path
from utils.timeline_state import (
    get_last_ping,
    get_state,
    pop_checkin_expect,
    set_checkin_expect,
    set_last_ping,
    set_state,
)
from utils.timeline_sync import (
    ensure_timeline_file,
    get_last_non_empty_slot,
    get_slot_body,
    slot_at,
    timeline_enabled,
    timeline_path,
)


PROMPTS_DIR = PACKAGE_ROOT / "prompts"
_CHECKIN_FILE = "checkin.md"

# ── 默认模板（fallback，与 checkin.md 内容保持同步） ──────────────────────

_DEFAULT_CHECKIN = """你是中文个人助理，负责「半小时状态 checkin」话术。
输出契约：只输出 1 行中文微信消息，30～80 字为宜；不要 markdown；不要 JSON。
场景：用户在 {slot} 这个时间点已有记录（{slot_preview}），请简要概括并附 1 条推进建议（可点名下一步拖延项，但不要人身攻击）。
语气要像是在轻轻确认，不是在催填空 — 该格已有内容，不需要用户回复即可。
若用户追问是否写入，请提醒：该格已有内容，默认不会自动追加到时间轴；如需追加请以「追加」开头写一句正文。
不要写成「备忘录提醒」或「到点闹钟」语气；那是另一套系统。

当前时间：{time}
检查的时间节点：{slot}
该格已有内容：{slot_body}
时间轴最近一条非空：{last_timeline_line}

【时间轴节选】
{timeline_tail}

【日记节选】
{diary_tail}

【生活日志节选】
{life_log_tail}

【项目总览节选】
{projects_tail}

请直接输出该行消息：

（补充说明：你稍后可能会收到用户回复。如果回复是简短的活动描述如「打游戏」「洗澡」「到实验室了」，那很可能是在回你刚才的 checkin 提问，请通过 unified 决策将内容写入时间轴对应格并回复确认。如果回复是长句、提问、表情、或明显在继续之前的聊天话题，请不要写入时间轴，正常回复即可。）

---

你是中文个人助理，负责「半小时状态 checkin」话术。
输出契约：只输出 1 行中文微信消息，30～80 字为宜；不要 markdown；不要 JSON。
场景：用户在 {slot} 这个时间点还没有记录，请温和询问「你在 {slot} 这个时间点在做什么」，并给 1 条可执行小建议（可点名拖延项，但不要人身攻击）。
不要写成「备忘录提醒」或「到点闹钟」语气；那是另一套系统。

当前时间：{time}
检查的时间节点：{slot}
时间轴最近一条非空：{last_timeline_line}

【时间轴节选】
{timeline_tail}

【日记节选】
{diary_tail}

【生活日志节选】
{life_log_tail}

【项目总览节选】
{projects_tail}

请直接输出该行消息：

（补充说明：你稍后可能会收到用户回复。如果回复是简短的活动描述如「打游戏」「洗澡」「到实验室了」，那很可能是在回你刚才的 checkin 提问，请通过 unified 决策将内容写入时间轴对应格并回复确认。如果回复是长句、提问、表情、或明显在继续之前的聊天话题，请不要写入时间轴，正常回复即可。）"""


def _load_checkin_prompts() -> tuple[str, str]:
    """从 checkin.md 读取联合模板，按 --- 拆分返回 (filled, empty)。

    每次调用都重新读取文件，确保热更新。
    文件缺失/读取失败/拆分异常 → 返回内嵌默认值。
    """
    path = PROMPTS_DIR / _CHECKIN_FILE
    try:
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            parts = content.split("\n---\n", 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    except OSError:
        pass
    parts = _DEFAULT_CHECKIN.split("\n---\n", 1)
    return parts[0].strip(), parts[1].strip()


def _ceil_to_next_half_hour(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    extra = 30 - (dt.minute % 30)
    if extra == 0:
        extra = 30
    return dt + timedelta(minutes=extra)


def _seconds_until_next_boundary() -> float:
    nxt = _ceil_to_next_half_hour(datetime.now())
    return max((nxt - datetime.now()).total_seconds(), 1.0)


def _checkin_sleep_seconds(cfg: dict) -> float:
    """``checkin_poll_interval_sec`` > 0 时按固定间隔；否则对齐下一整点半点。"""
    tl = cfg.get("timeline") or {}
    try:
        iv = float(tl.get("checkin_poll_interval_sec") or 0)
    except (TypeError, ValueError):
        iv = 0.0
    if iv > 0:
        return max(10.0, min(iv, 3600.0))
    return _seconds_until_next_boundary()


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


async def _build_summary_message(handler, *, slot: str, slot_body: str, now_str: str) -> str:
    """已填格概括推送：告知当前格已有内容，用户可不回复。"""
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
        "slot_body": slot_body,
        "last_timeline_line": get_last_non_empty_slot(cfg, today),
        "timeline_tail": _snip_file(tl, 3500),
        "diary_tail": _snip_file(diary, 2000),
        "life_log_tail": _snip_file(life, 2000),
        "projects_tail": _snip_file(overview, 1200),
    }
    bot_cfg = cfg.get("bot") or {}
    max_len = int(bot_cfg.get("max_reply_length") or 2000)

    slot_preview = slot_body[:400] + ("…" if len(slot_body) > 400 else "")
    filled_tmpl, _ = _load_checkin_prompts()
    prompt = filled_tmpl.format(
        time=ctx["time"],
        slot=ctx["slot_checked"],
        slot_body=ctx["slot_body"],
        slot_preview=slot_preview,
        last_timeline_line=ctx["last_timeline_line"],
        timeline_tail=ctx["timeline_tail"],
        diary_tail=ctx["diary_tail"],
        life_log_tail=ctx["life_log_tail"],
        projects_tail=ctx["projects_tail"],
    )

    sid = getattr(handler, "unified_session_id", "") or getattr(handler, "session_id", "")
    if not sid or not getattr(handler, "acp", None):
        log_flow_event(
            stage="checkin",
            route="ai_suggest_fallback",
            user_text="",
            extra={"reason": "no_session"},
        )
        short = slot_body[:120] + ("…" if len(slot_body) > 120 else "")
        return (
            f"{slot} 已记录：{short}，继续加油～有需要随时说。"
            "如需追加，请以「追加」开头写正文（例：追加 吃了药）。"
        )

    try:
        timeout = float((cfg.get("timeline") or {}).get("checkin_ai_timeout_sec") or 12.0)
    except (TypeError, ValueError):
        timeout = 12.0

    try:
        reply, _ = await asyncio.wait_for(
            handler.acp.prompt(sid, prompt, trace_tag="checkin_summary"),
            timeout=timeout,
        )
        body = (reply or "").strip().replace("\n", " ")
        if not body:
            raise ValueError("empty reply")
        body = body[: min(max_len, 500)]
        log_flow_event(
            stage="checkin",
            route="ai_summary_ok",
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
        short = slot_body[:120] + ("…" if len(slot_body) > 120 else "")
        return (
            f"{slot} 已记录：{short}，继续加油～有需要随时说。"
            "如需追加，请以「追加」开头写正文（例：追加 吃了药）。"
        )


async def _build_empty_slot_message(handler, *, slot: str, now_str: str) -> str:
    """空格请求记录：请求用户一句话描述当前在做什么。"""
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

    _, empty_tmpl = _load_checkin_prompts()
    prompt = empty_tmpl.format(
        time=ctx["time"],
        slot=ctx["slot_checked"],
        last_timeline_line=ctx["last_timeline_line"],
        timeline_tail=ctx["timeline_tail"],
        diary_tail=ctx["diary_tail"],
        life_log_tail=ctx["life_log_tail"],
        projects_tail=ctx["projects_tail"],
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
            f"{slot} 这个时间点还没有记录～方便用一句话补一下在做什么吗？"
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
            f"{slot} 这个时间点还没有记录～简单回一句在忙什么？"
            "需要开番茄也可以说「开个番茄：xxx」。"
        )


async def _checkin_iteration(handler, *, now: datetime | None = None) -> None:
    """一轮推送：当前半格 slot_at(now)；已填概括 / 空格请求记录。

    ``now`` 供单测注入；生产传 ``None`` 时用 ``datetime.now()``。
    """
    cfg = handler.cfg
    now = now if now is not None else datetime.now()
    slot = slot_at(now)
    d_iso = now.strftime("%Y-%m-%d")

    last_d, last_s = get_last_ping(cfg)
    if last_d == d_iso and last_s == slot:
        log_flow_event(
            stage="checkin",
            route="poll_skip_dup",
            user_text="",
            extra={"slot": slot, "note": "already_pinged"},
        )
        return

    ensure_timeline_file(cfg, now)
    slot_body = get_slot_body(cfg, slot, now)
    now_str = now.strftime("%H:%M")

    uid = getattr(handler.wx, "user_id", "") or ""

    if slot_body:
        msg = await _build_summary_message(
            handler, slot=slot, slot_body=slot_body, now_str=now_str
        )
        sent = False
        if handler.wx._context_tokens:
            last_user, last_token = list(handler.wx._context_tokens.items())[-1]
            await handler.wx.send_text(msg, last_user, last_token)
            # 已填格不要求回复；若仍留着上一轮「空格催填」的 expect，下一句闲聊会被误写入旧半格
            pop_checkin_expect(handler.cfg, last_user)
            sent = True
        elif uid:
            await handler.wx.send_text(msg, uid, "")
            pop_checkin_expect(handler.cfg, uid)
            sent = True
        if not sent:
            log_flow_event(
                stage="checkin",
                route="poll_trigger_filled_summary",
                user_text="",
                extra={"slot": slot, "error": "no_wx_target"},
            )
            return
        log_flow_event(
            stage="checkin",
            route="poll_trigger_filled_summary",
            user_text="",
            extra={"slot": slot, "preview": msg[:80]},
        )
    else:
        msg = await _build_empty_slot_message(handler, slot=slot, now_str=now_str)
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
                route="poll_trigger_empty_request",
                user_text="",
                extra={"slot": slot, "error": "no_wx_target"},
            )
            return
        log_flow_event(
            stage="checkin",
            route="poll_trigger_empty_request",
            user_text="",
            extra={"slot": slot, "preview": msg[:80]},
        )

    set_last_ping(cfg, d_iso, slot)


async def checkin_loop(handler):
    """整点/半点对齐（默认）或固定间隔轮询；使用 slot_at(now) 获取当前半格。

    已填格 → 概括推送（不设 checkin_expect，用户可不回复）
    空格 → 请求记录 + 设 checkin_expect（900 秒窗口，回复写入触发格）
    同一天同一 slot 只推一次（last_ping 去重）
    """
    while True:
        try:
            cfg = handler.cfg
            await asyncio.sleep(_checkin_sleep_seconds(cfg))
            tl_cfg = cfg.get("timeline") or {}
            if not timeline_enabled(cfg) or not bool(tl_cfg.get("checkin_enabled")):
                await asyncio.sleep(30)
                continue

            now = datetime.now()
            # 早上 7 点自动唤醒（每天重置，即使昨晚手动 /睡觉）
            if now.hour == 7 and now.minute < 30:
                if get_state(cfg) == "sleep":
                    set_state(cfg, "active")
                    log_flow_event(
                        stage="checkin",
                        route="auto_wake_7am",
                        user_text="",
                        extra={"note": "daily_auto_wake"},
                    )
                    print("[Bot] 早上 7 点，自动唤醒 checkin")
                await asyncio.sleep(60)
                continue
            # 凌晨 2 点强制休眠
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

            await _checkin_iteration(handler, now=now)

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Bot] checkin 循环错误: {e}")
            log_flow_event(
                stage="checkin",
                route="loop_error",
                user_text="",
                extra={"loop_error": str(e)[:200]},
            )
            await asyncio.sleep(5)
