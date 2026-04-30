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


def get_log_path(vault_root: str, daily_log_dir: str, dt: datetime = None) -> Path:
    """返回日志文件路径：vault_root/daily_log_dir/YYYY/MM/YYYY-MM-DD.md"""
    dt = dt or datetime.now()
    return Path(vault_root) / daily_log_dir / str(dt.year) / f"{dt.month:02d}" / dt.strftime("%Y-%m-%d.md")


def parse_reminders_from_log(log_path: Path) -> list[dict]:
    """从日志文件解析 ⏰ 提醒 节

    返回: [{"time": "07:30", "text": "上班", "done": False, "line": 5}, ...]
    """
    if not log_path.exists():
        return []

    lines = log_path.read_text(encoding="utf-8").split("\n")
    reminders = []
    in_remind_section = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        # 进入提醒节
        if re.match(r"^##\s*⏰\s*提醒", stripped):
            in_remind_section = True
            continue
        # 离开提醒节（遇到下一个 ##）
        if in_remind_section and stripped.startswith("##"):
            break
        # 解析提醒行
        if in_remind_section and stripped.startswith("- "):
            # 已触发的: - [x] 07:30：上班 ✅07:30
            done_match = re.match(r"^- \[x\]\s*(\d{1,2}:\d{2})[：:]\s*(.+?)\s*✅", stripped)
            if done_match:
                reminders.append({
                    "time": done_match.group(1),
                    "text": done_match.group(2).strip(),
                    "done": True, "line": i,
                })
                continue
            # 未触发的: - [ ] 07:30：上班
            pending_match = re.match(r"^- \[ \]\s*(\d{1,2}:\d{2})[：:]\s*(.+)", stripped)
            if pending_match:
                reminders.append({
                    "time": pending_match.group(1),
                    "text": pending_match.group(2).strip(),
                    "done": False, "line": i,
                })

    return reminders


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