"""琐事池：日序洗牌、每件一次、8 次后休闲提示。"""

from __future__ import annotations

from datetime import datetime

import pytest

from utils import chore_pool as cp


def _cfg(tmp_path):
    return {
        "vault": {"root": str(tmp_path)},
        "timeline": {"state_dir": "state"},
    }


def test_chore_hint_sequential_matches_order(monkeypatch, tmp_path):
    class _FixedNow:
        @staticmethod
        def now():
            return datetime(2030, 6, 15, 12, 0, 0)

    monkeypatch.setattr(cp, "datetime", _FixedNow)

    fixed_order = [
        "吃药",
        "接水",
        "上厕所",
        "远眺",
        "记账",
        "收拾垃圾",
        "收拾桌面",
        "上药",
    ]
    cfg = _cfg(tmp_path)
    cp._write_state(
        cfg,
        {
            "date": "2030-06-15",
            "order": list(fixed_order),
            "cursor": 0,
            "reminders": [],
        },
    )

    for i, name in enumerate(fixed_order):
        hint = cp.get_chore_hint(cfg, slot=f"{i:02d}:00")
        assert name in hint
        st = cp._read_state(cfg)
        assert st["cursor"] == i + 1
        assert len(st["reminders"]) == i + 1
        assert st["reminders"][-1] == {"slot": f"{i:02d}:00", "chore": name}


def test_chore_hint_leisure_after_eight(monkeypatch, tmp_path):
    class _FixedNow:
        @staticmethod
        def now():
            return datetime(2030, 6, 16, 8, 0, 0)

    monkeypatch.setattr(cp, "datetime", _FixedNow)

    cfg = _cfg(tmp_path)
    order = list(cp.CHORE_POOL)
    cp._write_state(
        cfg,
        {
            "date": "2030-06-16",
            "order": order,
            "cursor": 8,
            "reminders": [{"slot": "00:00", "chore": order[0]}],
        },
    )

    hint = cp.get_chore_hint(cfg, slot="23:30")
    assert "小说" in hint or "游戏" in hint
    st = cp._read_state(cfg)
    assert st["cursor"] == 8
    assert len(st["reminders"]) == 1


def test_new_day_regenerates_order(monkeypatch, tmp_path):
    class _FixedNow:
        @staticmethod
        def now():
            return datetime(2030, 7, 2, 0, 0, 0)

    monkeypatch.setattr(cp, "datetime", _FixedNow)

    cfg = _cfg(tmp_path)
    cp._write_state(
        cfg,
        {
            "date": "2030-07-01",
            "order": list(reversed(cp.CHORE_POOL)),
            "cursor": 8,
            "reminders": [],
        },
    )
    st = cp._ensure_today_state(cfg)
    assert st["date"] == "2030-07-02"
    assert st["cursor"] == 0
    assert sorted(st["order"]) == sorted(cp.CHORE_POOL)
