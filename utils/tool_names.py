"""三域决策 tool 名 / 域名 / 旧名兼容映射 — 唯一来源。

所有 handlers / utils 都从这里 import 常量；不要再在其它模块定义同名字面量，
否则会再次引发字符串漂移（曾经因为 handlers.todo 与 utils.route_fast 各定一份
而踩过循环导入）。

文件本身不依赖 handlers / acp / wechat，可被任何模块安全 import。
"""

from __future__ import annotations

# ─── 决策层 tool 名（命名空间）───
TOOL_DECISION_NONE = "none"

TOOL_TODO_MERGE_NEW_ITEMS = "todo.merge_new_items"
TOOL_TODO_DONE_CURRENT = "todo.done_current"
TOOL_TODO_NOT_DONE = "todo.not_done"
TOOL_TODO_NEXT = "todo.next"
TOOL_TODO_REORDER = "todo.reorder"
TOOL_TODO_REORDER_CONFIRM = "todo.reorder_confirm"
TOOL_TODO_SKIP_CURRENT = "todo.skip_current"
TOOL_TODO_ABANDON_CURRENT = "todo.abandon_current"

TOOL_RECORD_ADD = "record.add"
TOOL_REMIND_ADD = "remind.add"

TODO_COACH_TOOLS = frozenset(
    {
        TOOL_TODO_MERGE_NEW_ITEMS,
        TOOL_TODO_DONE_CURRENT,
        TOOL_TODO_NOT_DONE,
        TOOL_TODO_NEXT,
        TOOL_TODO_REORDER,
        TOOL_TODO_REORDER_CONFIRM,
        TOOL_TODO_SKIP_CURRENT,
        TOOL_TODO_ABANDON_CURRENT,
    }
)

# ─── Vault 写入工具名（用于 OpenCode 工具调用 envelope）───
TOOL_WRITE_TODO_SECTION = "write_todo_section"
TOOL_WRITE_RECORD_SECTION = "write_record_section"
TOOL_WRITE_REMIND_SECTION = "write_remind_section"

# ─── 域 ───
DOMAIN_TODO = "todo"
DOMAIN_RECORD = "record"
DOMAIN_REMIND = "remind"

ALL_DOMAINS = (DOMAIN_TODO, DOMAIN_RECORD, DOMAIN_REMIND)

# ─── 旧名兼容（LLM 输出旧名时由 dispatcher.normalize 自动映射到新名）───
# 不要给"看起来不像是旧名"的字符串加映射；必须确实是上一代代码 / SKILL 用过的。
LEGACY_TOOL_ALIASES: dict[str, str] = {
    "life_log": TOOL_RECORD_ADD,
    "merge_new_items": TOOL_TODO_MERGE_NEW_ITEMS,
    "done_current": TOOL_TODO_DONE_CURRENT,
    "not_done": TOOL_TODO_NOT_DONE,
    "next": TOOL_TODO_NEXT,
    "reorder": TOOL_TODO_REORDER,
    "reorder_confirm": TOOL_TODO_REORDER_CONFIRM,
    "skip_current": TOOL_TODO_SKIP_CURRENT,
    "abandon_current": TOOL_TODO_ABANDON_CURRENT,
}


def normalize_tool_name(raw: str) -> str:
    """大小写不敏感，但保留点号；旧名映射到新名；空值视为 none。"""
    name = (raw or "").strip()
    if not name:
        return TOOL_DECISION_NONE
    lower = name.lower()
    return LEGACY_TOOL_ALIASES.get(lower, lower)
