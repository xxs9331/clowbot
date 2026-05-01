"""确定性快通道：在 _llm_unified_decide 之前将常见句式映射为统一 decision。"""

import re

from handlers.todo import (
    TOOL_DECISION_NONE,
    TOOL_DONE_CURRENT,
    TOOL_MERGE_NEW_ITEMS,
    TOOL_NEXT,
    TOOL_NOT_DONE,
)
from utils.intent import (
    INTENT_TODO,
    INTENT_TODO_DONE,
    INTENT_TODO_NEXT,
    INTENT_TODO_NOT_DONE,
    detect_intent,
)


def _split_todo_items(text: str) -> list[str]:
    """与 Handler._split_todo_items 一致，避免 handlers 循环依赖。"""
    cleaned = text.strip().strip("。.!！")
    cleaned = cleaned.replace("然后", "，").replace("再", "，")
    parts = re.split(r"[,，、;；\n]+", cleaned)
    items = []
    for part in parts:
        item = re.sub(r"^[-\d\.\)\(、\s]+", "", part).strip()
        item = item.strip("：: ")
        if item:
            items.append(item)
    return items


_META_TODO_CLARIFY = re.compile(
    r"(skill|skills|SKILL|技能|路由|元问题|怎么判定|调用.*skill|待办.*skill|走.*skill)",
    re.I,
)


def _has_active_todo_queue(user_id: str, todo_queues: dict) -> bool:
    state = todo_queues.get(user_id)
    if not state:
        return False
    idx = state.get("idx", 0)
    tasks = state.get("tasks", [])
    return bool(tasks) and idx < len(tasks)


def build_fast_unified_decision(
    text: str,
    user_id: str,
    todo_queues: dict,
) -> dict | None:
    """命中快通道时返回 tool + payload + reply（与 _apply_unified_decision 一致），否则 None。"""

    if _has_active_todo_queue(user_id, todo_queues) and _META_TODO_CLARIFY.search(text):
        return {
            "tool": TOOL_DECISION_NONE,
            "payload": {},
            "reply": "催办和动作判定由 OpenCode 工程里加载的待办 SKILL 管；我这边按当前内存队列推进。",
        }

    intent, data = detect_intent(text)

    if intent == INTENT_TODO_DONE:
        return {"tool": TOOL_DONE_CURRENT, "payload": {}, "reply": ""}
    if intent == INTENT_TODO_NEXT:
        return {"tool": TOOL_NEXT, "payload": {}, "reply": ""}
    if intent == INTENT_TODO_NOT_DONE:
        return {"tool": TOOL_NOT_DONE, "payload": {}, "reply": ""}

    if intent == INTENT_TODO and data:
        tasks = _split_todo_items(data)
        if tasks:
            return {"tool": TOOL_MERGE_NEW_ITEMS, "payload": {"tasks": tasks}, "reply": ""}

    return None
