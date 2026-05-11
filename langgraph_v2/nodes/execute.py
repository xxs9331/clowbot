from __future__ import annotations

import re

from utils.refresh_hooks import run_post_write_hooks

from ..services import DomainServices
from ..state import ClawBotState

# 与 checkin「简短活动描述」同量级；过长句不回填，避免误把长聊写进时间轴/记录
_MAX_BACKFILL_USER_TEXT = 200
_SLOT_HHMM = re.compile(r"^\d{2}:\d{2}$")


def _normalize_payload_for_execute(tool: str, payload: dict, user_text: str) -> dict:
    """补齐执行层需要的字段：模型常漏 text，或用 content/time 代替 text/slot。"""
    out = dict(payload)
    ut = str(user_text or "").strip()
    ut_ok = bool(ut) and len(ut) <= _MAX_BACKFILL_USER_TEXT

    if tool == "timeline.append":
        if not str(out.get("text") or "").strip():
            c = str(out.get("content") or "").strip()
            if c:
                out["text"] = c
        if not str(out.get("slot") or "").strip():
            raw_t = str(out.get("time") or "").strip()
            if raw_t and _SLOT_HHMM.match(raw_t):
                out["slot"] = raw_t
        if not str(out.get("text") or "").strip() and ut_ok:
            out["text"] = ut
    elif tool == "record.add":
        if not str(out.get("text") or "").strip():
            c = str(out.get("content") or "").strip()
            if c:
                out["text"] = c
        if not str(out.get("text") or "").strip() and ut_ok:
            out["text"] = ut

    return out


async def execute(state: ClawBotState, services: DomainServices) -> ClawBotState:
    decision = state.get("decision") or {}
    tool = str(decision.get("tool") or "none")
    payload = decision.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    payload = _normalize_payload_for_execute(
        tool, payload, str(state.get("text") or "")
    )
    handled, out = await services.execute(
        user_id=str(state.get("from_user") or ""),
        tool=tool,
        payload=payload,
    )
    # LangGraph 不经过 dispatcher：须同样触发 post_write（琐事池、提醒调度 refresh 等）
    if handled and getattr(services.vault, "cfg", None):
        run_post_write_hooks(services.vault, tool, payload)
    vault = services.vault
    if handled and getattr(vault, "_eval_mode", False):
        fn = getattr(vault, "note_eval_tool_execution", None)
        if callable(fn):
            decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
            fn(
                tool=tool,
                payload=payload,
                reply_preview=str(decision.get("reply") or "")[:240],
            )
    return {"handled": handled, "tool_result": str(out or "")}

