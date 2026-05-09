"""定时提醒推送文案润色 — 独立 ACP 会话，失败无感回退。

首版：仅根据提醒时间 + 原始文案润色，不读取日记正文（reminder_polish_context_chars 默认 0）。
Session id 缓存在 acp 实例上（与 intent_llm 一致），不挂 handler、不用模块全局。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from utils.flow_log import log_flow_event

from config import PACKAGE_ROOT

_REMIND_POLISH_SESSION_ATTR = "_remind_polish_session_id"
_REMIND_POLISH_SESSION_LOCK_ATTR = "_remind_polish_session_lock"
_REMIND_POLISH_SESSION_PRIMED_ATTR = "_remind_polish_session_primed"

PROMPTS_DIR = PACKAGE_ROOT / "prompts"
_REMIND_POLISH_FILE = "remind_polish.md"

_DEFAULT_REMIND_POLISH = """你是中文生活助手，专门把「定时提醒」的推送正文写得更自然、温暖一点，每次语气可以略有变化，但不要夸张、不要鸡汤长文。

输出契约（必须遵守）：
- 只输出一小段中文正文：这是微信消息里紧跟在固定前缀后面的「内容部分」。
- 固定前缀由系统拼接，形如「⏰ 提醒（HH:MM）：」。你**不要**输出该前缀、不要输出时间、不要输出「⏰」。
- 单行输出：不要换行、不要 markdown、不要用代码块。
- 必须保留用户给定提醒要点的语义（可以换说法，但不要改成无关内容）。
- 长度建议不超过 80 个汉字；宁短勿长。

---

按你已加载的输出契约，为本轮提醒写「内容部分」仅一行。

{context_block}提醒时间（已由系统展示，你不要再写一遍）：{hhmm}
提醒要点（必须体现语义）：{anchor_text}
直接输出该行正文："""


def _load_remind_polish_prompts() -> tuple[str, str]:
    """从 remind_polish.md 读取，按 --- 拆分返回 (prime, turn)。热更新。"""
    path = PROMPTS_DIR / _REMIND_POLISH_FILE
    try:
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            parts = content.split("\n---\n", 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    except OSError:
        pass
    parts = _DEFAULT_REMIND_POLISH.split("\n---\n", 1)
    return parts[0].strip(), parts[1].strip()


def _build_turn_prompt(hhmm: str, anchor_text: str, _context_block: str) -> str:
    ctx = (_context_block or "").strip()
    ctx_part = f"\n【参考上下文】\n{ctx}\n" if ctx else ""
    return (
        "按你已加载的输出契约，为本轮提醒写「内容部分」仅一行。\n"
        f"{ctx_part}"
        f"提醒时间（已由系统展示，你不要再写一遍）：{hhmm}\n"
        f"提醒要点（必须体现语义）：{anchor_text}\n"
        "直接输出该行正文："
    )


async def _ensure_session(acp: Any, model: str) -> str:
    if not hasattr(acp, _REMIND_POLISH_SESSION_LOCK_ATTR):
        setattr(acp, _REMIND_POLISH_SESSION_LOCK_ATTR, asyncio.Lock())
    lock: asyncio.Lock = getattr(acp, _REMIND_POLISH_SESSION_LOCK_ATTR)
    async with lock:
        sid = getattr(acp, _REMIND_POLISH_SESSION_ATTR, "") or ""
        if not sid:
            sid = await acp.create_session(model)
            setattr(acp, _REMIND_POLISH_SESSION_ATTR, sid)
            setattr(acp, _REMIND_POLISH_SESSION_PRIMED_ATTR, False)

        primed = bool(getattr(acp, _REMIND_POLISH_SESSION_PRIMED_ATTR, False))
        if not primed:
            prime_tmpl, _ = _load_remind_polish_prompts()
            await acp.prompt(
                sid, prime_tmpl, trace_tag="remind_polish_prime"
            )
            setattr(acp, _REMIND_POLISH_SESSION_PRIMED_ATTR, True)
        return sid


async def _polish_body_once(
    acp: Any,
    *,
    model: str | None,
    hhmm: str,
    anchor_text: str,
    context_block: str,
    timeout_sec: float,
) -> str | None:
    """调用模型得到 body；失败返回 None。"""
    if not anchor_text or not str(anchor_text).strip():
        return None
    use_model = model or getattr(acp, "model", "") or ""
    try:
        sid = await _ensure_session(acp, use_model)
    except Exception as e:  # noqa: BLE001
        log_flow_event(
            stage="reminder",
            route="remind_polish_session_error",
            user_text=f"{hhmm} {anchor_text}",
            extra={"error": str(e)[:200]},
        )
        return None

    ctx = (context_block or "").strip()
    ctx_part = f"\n【参考上下文】\n{ctx}\n" if ctx else ""
    _, turn_tmpl = _load_remind_polish_prompts()
    prompt = turn_tmpl.format(context_block=ctx_part, hhmm=hhmm, anchor_text=anchor_text.strip())
    try:
        reply, _ = await asyncio.wait_for(
            acp.prompt(sid, prompt, trace_tag="remind_polish"),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        log_flow_event(
            stage="reminder",
            route="remind_polish_timeout",
            user_text=f"{hhmm} {anchor_text}",
            session_id=sid,
            extra={"timeout_sec": timeout_sec},
        )
        return None
    except Exception as e:  # noqa: BLE001
        log_flow_event(
            stage="reminder",
            route="remind_polish_error",
            user_text=f"{hhmm} {anchor_text}",
            session_id=sid,
            extra={"error": str(e)[:200]},
        )
        return None

    body = _normalize_body(reply or "")
    if not body:
        log_flow_event(
            stage="reminder",
            route="remind_polish_empty_reply",
            user_text=f"{hhmm} {anchor_text}",
            session_id=sid,
        )
        return None
    return body


async def build_reminder_wx_message(
    acp: Any,
    bot_cfg: dict,
    *,
    remind_time: str,
    remind_text: str,
) -> str:
    """返回完整微信文本（含前缀），已按 max_reply_length 截断且前缀完整。"""
    prefix = f"⏰ 提醒（{remind_time}）："
    try:
        max_len = int(bot_cfg.get("max_reply_length") or 2000)
    except (TypeError, ValueError):
        max_len = 2000

    anchor = (remind_text or "").strip()
    if not bot_cfg.get("reminder_polish_enabled") or acp is None:
        return finalize_reminder_text(prefix, anchor, max_len)

    try:
        timeout_sec = float(bot_cfg.get("reminder_polish_timeout_sec") or 8.0)
    except (TypeError, ValueError):
        timeout_sec = 8.0
    if timeout_sec <= 0:
        timeout_sec = 8.0

    model_raw = bot_cfg.get("reminder_polish_model")
    model = (str(model_raw).strip() if model_raw is not None else "") or None

    # 首版：不读日记；context_chars 仅预留配置位（>0 时仍不传正文，避免误把大段日志发出）
    context_block = ""

    polished = await _polish_body_once(
        acp,
        model=model,
        hhmm=remind_time,
        anchor_text=anchor,
        context_block=context_block,
        timeout_sec=timeout_sec,
    )
    body = polished if polished else anchor
    if polished:
        log_flow_event(
            stage="reminder",
            route="remind_polish_ok",
            user_text=f"{remind_time} {anchor}",
            extra={"preview": body[:120]},
        )
    return finalize_reminder_text(prefix, body, max_len)
