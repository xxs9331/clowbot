"""琐事池：每日将 8 件事随机洗牌成固定顺序，checkin 按序各提醒一次；用完后注入小说/游戏类休闲提示。

状态仅持久化到 chore_pool_state.json（路径规则与 timeline state_dir 一致）。
不跟踪用户是否「完成」某事，亦无 post_write 自动划掉。
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

# ── 8 件琐事（顺序仅用于校验；每日 order 为洗牌结果）──────────────────────
CHORE_POOL: list[str] = [
    "吃药",
    "接水",
    "上厕所",
    "远眺",
    "记账",
    "收拾垃圾",
    "收拾桌面",
    "上药",
]

_LEISURE_HINTS: tuple[str, ...] = (
    "今日 8 件琐事已在各半格 checkin 里各点名过一次。话术里可穿插一句：放松一下看会儿小说（轻松、无压力即可）。",
    "今日琐事顺序已轮完一轮。话术里可轻提一句：玩会儿游戏换换脑也可以～",
)

# ── 状态文件路径 ──────────────────────────────────────────────────────
_STATE_FILE = "chore_pool_state.json"
_BOT_ROOT = Path(__file__).resolve().parent.parent  # .clawbot/

_mode_lock: dict[str, Any] = {}


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


def _read_state(cfg: dict) -> dict:
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        o = raw.get("order")
        if (
            isinstance(o, list)
            and len(o) == len(CHORE_POOL)
            and sorted(str(x) for x in o) == sorted(CHORE_POOL)
        ):
            return raw
        # 旧版仅有 completed/last_suggested，或 order 损坏：视为无状态，由 _ensure_today_state 洗牌重建
        return {}
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
    if st.get("date") != today:
        return False
    o = st.get("order")
    if not isinstance(o, list) or len(o) != len(CHORE_POOL):
        return False
    try:
        return sorted(str(x) for x in o) == sorted(CHORE_POOL)
    except TypeError:
        return False


def _bootstrap_today(cfg: dict, today: str) -> dict:
    order = list(CHORE_POOL)
    random.shuffle(order)
    st = {"date": today, "order": order, "cursor": 0, "reminders": []}
    _write_state(cfg, st)
    return st


def _ensure_today_state(cfg: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    st = _read_state(cfg)
    if not _state_valid_for_today(st, today):
        return _bootstrap_today(cfg, today)
    cursor = max(0, min(int(st.get("cursor") or 0), len(CHORE_POOL)))
    raw_rm = st.get("reminders") or []
    reminders: list[dict] = []
    if isinstance(raw_rm, list):
        for x in raw_rm:
            if isinstance(x, dict) and str(x.get("chore") or "").strip():
                reminders.append(
                    {
                        "slot": str(x.get("slot") or ""),
                        "chore": str(x.get("chore") or "").strip(),
                    }
                )
    canonical = {
        "date": st["date"],
        "order": list(st["order"]),
        "cursor": cursor,
        "reminders": reminders,
    }
    if "completed" in st or "last_suggested" in st:
        _write_state(cfg, canonical)
    return canonical


# ── 公共 API ──────────────────────────────────────────────────────────


def get_chore_hint(cfg: dict, *, slot: str = "") -> str:
    """返回注入 checkin prompt 的琐事/休闲提示文本。

    - 当日首次调用会洗牌并写入 ``order``；之后每次消费下一件，直至 8 件用完。
    - ``slot`` 非空时向 ``reminders`` 追加一条记录（仅审计）。
    """
    st = _ensure_today_state(cfg)
    cursor = int(st.get("cursor") or 0)
    order: list[str] = list(st["order"])

    if cursor >= len(CHORE_POOL):
        return random.choice(_LEISURE_HINTS)

    pick = order[cursor]
    reminders = list(st.get("reminders") or [])
    sl = str(slot or "").strip()
    if sl:
        reminders.append({"slot": sl, "chore": pick})

    next_cursor = cursor + 1
    st_out = {
        "date": st["date"],
        "order": order,
        "cursor": next_cursor,
        "reminders": reminders,
    }
    _write_state(cfg, st_out)

    rest = order[next_cursor:] if next_cursor < len(CHORE_POOL) else []
    rest_s = "、".join(rest) if rest else "无"
    return (
        f"琐事提示（按日随机顺序，每件当天只提醒一次）：当前第 {cursor + 1}/{len(CHORE_POOL)} 件，"
        f"可在话术里自然带上「{pick}」。尚未点到的还有：{rest_s}。"
    )
