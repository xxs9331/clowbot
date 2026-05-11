"""琐事池：每次 checkin 从 8 件事中随机抽一类注入话术；不保证当天每件都点到。

状态仅持久化到 chore_pool_state.json（路径规则与 timeline state_dir 一致）。
``reminders`` 仅作审计（slot → 当时抽中的琐事）；不跟踪用户是否完成。
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

# ── 8 件琐事（每次随机其一）────────────────────────────────────────────
CHORE_POOL: list[str] = [
    "吃药",
    "接水",
    "上厕所",
    "定期",
    "记账",
    "收拾垃圾",
    "收拾桌面",
    "上药",
]

# ── 状态文件路径 ──────────────────────────────────────────────────────
_STATE_FILE = "chore_pool_state.json"
_BOT_ROOT = Path(__file__).resolve().parent.parent  # .clawbot/
_CHORE_HINT_PROMPT = _BOT_ROOT / "prompts" / "chore_hint.md"

# 与 prompts/chore_hint.md 同步；文件缺失或 format 失败时回退
_DEFAULT_CHORE_HINT_TMPL = (
    "琐事提示（每次从 {pool_size} 件中随机其一）："
    "本次可在话术里自然带上「{pick}」。其余事项在后续 checkin 仍可能被抽到。"
)

_mode_lock: dict[str, Any] = {}


def _load_chore_hint_template() -> str:
    """每次调用重新读文件，与 checkin.md 等一致，便于热更新。"""
    try:
        if _CHORE_HINT_PROMPT.is_file():
            t = _CHORE_HINT_PROMPT.read_text(encoding="utf-8", errors="replace").strip()
            if t:
                return t
    except OSError:
        pass
    return _DEFAULT_CHORE_HINT_TMPL


def _render_chore_hint(pick: str, *, pool_size: int) -> str:
    tmpl = _load_chore_hint_template()
    try:
        return tmpl.format(pick=pick, pool_size=pool_size)
    except (KeyError, ValueError):
        return _DEFAULT_CHORE_HINT_TMPL.format(pick=pick, pool_size=pool_size)


def _state_path(cfg: dict) -> Path:
    """复用 timeline_state 的状态目录逻辑。
    - ``@bot`` / ``@package`` → .clawbot/ 根
    - 否则 → vault.root / state_dir /
    """
    tl = cfg.get("timeline") or {}
    rel = str(tl.get("state_dir") or "").strip()
    if rel.lower() in ("@bot", "@package"):
        return (_BOT_ROOT / _STATE_FILE).resolve()
    root = Path(str((cfg.get("vault") or {}).get("root") or "").strip()).resolve()
    rel_norm = rel.strip("/\\").replace("\\", "/")
    return (root / rel_norm / _STATE_FILE).resolve()


def _normalize_reminders(raw_rm: Any) -> list[dict]:
    reminders: list[dict] = []
    if not isinstance(raw_rm, list):
        return reminders
    for x in raw_rm:
        if isinstance(x, dict) and str(x.get("chore") or "").strip():
            reminders.append(
                {
                    "slot": str(x.get("slot") or ""),
                    "chore": str(x.get("chore") or "").strip(),
                }
            )
    return reminders


def _read_state(cfg: dict) -> dict:
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        date = str(raw.get("date") or "").strip()
        if not date:
            return {}
        reminders = _normalize_reminders(raw.get("reminders"))
        return {"date": date, "reminders": reminders}
    except Exception:
        return {}


def _write_state(cfg: dict, data: dict) -> None:
    import threading

    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    key = str(p)
    if key not in _mode_lock:
        _mode_lock[key] = threading.Lock()
    with _mode_lock[key]:
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _state_valid_for_today(st: dict, today: str) -> bool:
    return isinstance(st.get("date"), str) and st.get("date") == today


def _bootstrap_today(cfg: dict, today: str) -> dict:
    st = {"date": today, "reminders": []}
    _write_state(cfg, st)
    return st


def _ensure_today_state(cfg: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    st = _read_state(cfg)
    if not _state_valid_for_today(st, today):
        return _bootstrap_today(cfg, today)
    canonical = {"date": st["date"], "reminders": list(st.get("reminders") or [])}
    # 旧版含 order/cursor：落盘时去掉，避免误导
    p = _state_path(cfg)
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and ("order" in raw or "cursor" in raw):
                _write_state(cfg, canonical)
        except Exception:
            pass
    return canonical


# ── 公共 API ──────────────────────────────────────────────────────────


def get_chore_hint(cfg: dict, *, slot: str = "") -> str:
    """返回注入 checkin prompt 的琐事提示文本。

    每次调用从 ``CHORE_POOL`` 均匀随机抽一项；``slot`` 非空时向 ``reminders`` 追加一条（审计）。
    """
    st = _ensure_today_state(cfg)
    pick = random.choice(CHORE_POOL)
    reminders = list(st.get("reminders") or [])
    sl = str(slot or "").strip()
    if sl:
        reminders.append({"slot": sl, "chore": pick})

    st_out = {"date": st["date"], "reminders": reminders}
    _write_state(cfg, st_out)

    return _render_chore_hint(pick, pool_size=len(CHORE_POOL))
