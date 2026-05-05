"""Checkin 活跃/休眠状态与轮询去重，持久化在 vault 子目录或 @bot 本地。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

_STATE_FILE = "timeline_checkin_state.json"
# .clawbot/ 根（本文件在 utils/ 下）
_BOT_ROOT = Path(__file__).resolve().parent.parent

_mode_lock: dict[str, Any] = {}  # path -> threading.Lock，延迟导入 threading 避免循环


def _state_path(cfg: dict) -> Path:
    """状态文件路径。

    - ``timeline.state_dir`` 为 ``@bot`` / ``@package``：``<.clawbot>/timeline_checkin_state.json``（与 vault 分离，便于本机调试）。
    - 否则：``vault.root`` / ``state_dir`` / ``timeline_checkin_state.json``。
    """
    tl = cfg.get("timeline") or {}
    rel = str(tl.get("state_dir") or "").strip()
    if rel.lower() in ("@bot", "@package"):
        return (_BOT_ROOT / _STATE_FILE).resolve()
    root = Path(str((cfg.get("vault") or {}).get("root") or "").strip()).resolve()
    rel_norm = rel.strip("/\\").replace("\\", "/")
    return (root / rel_norm / _STATE_FILE).resolve()


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _read_state(cfg: dict) -> dict:
    p = _state_path(cfg)
    if not p.exists():
        return {
            "mode": "sleep",
            "last_ping_date": "",
            "last_ping_slot": "",
            "checkin_expect": {},
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {
            "mode": "sleep",
            "last_ping_date": "",
            "last_ping_slot": "",
            "checkin_expect": {},
        }


def _write_state(cfg: dict, data: dict) -> None:
    import threading

    p = _state_path(cfg)
    _ensure_dir(p)
    key = str(p)
    if key not in _mode_lock:
        _mode_lock[key] = threading.Lock()
    with _mode_lock[key]:
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def get_state(cfg: dict) -> str:
    """返回 ``active`` 或 ``sleep``。"""
    return str(_read_state(cfg).get("mode") or "sleep").strip().lower() or "sleep"


def set_state(cfg: dict, mode: str) -> None:
    m = (mode or "").strip().lower()
    if m not in ("active", "sleep"):
        m = "sleep"
    st = _read_state(cfg)
    st["mode"] = m
    st["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_state(cfg, st)


def get_last_ping(cfg: dict) -> tuple[str, str]:
    st = _read_state(cfg)
    return str(st.get("last_ping_date") or ""), str(st.get("last_ping_slot") or "")


def set_last_ping(cfg: dict, date_iso: str, slot_hhmm: str) -> None:
    st = _read_state(cfg)
    st["last_ping_date"] = date_iso
    st["last_ping_slot"] = slot_hhmm
    _write_state(cfg, st)


def set_checkin_expect(cfg: dict, user_id: str, date_iso: str, slot_hhmm: str) -> None:
    st = _read_state(cfg)
    exp = st.get("checkin_expect")
    if not isinstance(exp, dict):
        exp = {}
    exp[user_id] = {
        "date": date_iso,
        "slot": slot_hhmm,
        "ts": datetime.now().timestamp(),
    }
    st["checkin_expect"] = exp
    _write_state(cfg, st)


def pop_checkin_expect(cfg: dict, user_id: str) -> dict | None:
    st = _read_state(cfg)
    exp = st.get("checkin_expect")
    if not isinstance(exp, dict):
        return None
    v = exp.pop(user_id, None)
    st["checkin_expect"] = exp
    _write_state(cfg, st)
    return v if isinstance(v, dict) else None


def get_checkin_expect(cfg: dict, user_id: str) -> dict | None:
    st = _read_state(cfg)
    exp = st.get("checkin_expect")
    if not isinstance(exp, dict):
        return None
    v = exp.get(user_id)
    return v if isinstance(v, dict) else None


def mark_daily_opening_chat(cfg: dict, user_id: str) -> str:
    """同一用户当天首条消息：若仍为 ``sleep`` 则切 ``active``（视为起床）。

    返回 ``woke`` | ``noop`` | ``no_user``。不拦截消息，由主路由继续处理。
    """
    uid = (user_id or "").strip()
    if not uid:
        return "no_user"
    today = datetime.now().strftime("%Y-%m-%d")
    st = _read_state(cfg)
    by_user = st.get("first_chat_date_by_user")
    if not isinstance(by_user, dict):
        by_user = {}
    if by_user.get(uid) == today:
        return "noop"
    mode = str(st.get("mode") or "sleep").strip().lower() or "sleep"
    woke = mode == "sleep"
    if woke:
        st["mode"] = "active"
    by_user[uid] = today
    st["first_chat_date_by_user"] = by_user
    st["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_state(cfg, st)
    return "woke" if woke else "noop"
