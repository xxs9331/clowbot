"""把小模型分类结果翻译成 dispatcher 能消费的 decision dict。

设计要点：
- 仅做"明确无歧义"的 intent → tool 映射；query_* / none / 不可执行的 intent 一律返回 None
  让上层走原 unified 兜底
- 模板槽位防御：template_name 是泛词时拒绝（_GENERIC_TEMPLATE_NAMES）
- 不解析时间/日期：复杂解析交给原 detect_intent / unified；本桥只做"已经清晰"的最后一公里
"""

from __future__ import annotations

from utils.tool_names import (
    TOOL_RECORD_ADD,
    TOOL_REMIND_ADD,
    TOOL_TODO_ABANDON_CURRENT,
    TOOL_TODO_DONE_CURRENT,
    TOOL_TODO_MERGE_NEW_ITEMS,
    TOOL_TODO_NEXT,
    TOOL_TODO_NOT_DONE,
    TOOL_TODO_REORDER,
    TOOL_TODO_SKIP_CURRENT,
)

# 这些"模板名"实质是泛词，不能作为有效 template_name
_GENERIC_TEMPLATE_NAMES = frozenset({"模板", "待办模板", "流程模板", "模版", "待办模版"})


def _todo_add_decision(slots: dict) -> dict | None:
    template = (slots.get("template_name") or "").strip()
    if template:
        if template in _GENERIC_TEMPLATE_NAMES:
            return None
        # 走 todo coach 已有模板展开逻辑（_expand_template_tasks）
        return {
            "tool": TOOL_TODO_MERGE_NEW_ITEMS,
            "payload": {"tasks": [template]},
            "reply": "",
        }
    text = (slots.get("text") or "").strip()
    if not text:
        return None
    # 多项分隔交给 todo coach 上游已有逻辑（这里只塞一项，避免重复拆分歧义）
    return {
        "tool": TOOL_TODO_MERGE_NEW_ITEMS,
        "payload": {"tasks": [text]},
        "reply": "",
    }


def _remind_add_decision(slots: dict) -> dict | None:
    text = (slots.get("text") or "").strip()
    hhmm = (slots.get("hhmm") or "").strip()
    if not text or not hhmm:
        return None
    return {
        "tool": TOOL_REMIND_ADD,
        "payload": {"text": text, "hhmm": hhmm},
        "reply": "",
    }


def _record_add_decision(slots: dict) -> dict | None:
    text = (slots.get("text") or "").strip()
    if not text:
        return None
    payload: dict = {"text": text}
    cat = (slots.get("category") or "").strip()
    if cat:
        payload["category"] = cat
    ed = (slots.get("event_date") or "").strip()
    if ed:
        payload["event_date"] = ed
    eh = (slots.get("event_hhmm") or slots.get("hhmm") or "").strip()
    if eh:
        payload["event_hhmm"] = eh
    return {"tool": TOOL_RECORD_ADD, "payload": payload, "reply": ""}


_SIMPLE_TODO_TOOLS = {
    "todo_done": TOOL_TODO_DONE_CURRENT,
    "todo_next": TOOL_TODO_NEXT,
    "todo_not_done": TOOL_TODO_NOT_DONE,
    "todo_skip": TOOL_TODO_SKIP_CURRENT,
    "todo_abandon": TOOL_TODO_ABANDON_CURRENT,
}


def intent_to_decision(intent_obj: dict | None) -> dict | None:
    """把 {intent, slots, confidence} 翻成 dispatcher decision，否则返回 None。

    返回 None 的语义：本层处理不了，调用方应继续走原 unified 兜底。
    """
    if not intent_obj:
        return None
    intent = (intent_obj.get("intent") or "").strip().lower()
    slots = intent_obj.get("slots") or {}
    if not isinstance(slots, dict):
        slots = {}

    if intent in _SIMPLE_TODO_TOOLS:
        return {"tool": _SIMPLE_TODO_TOOLS[intent], "payload": {}, "reply": ""}

    if intent == "todo_add":
        return _todo_add_decision(slots)
    if intent == "remind_add":
        return _remind_add_decision(slots)
    if intent == "record_add":
        return _record_add_decision(slots)
    if intent == "todo_reorder":
        # 重排需要 reorder 列表，小模型给不全 → 让 unified 处理
        return None

    # query_* / none / 未识别 → 让上层兜底
    return None
