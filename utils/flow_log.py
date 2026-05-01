"""单条消息处理流程日志：路由、ACP 调用、字符/流事件统计（非精确 token）"""

import json
from datetime import datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PACKAGE_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# 预览截断长度（避免单条日志过大）
PREVIEW_PROMPT = 1200
PREVIEW_REPLY = 2000
PREVIEW_REASONING = 1500


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _short_sid(sid: str, n: int = 12) -> str:
    if not sid:
        return ""
    return sid if len(sid) <= n else f"{sid[:n]}…"


def _mask_user(uid: str) -> str:
    if not uid:
        return ""
    if len(uid) <= 4:
        return "****"
    return f"…{uid[-4:]}"


def _snip(s: str, max_len: int) -> str:
    if not s:
        return ""
    s = s.replace("\r\n", "\n")
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"\n…(共 {len(s)} 字符，已截断)"


def snapshot_todo_queue(queues: dict, user_id: str) -> dict:
    st = queues.get(user_id)
    if not st:
        return {"active": False}
    tasks = st.get("tasks") or []
    idx = int(st.get("idx", 0))
    cur = tasks[idx] if idx < len(tasks) else ""
    return {
        "active": True,
        "idx": idx,
        "total": len(tasks),
        "current": cur[:80] if cur else "",
    }


def log_flow_event(
    *,
    stage: str,
    route: str,
    user_text: str = "",
    from_user: str = "",
    session_id: str = "",
    extra: dict | None = None,
) -> None:
    """记录路由与业务阶段（不含完整模型 I/O，模型见 log_acp_turn）"""
    today = datetime.now().strftime("%Y-%m-%d")
    path = LOG_DIR / f"flow-{today}.log"
    lines = [
        "─" * 52,
        f"[{_ts()}] stage={stage} route={route}",
    ]
    if user_text:
        lines.append(f"  user: {_snip(user_text, 300)}")
    if from_user:
        lines.append(f"  from: {_mask_user(from_user)}")
    if session_id:
        lines.append(f"  session: {_short_sid(session_id)}")
    if extra:
        try:
            lines.append(f"  extra: {json.dumps(extra, ensure_ascii=False, default=str)}")
        except Exception:
            lines.append(f"  extra: {str(extra)[:500]}")
    lines.append("")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def log_acp_turn(
    *,
    trace: str,
    session_id: str,
    model: str,
    prompt: str,
    reply: str,
    reasoning: str,
    meta: dict | None = None,
) -> None:
    """记录一次 session/prompt 的输入输出与统计"""
    today = datetime.now().strftime("%Y-%m-%d")
    path = LOG_DIR / f"flow-{today}.log"
    m = meta or {}
    p_len = len(prompt or "")
    r_len = len(reply or "")
    rs_len = len(reasoning or "")
    lines = [
        "·" * 52,
        f"[{_ts()}] acp trace={trace} model={model}",
        f"  session: {_short_sid(session_id)}",
        f"  stats: prompt_chars={p_len} reply_chars={r_len} reasoning_chars={rs_len}"
        + (
            f" raw_events={m.get('raw_count')}"
            if m.get("raw_count") is not None
            else ""
        )
        + (
            f" notifications={m.get('notification_count')}"
            if m.get("notification_count") is not None
            else ""
        ),
        f"  prompt_preview:\n{_snip(prompt or '', PREVIEW_PROMPT)}",
        f"  reply_preview:\n{_snip(reply or '', PREVIEW_REPLY)}",
    ]
    # 链路可观测：用于区分“日志截断 / 上游截断 / 本地提前退出”
    if m.get("break_reason") is not None:
        lines.append(f"  collector_break_reason: {m.get('break_reason')}")
    if m.get("saw_final_response") is not None:
        lines.append(f"  collector_saw_final_response: {m.get('saw_final_response')}")
    if m.get("saw_end_turn") is not None:
        lines.append(f"  collector_saw_end_turn: {m.get('saw_end_turn')}")
    if m.get("update_counters"):
        try:
            lines.append(
                "  collector_update_counters: "
                + json.dumps(m.get("update_counters"), ensure_ascii=False, default=str)
            )
        except Exception:
            lines.append(f"  collector_update_counters: {str(m.get('update_counters'))[:500]}")
    if reasoning:
        lines.append(f"  reasoning_preview:\n{_snip(reasoning, PREVIEW_REASONING)}")
    else:
        lines.append("  reasoning_preview: (empty)")
    lines.append(
        f"  note: 完整推理长文另见 logs/reasoning-{today}.log（若业务路径写入）"
    )
    lines.append("")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
