"""待办 Vault 工具 — 给 OpenCode 的结构化单次调用，细则只在 todo-coach SKILL 里。

ClawBot 不在此复现 SKILL 流程；只提供路径、payload 与输出约定，模型先读 SKILL 再执行 fs。
"""

from __future__ import annotations

import json
from typing import Any

from acp.opencode_client import todo_coach_skill_path
from utils.log_sync import get_log_path

# 与 todo-coach SKILL 配合的固定工具名（仅保留读/写两类）
TOOL_READ_TODO_SECTION = "read_todo_section"
TOOL_WRITE_TODO_SECTION = "write_todo_section"

# 输出约定（短句，避免在 Python 里展开 SKILL 细节）
OUTPUT_SYNC_QUEUE_JSON = (
    "只读解析：不要改文件。只输出一行 JSON，格式："
    '`{"todo_section":"...", "queue_flat":["子项1",...]}`；无待办时 queue_flat 为空数组。'
)
OUTPUT_WRITE_CONFIRM = "按 SKILL 完成写盘后：只回复一句给用户的中文微信确认，不要分析过程。"


def build_todo_vault_tool_prompt(
    system_prefix: str,
    vault_root: str,
    daily_log_dir: str,
    tool_id: str,
    payload: dict[str, Any],
    output_contract: str,
) -> str:
    """拼一条 session/prompt：模型应 read SKILL → 再按 tool/payload 操作 today_log。"""
    skill_path = todo_coach_skill_path(vault_root)
    today_log = str(get_log_path(vault_root, daily_log_dir))
    envelope = {
        "tool": tool_id,
        "skill_path": skill_path,
        "today_log": today_log,
        "payload": payload,
        "output_contract": output_contract,
    }
    return (
        f"{system_prefix}\n\n"
        "## ClawBot · 待办 Vault 工具调用\n"
        "步骤：1) 用 fs/read_text_file 读取 `skill_path`（todo-coach 全文）。\n"
        "2) 再按该 SKILL 操作 `today_log` 的 `## 📋 待办`（除非工具另有说明）。\n"
        "3) 本消息中的 JSON 仅提供参数与输出格式，业务规则以 SKILL 为准。\n\n"
        f"```json\n{json.dumps(envelope, ensure_ascii=False, indent=2)}\n```\n"
    )
