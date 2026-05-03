"""确定性快通道：仅保留元问题澄清，业务意图交给小模型分类层。

tool 名常量唯一来源是 utils.tool_names；本文件不再保留字面量副本，避免漂移。
（与 handlers.* 不存在循环依赖，因为 utils.tool_names 是叶子模块）
"""

import re

from utils.tool_names import TOOL_DECISION_NONE, TOOL_TODO_DONE_CURRENT

_META_TODO_CLARIFY = re.compile(
    r"(skill|skills|SKILL|技能|路由|元问题|怎么判定|调用.*skill|待办.*skill|走.*skill)",
    re.I,
)
# 「做完」单独匹配会命中「还没做完」里的子串，误触 done_current；用负向后顾排除「没/还」紧邻的前缀。
_FAST_TODO_DONE = re.compile(
    r"(做完了|(?<![没还])做完|搞定了|搞定|完成了|好了|ok|OK|Ok|okk|OKK|行了|可以了|已完成)",
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
    """命中快通道时返回 decision，否则 None。

    当前策略：仅处理「有活跃待办队列 + 询问技能/路由元问题」。
    其它 todo/record/remind 意图统一交由小模型分类层 + unified 决策层。
    """

    has_active_queue = _has_active_todo_queue(user_id, todo_queues)
    if has_active_queue and _META_TODO_CLARIFY.search(text):
        return {
            "tool": TOOL_DECISION_NONE,
            "payload": {},
            "reply": "催办和动作判定由 OpenCode 工程里加载的待办 SKILL 管；我这边按当前内存队列推进。",
        }

    # 队列推进高频确认句走确定性快通道，避免每次都触发慢速 LLM 判定。
    if has_active_queue and _FAST_TODO_DONE.search(text or ""):
        return {"tool": TOOL_TODO_DONE_CURRENT, "payload": {}, "reply": ""}

    return None
