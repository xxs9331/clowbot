"""评测 Case 数据结构与加载器。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EvalCase:
    """单个评测用例。"""

    id: str
    type: str  # simple / medium / long
    description: str
    input: str  # 用户消息文本
    expected_tool: str  # 期望工具名
    expected_payload_keys: list[str] = field(default_factory=list)
    expected_category: str = ""  # record.add 的 category
    max_steps: int = 1
    expected_failure_modes: list[str] = field(default_factory=list)
    context_before: list[dict[str, Any]] = field(default_factory=list)
    # —— 以下为 runner 写入 ——
    actual_tool: str = ""
    actual_payload: dict[str, Any] = field(default_factory=dict)
    actual_reply: str = ""
    passed: bool | None = None
    failure_mode_hit: str = ""
    notes: str = ""

    def to_report_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "description": self.description,
            "input": self.input[:80],
            "expected_tool": self.expected_tool,
            "actual_tool": self.actual_tool,
            "passed": self.passed,
            "failure_mode": self.failure_mode_hit,
            "notes": self.notes,
        }


def load_cases(file_path: str | Path) -> list[EvalCase]:
    """从 YAML 文件加载评测用例列表。"""
    with open(file_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cases = []
    for item in raw.get("cases", []):
        case = EvalCase(
            id=item["id"],
            type=item.get("type", "simple"),
            description=item.get("description", ""),
            input=item["input"],
            expected_tool=item.get("expected", {}).get("tool", "none"),
            expected_payload_keys=item.get("expected", {}).get("payload_keys", []),
            expected_category=item.get("expected", {}).get("category", ""),
            max_steps=item.get("max_steps", 1),
            expected_failure_modes=item.get("expected_failure_modes", []),
            context_before=item.get("context_before", []),
        )
        cases.append(case)
    return cases


def load_all_cases(cases_dir: str | Path) -> list[EvalCase]:
    """加载 cases/ 目录下所有 YAML。"""
    all_cases: list[EvalCase] = []
    for fp in sorted(Path(cases_dir).glob("*.yaml")):
        all_cases.extend(load_cases(fp))
    return all_cases
