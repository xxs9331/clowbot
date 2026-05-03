"""section_reader 单测：覆盖 todo 分组、跨多分类 record、remind text/path、空节、### 不误退。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from utils.section_reader import (
    extract_section_text,
    format_todo_checkbox_lines,
    read_record_section,
    read_remind_section,
    read_todo_section,
)


def test_extract_section_text_h3_inside_does_not_break():
    md = textwrap.dedent(
        """\
        # 标题
        ## 📝 记录
        ### 🏋️ 身体
        - [x] 体重 68.5kg ✅08:00

        ### 📚 阅读
        - [x] 看了一小时书 ✅09:00

        ## ⏰ 提醒
        - [ ] 21:30：喝水
        """
    )
    section_with_h2 = extract_section_text(md, "📝", "记录", include_heading=True)
    assert section_with_h2.startswith("## 📝 记录")
    # H3 不应触发提前退出
    assert "### 🏋️ 身体" in section_with_h2
    assert "### 📚 阅读" in section_with_h2
    # 不应越过 ## ⏰ 提醒 这条 H2
    assert "## ⏰ 提醒" not in section_with_h2
    assert "21:30：喝水" not in section_with_h2


def test_extract_section_text_missing_returns_empty():
    assert extract_section_text("# only title\n", "📋", "待办") == ""
    assert extract_section_text("", "📋", "待办") == ""


def test_format_todo_checkbox_lines_five_then_rest():
    labels = [str(i) for i in range(1, 8)]
    lines = format_todo_checkbox_lines(labels, done=False, items_per_line=5)
    assert lines == [
        "- [ ] 1，2，3，4，5",
        "- [ ] 6，7",
    ]


def test_format_todo_checkbox_lines_done_with_timestamp():
    lines = format_todo_checkbox_lines(
        ["a", "b"],
        done=True,
        completed_timestamp="18:55",
    )
    assert lines == ["- [x] a，b ✅18:55"]


def test_format_todo_checkbox_lines_roundtrip_queue_flat():
    lines = format_todo_checkbox_lines(["关机", "上厕所", "拿雨伞", "搬水桶", "吃药"], done=False)
    md = "## 📋 待办\n" + "\n".join(lines) + "\n"
    flat = read_todo_section(md)["queue_flat"]
    assert flat == ["关机", "上厕所", "拿雨伞", "搬水桶", "吃药"]


def test_read_todo_section_groups_and_queue_flat():
    md = textwrap.dedent(
        """\
        ## 📋 待办

        - [ ] 早会
          - [ ] 看议程
          - [ ] 准备发言

        - [x] 跑步 ✅07:30

        - [ ] 写周报，整理图，发邮件
        """
    )
    out = read_todo_section(md)
    flat = out["queue_flat"]
    assert "看议程" in flat
    assert "准备发言" in flat
    assert "跑步" not in flat
    assert "写周报" in flat or "整理图" in flat


def test_read_record_section_multi_categories():
    md = textwrap.dedent(
        """\
        ## 📝 记录

        ### 🏋️ 身体
        - [x] 体重 68.5kg ✅08:00

        ### 🏃 运动
        - [x] 跑步 5km ✅07:30
        - [x] 拉伸 ✅07:55

        ### 📚 阅读
        - [x] 看了一小时书 ✅09:00

        ## ⏰ 提醒
        """
    )
    out = read_record_section(md)
    assert set(out["categories_order"]) >= {"身体", "运动", "阅读"}
    by = out["by_category"]
    assert any("跑步" in i["text"] for i in by["运动"])
    assert any(i["done"] for i in by["身体"])


def test_read_remind_section_text_input_matches_path_input(tmp_path: Path):
    md = textwrap.dedent(
        """\
        # 标题
        ## ⏰ 提醒
        - [ ] 09:00：开会
        - [x] 07:30：晨跑 ✅07:32
        ## 其它
        """
    )
    items_text = read_remind_section(md)["items"]
    log = tmp_path / "2026-01-01.md"
    log.write_text(md, encoding="utf-8")
    items_path = read_remind_section(log)["items"]
    items_path_str = read_remind_section(str(log))["items"]
    assert items_text == items_path == items_path_str
    assert {(i["time"], i["text"], i["done"]) for i in items_text} == {
        ("09:00", "开会", False),
        ("07:30", "晨跑", True),
    }


def test_read_remind_section_empty_when_section_missing():
    out = read_remind_section("# 没有提醒节\n")
    assert out["items"] == []
    assert out["raw"] == ""


@pytest.mark.parametrize("md", ["", "## 📋 待办\n"])
def test_read_todo_section_empty(md: str):
    out = read_todo_section(md)
    assert out["queue_flat"] == []
