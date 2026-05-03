"""三域统一确定性 section 解析器（read 改 Python 后由它代替 LLM）。

约定：
- 输入是当日日记 markdown 全文（str），不直接读盘，便于复用与单测。
- 不写盘、不抛业务异常；缺节/空节返回空结构。
- 解析行为以 vault 现有 markdown 习惯为准（见 todo-coach SKILL.md / 生活日志 SKILL.md / log_sync.py）。
"""

from __future__ import annotations

import re
from pathlib import Path

from utils.log_sync import parse_reminder_lines, parse_reminders_from_log


# ─── 通用工具 ───

_SECTION_HEAD_RE = re.compile(
    r"^\s*##\s*(?:\d+(?:\.\d+)?\s+)?({emoji})\s*({title})",
)


def _extract_section_lines(md_text: str, emoji: str, title: str) -> list[str]:
    """提取 `## {emoji} {title}` 章节内的行（不含标题行；遇下一个 H2 或文件尾停止；H3+ 不视为章节结束）。"""
    body, _ = _extract_section_with_heading(md_text, emoji, title)
    return body


def _extract_section_with_heading(
    md_text: str, emoji: str, title: str
) -> tuple[list[str], str]:
    """同 `_extract_section_lines`，但额外返回原始的 H2 标题行（找不到则空串）。"""
    if not md_text:
        return [], ""
    head_re = re.compile(
        rf"^\s*##\s*(?:\d+(?:\.\d+)?\s+)?{re.escape(emoji)}\s*{re.escape(title)}"
    )
    next_h2_re = re.compile(r"^##(?:\s|$)")
    out: list[str] = []
    heading = ""
    in_section = False
    for line in md_text.split("\n"):
        stripped = line.strip()
        if head_re.match(stripped):
            heading = line
            in_section = True
            continue
        if in_section and next_h2_re.match(stripped):
            break
        if in_section:
            out.append(line)
    return out, heading


def extract_section_text(
    md_text: str, emoji: str, title: str, *, include_heading: bool = False
) -> str:
    """便捷封装：返回章节文本（可选含 H2 标题），strip 末尾空白；找不到返回空串。"""
    body, heading = _extract_section_with_heading(md_text, emoji, title)
    if not body and not heading:
        return ""
    if include_heading and heading:
        return ("\n".join([heading, *body])).rstrip()
    return ("\n".join(body)).rstrip()


# ─── todo: ## 📋 待办 ───

# 生活日志 `## 📋 待办`：每行一条 `- [ ]`/`- [x]`，行内用逗号串联至多若干个子项（与 todo-coach 一致）
TODO_ITEMS_PER_LINE = 5

# 行内时间戳（整组完成后）：✅HH:MM 或 ✅ HH:MM
_TODO_TIMESTAMP_RE = re.compile(r"✅\s*(\d{1,2}:\d{2})")
# 中文/英文逗号、顿号皆可作为子项分隔
_TODO_ITEM_SPLIT_RE = re.compile(r"[，,、]")


def format_todo_checkbox_lines(
    labels: list[str],
    *,
    done: bool = False,
    items_per_line: int = TODO_ITEMS_PER_LINE,
    completed_timestamp: str | None = None,
) -> list[str]:
    """把扁平子项名格式化为待办节的 checklist 行（每行至多 `items_per_line` 项，中文逗号连接）。

    与 `2-Areas/习惯养成/生活日志` 中手写习惯及 todo-coach「每行最多五项」一致。
    """
    n = int(items_per_line) if int(items_per_line) > 0 else TODO_ITEMS_PER_LINE
    clean = [str(x).strip() for x in labels if str(x).strip()]
    if not clean:
        return []
    chunks: list[list[str]] = []
    cur: list[str] = []
    for x in clean:
        if len(cur) >= n:
            chunks.append(cur)
            cur = []
        cur.append(x)
    if cur:
        chunks.append(cur)
    mark = "x" if done else " "
    lines: list[str] = []
    for chunk in chunks:
        body = "，".join(chunk)
        line = f"- [{mark}] {body}"
        if done and completed_timestamp:
            line += f" ✅{completed_timestamp}"
        lines.append(line)
    return lines


def _split_todo_subitems(payload: str) -> list[str]:
    """把单行待办的 payload 按中文逗号等切成子项，去掉空项与时间戳残留。"""
    payload = _TODO_TIMESTAMP_RE.sub("", payload).strip()
    items: list[str] = []
    for raw in _TODO_ITEM_SPLIT_RE.split(payload):
        item = raw.strip().strip("。.;；")
        if item:
            items.append(item)
    return items


def read_todo_section(md_text: str) -> dict:
    """解析 `## 📋 待办` 节为结构化数据。

    返回：
        {
            "groups": [
                {"items": ["换鞋","裤子",...], "done": True,  "timestamp": "16:22"},
                {"items": ["鞋架","视频",...], "done": False, "timestamp": None},
            ],
            "queue_flat": ["鞋架","视频",...],   # 仅未完成子项扁平
            "raw": "<整段原文>",
        }
    """
    lines = _extract_section_lines(md_text, "📋", "待办")
    raw = "\n".join(lines).strip()

    groups: list[dict] = []
    for line in lines:
        s = line.strip()
        if not s.startswith("- ["):
            continue
        # 状态
        if s.startswith("- [x]") or s.startswith("- [X]"):
            done = True
        elif s.startswith("- [ ]"):
            done = False
        else:
            continue
        body = s[5:].strip()  # 去掉 "- [ ]" / "- [x]"
        ts_match = _TODO_TIMESTAMP_RE.search(body)
        timestamp = ts_match.group(1) if ts_match else None
        items = _split_todo_subitems(body)
        if not items:
            continue
        groups.append({"items": items, "done": done, "timestamp": timestamp})

    queue_flat: list[str] = []
    for g in groups:
        if g["done"]:
            continue
        queue_flat.extend(g["items"])

    return {"groups": groups, "queue_flat": queue_flat, "raw": raw}


# ─── record: ## 📝 记录 ───

# 记录行：- [ ] xxx ✅HH:MM   /   - [x] xxx ✅HH:MM
_RECORD_LINE_RE = re.compile(
    r"^\s*-\s*\[(?P<state>[ xX])\]\s*(?P<text>.+?)(?:\s*✅\s*(?P<ts>\d{1,2}:\d{2}))?\s*$"
)
# 类目行编号前缀：1. / 1.1 / 1.2.3
_CAT_NUM_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*\s+")
# 类目行 emoji/符号前缀（直到出现首个 \w 或文字）
_CAT_SYM_PREFIX_RE = re.compile(r"^[^\w\u4e00-\u9fff]+\s*")


def _parse_record_cat_name(stripped: str) -> str | None:
    """解析 `### [可选编号] [可选emoji] 名称` 中的「名称」。"""
    if not stripped.startswith("### "):
        return None
    rest = stripped[4:].strip()
    rest = _CAT_NUM_PREFIX_RE.sub("", rest)
    rest = _CAT_SYM_PREFIX_RE.sub("", rest)
    rest = rest.strip()
    return rest or None


def read_record_section(md_text: str) -> dict:
    """解析 `## 📝 记录` 节。

    返回：
        {
            "categories_order": ["身体", "阅读", ...],
            "by_category": {
                "身体": [{"text": "...", "done": True, "timestamp": "16:01"}, ...],
                ...
            },
            "raw": "<整段原文>",
        }
    """
    lines = _extract_section_lines(md_text, "📝", "记录")
    raw = "\n".join(lines).strip()

    categories_order: list[str] = []
    by_category: dict[str, list[dict]] = {}
    current_cat: str | None = None

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("### "):
            cat = _parse_record_cat_name(s)
            if cat:
                if cat not in by_category:
                    categories_order.append(cat)
                    by_category[cat] = []
                current_cat = cat
            continue
        if not s.startswith("- ["):
            continue
        m = _RECORD_LINE_RE.match(s)
        if not m or current_cat is None:
            continue
        by_category[current_cat].append(
            {
                "text": m.group("text").strip(),
                "done": m.group("state").lower() == "x",
                "timestamp": m.group("ts"),
            }
        )

    return {
        "categories_order": categories_order,
        "by_category": by_category,
        "raw": raw,
    }


# ─── remind: ## ⏰ 提醒 ───
# 解析与 utils.log_sync.parse_reminder_lines 共用同一份正则常量与解析函数，
# 不再在本文件复制；read_remind_section 只决定输入是文本还是路径，并补上 raw。


def read_remind_section(md_text_or_path) -> dict:
    """解析 `## ⏰ 提醒` 节。

    支持两种输入：
      - md_text (str)：直接给 markdown 全文（用于无文件场景与测试）
      - log_path (Path/str)：直接给路径

    返回：
        {
            "items": [{"time": "07:30", "text": "上班", "done": False, "line": 5}, ...],
            "raw": "<整段原文 或 空>",
        }
    """
    if isinstance(md_text_or_path, Path) or (
        isinstance(md_text_or_path, str)
        and ("\n" not in md_text_or_path)
        and md_text_or_path.endswith(".md")
    ):
        path = Path(md_text_or_path)
        items = parse_reminders_from_log(path)
        try:
            md = path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception:
            md = ""
        raw = "\n".join(_extract_section_lines(md, "⏰", "提醒")).strip()
        return {"items": items, "raw": raw}

    md_text = md_text_or_path or ""
    items = parse_reminder_lines(md_text.split("\n"))
    raw = "\n".join(_extract_section_lines(md_text, "⏰", "提醒")).strip()
    return {"items": items, "raw": raw}
