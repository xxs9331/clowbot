"""三域统一的 Vault 写入工具 prompt 构造。

只构造发给 OpenCode 的 prompt：让模型先 `fs/read_text_file` 读 SKILL，再按 payload.op
操作 today_log 对应章节并写回。所有业务规则都在 SKILL.md 里，不在 Python 复现。

读（read）已迁到 utils/section_reader.py（Python 端确定性解析），不在本文件。
"""

from __future__ import annotations

import json
from typing import Any

from acp.opencode_client import (
    record_coach_skill_path,
    remind_coach_skill_path,
    todo_coach_skill_path,
)
from utils.log_sync import get_log_path
from utils.tool_names import (
    DOMAIN_RECORD,
    DOMAIN_REMIND,
    DOMAIN_TODO,
    TOOL_WRITE_RECORD_SECTION,
    TOOL_WRITE_REMIND_SECTION,
    TOOL_WRITE_TODO_SECTION,
)

# 兼容：保留模块级别名供老代码 from utils.coach_tools import DOMAIN_TODO 等
__all__ = [
    "DOMAIN_TODO",
    "DOMAIN_RECORD",
    "DOMAIN_REMIND",
    "TOOL_WRITE_TODO_SECTION",
    "TOOL_WRITE_RECORD_SECTION",
    "TOOL_WRITE_REMIND_SECTION",
    "OUTPUT_WRITE_CONFIRM",
    "build_coach_write_prompt",
    "get_domain_tool",
]

# ─── 通用输出约定 ───
OUTPUT_WRITE_CONFIRM = "按 SKILL 完成写盘后：只回复一句给用户的中文微信确认，不要分析过程。"

_DOMAIN_META: dict[str, dict[str, Any]] = {
    DOMAIN_TODO: {
        "label": "待办",
        "section": "## 📋 待办",
        "tool": TOOL_WRITE_TODO_SECTION,
        "skill_path_fn": todo_coach_skill_path,
    },
    DOMAIN_RECORD: {
        "label": "记录",
        "section": "## 📝 记录",
        "tool": TOOL_WRITE_RECORD_SECTION,
        "skill_path_fn": record_coach_skill_path,
    },
    DOMAIN_REMIND: {
        "label": "提醒",
        "section": "## ⏰ 提醒",
        "tool": TOOL_WRITE_REMIND_SECTION,
        "skill_path_fn": remind_coach_skill_path,
    },
}


def get_domain_tool(domain: str) -> str:
    return _DOMAIN_META[domain]["tool"]


def build_coach_write_prompt(
    domain: str,
    *,
    system_prefix: str,
    vault_root: str,
    daily_log_dir: str,
    payload: dict[str, Any],
    output_contract: str = OUTPUT_WRITE_CONFIRM,
    tool_id: str | None = None,
) -> str:
    """三域共用的写入 prompt。

    参数：
        domain: "todo" | "record" | "remind"
        system_prefix: build_system_prompt(...) 的输出
        vault_root / daily_log_dir: 用于解析 today_log 与 SKILL 路径
        payload: 含 op + 业务字段（规则见对应 SKILL.md）
        output_contract: 给模型的回复格式短句
        tool_id: 可选覆盖默认工具名（默认按 domain 取 write_*_section）
    """
    if domain not in _DOMAIN_META:
        raise ValueError(f"unknown coach domain: {domain}")
    meta = _DOMAIN_META[domain]
    skill_path = meta["skill_path_fn"](vault_root)
    today_log = str(get_log_path(vault_root, daily_log_dir))
    envelope = {
        "tool": tool_id or meta["tool"],
        "skill_path": skill_path,
        "today_log": today_log,
        "payload": payload,
        "output_contract": output_contract,
    }
    return (
        f"{system_prefix}\n\n"
        f"## ClawBot · {meta['label']} Vault 工具调用\n"
        "步骤：1) 用 fs/read_text_file 读取 `skill_path`（对应 SKILL 全文）。\n"
        f"2) 再按该 SKILL 操作 `today_log` 内的 `{meta['section']}` 章节。\n"
        "3) 本消息中的 JSON 仅提供参数与输出格式，业务规则以 SKILL 为准。\n\n"
        f"```json\n{json.dumps(envelope, ensure_ascii=False, indent=2)}\n```\n"
    )
