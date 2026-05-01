"""build_coach_write_prompt 单测：三域 envelope 字段 + section 标题 + skill 路径正确。"""

from __future__ import annotations

import json
import re

import pytest

from utils.coach_tools import (
    DOMAIN_RECORD,
    DOMAIN_REMIND,
    DOMAIN_TODO,
    OUTPUT_WRITE_CONFIRM,
    TOOL_WRITE_RECORD_SECTION,
    TOOL_WRITE_REMIND_SECTION,
    TOOL_WRITE_TODO_SECTION,
    build_coach_write_prompt,
    get_domain_tool,
)


_FAKE_VAULT = "D:/fake/vault"
_FAKE_LOG_DIR = "2-Areas/习惯养成/生活日志"


def _extract_envelope(prompt: str) -> dict:
    m = re.search(r"```json\n(.*?)\n```", prompt, re.DOTALL)
    assert m, "envelope JSON not found in prompt"
    return json.loads(m.group(1))


@pytest.mark.parametrize(
    "domain, expect_tool, expect_section, expect_skill_substr",
    [
        (DOMAIN_TODO, TOOL_WRITE_TODO_SECTION, "## 📋 待办", "todo-coach"),
        (DOMAIN_RECORD, TOOL_WRITE_RECORD_SECTION, "## 📝 记录", "record-coach"),
        (DOMAIN_REMIND, TOOL_WRITE_REMIND_SECTION, "## ⏰ 提醒", "remind-coach"),
    ],
)
def test_build_coach_write_prompt_three_domains(
    domain, expect_tool, expect_section, expect_skill_substr
):
    payload = {"op": "demo", "value": 42}
    prompt = build_coach_write_prompt(
        domain,
        system_prefix="<SYSTEM_PREFIX>",
        vault_root=_FAKE_VAULT,
        daily_log_dir=_FAKE_LOG_DIR,
        payload=payload,
    )
    assert "<SYSTEM_PREFIX>" in prompt
    assert expect_section in prompt
    env = _extract_envelope(prompt)
    assert env["tool"] == expect_tool
    assert env["payload"] == payload
    assert env["output_contract"] == OUTPUT_WRITE_CONFIRM
    assert expect_skill_substr in env["skill_path"].replace("\\", "/")
    assert _FAKE_LOG_DIR.replace("/", "\\") in env["today_log"] or _FAKE_LOG_DIR in env[
        "today_log"
    ].replace("\\", "/")


def test_get_domain_tool_round_trip():
    assert get_domain_tool(DOMAIN_TODO) == TOOL_WRITE_TODO_SECTION
    assert get_domain_tool(DOMAIN_RECORD) == TOOL_WRITE_RECORD_SECTION
    assert get_domain_tool(DOMAIN_REMIND) == TOOL_WRITE_REMIND_SECTION


def test_unknown_domain_raises():
    with pytest.raises(ValueError):
        build_coach_write_prompt(
            "unknown",
            system_prefix="x",
            vault_root=_FAKE_VAULT,
            daily_log_dir=_FAKE_LOG_DIR,
            payload={},
        )


def test_custom_tool_id_overrides_default():
    prompt = build_coach_write_prompt(
        DOMAIN_TODO,
        system_prefix="x",
        vault_root=_FAKE_VAULT,
        daily_log_dir=_FAKE_LOG_DIR,
        payload={},
        tool_id="todo.custom_op",
    )
    env = _extract_envelope(prompt)
    assert env["tool"] == "todo.custom_op"
