"""日志同步 — 后台扫描提醒节，时间到了就推送

只做一件事：每分钟读今天的日志文件，扫描 ## ⏰ 提醒 节，
把时间已到且未完成的提醒推送给用户，并标记为已完成。

日志格式：
  ## ⏰ 提醒
  - [ ] 07:30：上班
  - [x] 07:30：上班 ✅07:30   ← 已触发

数据全在日志文件里，不依赖 JSON。
"""

import re
from datetime import datetime
from pathlib import Path

# ─── reminder 行解析正则（模块级常量；section_reader 与 log_sync 共用，避免漂移）───

# 提醒节 H2 标题：## ⏰ 提醒（可带编号前缀 1.1）
REMIND_SECTION_HEAD_RE = re.compile(r"^##\s*(?:\d+(?:\.\d+)?\s+)?⏰\s*提醒")
# 已触发：- [x] HH:MM：内容 ✅HH:MM
REMIND_DONE_LINE_RE = re.compile(
    r"^- \[x\]\s*(?:.*?)(\d{1,2}:\d{2})[：:]\s*(.+?)\s*✅"
)
# 未触发：- [ ] HH:MM：内容
REMIND_PENDING_LINE_RE = re.compile(
    r"^- \[ \]\s*(?:.*?)(\d{1,2}:\d{2})[：:]\s*(.+)"
)


def get_log_path(vault_root: str, daily_log_dir: str, dt: datetime = None) -> Path:
    """返回日志文件路径：vault_root/daily_log_dir/YYYY/MM/YYYY-MM-DD.md"""
    dt = dt or datetime.now()
    return Path(vault_root) / daily_log_dir / str(dt.year) / f"{dt.month:02d}" / dt.strftime("%Y-%m-%d.md")


def append_to_markdown_section(filepath: Path, heading: str, line: str) -> None:
    """在 Markdown 文件的指定标题节下追加一行。标题不存在则创建。"""
    if not filepath.exists():
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(f"{heading}\n{line}\n", encoding="utf-8")
        return

    content = filepath.read_text(encoding="utf-8")
    lines_list = content.splitlines()
    heading_index = None
    for i, item in enumerate(lines_list):
        if item.strip() == heading:
            heading_index = i
            break

    if heading_index is None:
        content = content.rstrip("\n") + f"\n\n{heading}\n{line}\n"
    else:
        insert_at = heading_index + 1
        while insert_at < len(lines_list) and not lines_list[insert_at].startswith("## "):
            insert_at += 1
        lines_list.insert(insert_at, line)
        content = "\n".join(lines_list) + "\n"

    filepath.write_text(content, encoding="utf-8")


def parse_reminder_lines(text_lines: list[str]) -> list[dict]:
    """对一组 markdown 行（已 split 过的）按提醒节解析。

    遇到第一行匹配 REMIND_SECTION_HEAD_RE 视为进入提醒节；
    再遇到任何 `##` 起始视为退出。返回：
        [{"time": "07:30", "text": "上班", "done": False, "line": 5}, ...]
    """
    reminders: list[dict] = []
    in_section = False
    for i, line in enumerate(text_lines):
        stripped = line.strip()
        if REMIND_SECTION_HEAD_RE.match(stripped):
            in_section = True
            continue
        if in_section and stripped.startswith("##"):
            break
        if not (in_section and stripped.startswith("- ")):
            continue
        m_done = REMIND_DONE_LINE_RE.match(stripped)
        if m_done:
            reminders.append(
                {
                    "time": m_done.group(1),
                    "text": m_done.group(2).strip(),
                    "done": True,
                    "line": i,
                }
            )
            continue
        m_pending = REMIND_PENDING_LINE_RE.match(stripped)
        if m_pending:
            reminders.append(
                {
                    "time": m_pending.group(1),
                    "text": m_pending.group(2).strip(),
                    "done": False,
                    "line": i,
                }
            )
    return reminders


def parse_reminders_from_log(log_path: Path) -> list[dict]:
    """从日志文件解析 ⏰ 提醒 节（薄包装：读盘后委托给 parse_reminder_lines）。"""
    if not log_path.exists():
        return []
    return parse_reminder_lines(log_path.read_text(encoding="utf-8").split("\n"))


def mark_reminder_done(log_path: Path, line_num: int, now_str: str = "") -> bool:
    """将日志中指定行的 - [ ] 标记为 - [x] ... ✅HH:MM"""
    if not log_path.exists():
        return False
    now_str = now_str or datetime.now().strftime("%H:%M")
    lines = log_path.read_text(encoding="utf-8").split("\n")
    if line_num >= len(lines):
        return False
    line = lines[line_num]
    if "- [ ]" not in line:
        return False
    # - [ ] 07:30：上班 → - [x] 07:30：上班 ✅07:30
    lines[line_num] = line.replace("- [ ]", f"- [x]", 1) + f" ✅{now_str}"
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def mark_reminder_done_by_time_text(
    log_path: Path,
    remind_time: str,
    remind_text: str,
    now_str: str = "",
) -> bool:
    """按「时间+文本」标记提醒完成，避免行号在并发写入后失效。"""
    if not log_path.exists():
        return False
    now_str = now_str or datetime.now().strftime("%H:%M")
    lines = log_path.read_text(encoding="utf-8").split("\n")
    escaped_text = re.escape((remind_text or "").strip())
    target_re = re.compile(
        rf"^\s*-\s*\[\s\]\s*(?:.*?){re.escape(remind_time)}[：:]\s*{escaped_text}\s*$"
    )
    for i, line in enumerate(lines):
        if target_re.match(line.strip()):
            lines[i] = line.replace("- [ ]", "- [x]", 1) + f" ✅{now_str}"
            log_path.write_text("\n".join(lines), encoding="utf-8")
            return True
    return False


def mark_todo_group_done(filepath: Path, group_anchor_text: str, now_hm: str) -> bool:
    """在 ## 📋 待办 节内，将包含 anchor 的 - [ ] 行改为 - [x] ... ✅HH:MM。"""
    if not filepath.exists():
        return False
    content = filepath.read_text(encoding="utf-8")
    # 延迟导入避免与 section_reader 的模块级互导循环。
    from utils.section_reader import extract_section_text

    todo_body = extract_section_text(content, "📋", "待办", include_heading=False)
    if not todo_body:
        return False
    for line in todo_body.splitlines():
        s = line.strip()
        if s.startswith("- [ ]") and group_anchor_text in s:
            body = s[5:].strip()
            new_line = f"- [x] {body} ✅{now_hm}"
            filepath.write_text(content.replace(line, new_line, 1), encoding="utf-8")
            return True
    return False


def remove_todo_item_from_group(filepath: Path, item_text: str) -> bool:
    """从待办行中精确移除子项；空组则删行。"""
    if not filepath.exists():
        return False
    content = filepath.read_text(encoding="utf-8")
    # 延迟导入避免与 section_reader 的模块级互导循环。
    from utils.section_reader import _TODO_TIMESTAMP_RE, _split_todo_subitems, extract_section_text

    todo_body = extract_section_text(content, "📋", "待办", include_heading=False)
    if not todo_body:
        return False
    for line in todo_body.splitlines():
        s = line.strip()
        if not (s.startswith("- [ ]") or s.startswith("- [x]")):
            continue
        body = s[5:].strip()
        items = _split_todo_subitems(body)
        if item_text not in items:
            continue
        items = [x for x in items if x != item_text]
        ts_match = _TODO_TIMESTAMP_RE.search(body)
        ts_suffix = f" ✅{ts_match.group(1)}" if ts_match else ""
        if not items:
            content = content.replace(line + "\n", "", 1)
            if line in content:
                content = content.replace(line, "", 1)
        else:
            prefix = "- [x]" if s.startswith("- [x]") else "- [ ]"
            new_line = f"{prefix} {'，'.join(items)}{ts_suffix}"
            content = content.replace(line, new_line, 1)
        filepath.write_text(content, encoding="utf-8")
        return True
    return False


def get_due_reminders(log_path: Path, now: datetime = None) -> list[dict]:
    """返回已到期的未完成提醒"""
    now = now or datetime.now()
    current_time = now.strftime("%H:%M")
    reminders = parse_reminders_from_log(log_path)
    due = []
    for r in reminders:
        if not r["done"] and r["time"] <= current_time:
            due.append(r)
    return due