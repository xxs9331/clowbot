from __future__ import annotations

from utils.route_fast import build_fast_unified_decision

from ..state import ClawBotState


def _split_tasks(text: str) -> list[str]:
    raw = text.replace("：", ":")
    for prefix in ("添加待办:", "待办:", "todo:"):
        if raw.startswith(prefix):
            body = raw[len(prefix) :]
            body = body.replace("、", ",").replace("，", ",")
            return [x.strip() for x in body.split(",") if x.strip()]
    return []


async def fast_rule(state: ClawBotState) -> ClawBotState:
    text = str(state.get("text") or "")
    uid = str(state.get("from_user") or "")
    qs = list(state.get("queue_snapshot") or [])
    fake_queues = {uid: {"tasks": qs, "idx": 0}} if qs else {}
    fd = build_fast_unified_decision(text, uid, fake_queues)
    if fd:
        return {"decision": fd}
    low = text.lower()
    if text in ("做完了", "好了", "完成了", "done"):
        return {"decision": {"tool": "todo.done_current", "payload": {}, "reply": ""}}
    if text in ("下一个", "next"):
        return {"decision": {"tool": "todo.next", "payload": {}, "reply": ""}}
    tasks = _split_tasks(low)
    if tasks:
        return {
            "decision": {
                "tool": "todo.merge_new_items",
                "payload": {"tasks": tasks},
                "reply": "",
            }
        }
    return {}


def route_after_fast_rule(state: ClawBotState) -> str:
    if state.get("decision"):
        return "execute"
    return "llm_decide"
