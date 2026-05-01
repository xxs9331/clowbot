"""确定性快通道：仅保留元问题澄清，业务意图交给小模型分类层。

tool 名常量唯一来源是 utils.tool_names；本文件不再保留字面量副本，避免漂移。
（与 handlers.* 不存在循环依赖，因为 utils.tool_names 是叶子模块）
"""

import re

from utils.tool_names import TOOL_DECISION_NONE

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
    """命中快通道时返回 decision，否则 None。

    当前策略：仅处理「有活跃待办队列 + 询问技能/路由元问题」。
    其它 todo/record/remind 意图统一交由小模型分类层 + unified 决策层。
    """

    if _has_active_todo_queue(user_id, todo_queues) and _META_TODO_CLARIFY.search(text):
        return {
            "tool": TOOL_DECISION_NONE,
            "payload": {},
            "reply": "催办和动作判定由 OpenCode 工程里加载的待办 SKILL 管；我这边按当前内存队列推进。",
        }

    return None
