"""时间轴 48 格（00:00–23:30）读写与 upsert。

一行一格：`HH:MM  内容`；同格多事件用配置的分隔符（默认 `|`）追加。
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from utils.flow_log import log_flow_event
from utils.time_utils import date_str, month_dir, year_dir

# 每文件一把锁，避免并发写坏行
_file_locks: dict[str, threading.Lock] = {}
_meta_lock = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _meta_lock:
        if key not in _file_locks:
            _file_locks[key] = threading.Lock()
        return _file_locks[key]


def timeline_vault_root(cfg: dict) -> Path:
    """时间轴文件根：仅使用 ``timeline.root_dir``（由 ``validate_config`` 保证非空）。"""
    tl = cfg.get("timeline") or {}
    return Path(str(tl.get("root_dir") or "").strip()).resolve()


def timeline_dir_relative(cfg: dict) -> str:
    tl = cfg.get("timeline") or {}
    return str(tl.get("timeline_dir") or "").strip().strip("/\\").replace("\\", "/")


def timeline_enabled(cfg: dict) -> bool:
    tl = cfg.get("timeline") or {}
    return bool(tl.get("enabled", False))


def timeline_path(cfg: dict, dt: datetime | None = None) -> Path:
    dt = dt or datetime.now()
    root = timeline_vault_root(cfg)
    rel = timeline_dir_relative(cfg)
    return root / rel / year_dir(dt) / month_dir(dt) / f"{date_str(dt)}.md"


def slot_at(dt: datetime) -> str:
    """当前时刻所在半格起点 HH:MM。"""
    m = (dt.minute // 30) * 30
    return f"{dt.hour:02d}:{m:02d}"


def last_completed_slot_at_boundary(now: datetime | None = None) -> str:
    """在整点或半点触发时，返回「刚结束」的半格起点 HH:MM。

    例：10:30 触发 → 刚结束的是 10:00–10:30 → 返回 ``10:00``。
    """
    now = now or datetime.now()
    ns = now.replace(second=0, microsecond=0)
    prev = ns - timedelta(minutes=30)
    return slot_at(prev)


SLOT_LINE_RE = re.compile(r"^(\d{2}:\d{2})\s+(.*)\s*$")


def _parse_slot_lines(text: str) -> dict[str, str]:
    """返回 slot -> 该行除去时间前缀后的正文（strip 后）。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = SLOT_LINE_RE.match(line.rstrip("\n"))
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def build_blank_timeline_body(day: date | None = None) -> str:
    day = day or datetime.now().date()
    ds = day.strftime("%Y-%m-%d")
    lines = [f"# 🕐 {ds}", ""]
    for h in range(24):
        for minute in (0, 30):
            lines.append(f"{h:02d}:{minute:02d}  ")
    return "\n".join(lines) + "\n"


def ensure_timeline_file(cfg: dict, dt: datetime | None = None) -> Path:
    """若当日时间轴不存在则创建 48 格空壳。"""
    dt = dt or datetime.now()
    path = timeline_path(cfg, dt)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    body = build_blank_timeline_body(dt.date())
    path.write_text(body, encoding="utf-8")
    return path


def is_slot_empty(cfg: dict, slot_hhmm: str, dt: datetime | None = None) -> bool:
    path = timeline_path(cfg, dt)
    if not path.exists():
        return True
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return True
    slots = _parse_slot_lines(text)
    return not (slots.get(slot_hhmm, "").strip())


def get_last_non_empty_slot(cfg: dict, dt: datetime | None = None) -> str:
    """从文件底部向上找最近一格非空内容，返回 ``HH:MM 正文`` 或 ``无``。"""
    path = timeline_path(cfg, dt)
    if not path.exists():
        return "无"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "无"
    for line in reversed(lines):
        m = SLOT_LINE_RE.match(line.rstrip("\n"))
        if not m:
            continue
        body = m.group(2).strip()
        if body:
            return f"{m.group(1)} {body}"
    return "无"


def upsert_timeline_slot(
    cfg: dict,
    slot_hhmm: str,
    content: str,
    *,
    dt: datetime | None = None,
    mode: Literal["fill_or_append", "replace"] = "fill_or_append",
) -> bool:
    """写入指定半格：空则填；非空则 `` | `` 追加（可配置）。replace 整格替换。"""
    if not timeline_enabled(cfg):
        return False
    c = (content or "").strip()
    if not c:
        return False
    if not re.match(r"^\d{2}:\d{2}$", slot_hhmm):
        log_flow_event(
            stage="timeline",
            route="write_fail",
            user_text=c,
            extra={"reason": "bad_slot", "slot": slot_hhmm},
        )
        return False

    tl = cfg.get("timeline") or {}
    sep = str(tl.get("append_separator") or "|").strip() or "|"

    path = ensure_timeline_file(cfg, dt)
    lock = _lock_for(path)
    with lock:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            log_flow_event(
                stage="timeline",
                route="write_fail",
                user_text=c,
                extra={"error": str(e)[:200], "path": str(path)},
            )
            return False

        lines = raw.splitlines(keepends=False)
        out_lines: list[str] = []
        found = False
        changed = False
        for line in lines:
            m = SLOT_LINE_RE.match(line.rstrip("\n"))
            if not m or m.group(1) != slot_hhmm:
                out_lines.append(line)
                continue
            found = True
            prefix = f"{slot_hhmm}  "
            old_body = m.group(2).strip()
            if mode == "replace":
                new_body = c
            elif not old_body:
                new_body = c
            else:
                if c in old_body or any(
                    part.strip() == c for part in old_body.split(sep)
                ):
                    new_body = old_body
                    log_flow_event(
                        stage="timeline",
                        route="append_conflict",
                        user_text=c,
                        extra={"slot": slot_hhmm, "note": "duplicate_skip"},
                    )
                else:
                    new_body = f"{old_body} {sep} {c}"
            new_line = prefix + new_body
            if new_line != line.rstrip("\n"):
                changed = True
            out_lines.append(new_line)

        if not found:
            log_flow_event(
                stage="timeline",
                route="write_fail",
                user_text=c,
                extra={"reason": "slot_line_missing", "slot": slot_hhmm},
            )
            return False

        if not changed:
            return True

        new_text = "\n".join(out_lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as e:
            log_flow_event(
                stage="timeline",
                route="write_fail",
                user_text=c,
                extra={"error": str(e)[:200], "path": str(path)},
            )
            return False

    log_flow_event(
        stage="timeline",
        route="write_ok",
        user_text=c,
        extra={"slot": slot_hhmm, "path": str(path)},
    )
    return True


def slot_for_hhmm(hhmm: str, dt: datetime | None = None) -> str:
    """将 ``HH:MM`` 规范到半格起点（向下取整到 0/30）。"""
    dt = dt or datetime.now()
    parts = (hhmm or "").strip().split(":")
    if len(parts) != 2:
        return slot_at(dt)
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        return slot_at(dt)
    m = (m // 30) * 30
    if h < 0 or h > 23:
        return slot_at(dt)
    return f"{h:02d}:{m:02d}"
