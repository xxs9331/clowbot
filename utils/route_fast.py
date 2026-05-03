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
# 另：整句仅为 ok/okay/okk（可带句号、空格）走单独规则，避免英文词里子串 ok（如 took）误触。
_FAST_TODO_DONE = re.compile(
    r"(做完了|(?<![没还])做完|搞定了|搞定|完成了|好了|行了|可以了|已完成)",
    re.I,
)
# 整句只有 ok / okay /okk（可带收尾标点）
_STANDALONE_OK_ACK = re.compile(
    r"^[\s\u3000]*(?:okk|okay|ok)(?:[\s\u3000]*[.!！?？。…~～])*[\s\u3000]*$",
    re.I,
)
# 行首 ok 族，后接空白/标点/CJK（避免 took、book 等子串误触）
_LEADING_OK_ACK = re.compile(
    r"^[\s\u3000]*(?:okk|okay|ok)(?:$|[\s,，.。!！?？…~～、；;:(（)）【】《》]|[\u4e00-\u9fff])",
    re.I,
)

# 全角拉丁字母 O/K → 半角，便于「ｏｋ」「ＯＫ」与 ok 同权
_FW_OK_TRANSLATE = str.maketrans(
    {
        "\uff2f": "O",  # FULLWIDTH LATIN CAPITAL LETTER O
        "\uff4f": "o",
        "\uff2b": "K",  # FULLWIDTH LATIN CAPITAL LETTER K
        "\uff4b": "k",
    }
)


def _normalize_ack_spellings(text: str) -> str:
    return (text or "").translate(_FW_OK_TRANSLATE)


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

    做完确认除中文句式外，支持 ok / okay / okk（含全角 ｏｋ、ＯＫ），整句或行首后接中文/标点。
    """

    has_active_queue = _has_active_todo_queue(user_id, todo_queues)
    if has_active_queue and _META_TODO_CLARIFY.search(text):
        return {
            "tool": TOOL_DECISION_NONE,
            "payload": {},
            "reply": "催办和动作判定由 OpenCode 工程里加载的待办 SKILL 管；我这边按当前内存队列推进。",
        }

    # 队列推进高频确认句走确定性快通道，避免每次都触发慢速 LLM 判定。
    canon = _normalize_ack_spellings(text or "")
    st = canon.strip()
    if has_active_queue and (
        _STANDALONE_OK_ACK.match(st)
        or _LEADING_OK_ACK.match(st)
        or _FAST_TODO_DONE.search(canon)
    ):
        return {"tool": TOOL_TODO_DONE_CURRENT, "payload": {}, "reply": ""}

    return None
