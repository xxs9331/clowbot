"""琐事池：每次 checkin 从 8 件中随机抽一类。"""

from __future__ import annotations

from datetime import datetime

import pytest

from utils import chore_pool as cp


def _cfg(tmp_path):
    return {
        "vault": {"root": str(tmp_path)},
        "timeline": {"state_dir": "state"},
    }


def test_chore_hint_random_choice(monkeypatch, tmp_path):
    class _FixedNow:
        @staticmethod
        def now():
            return datetime(2030, 6, 15, 12, 0, 0)

    monkeypatch.setattr(cp, "datetime", _FixedNow)
    monkeypatch.setattr(cp.random, "choice", lambda seq: "记账")

    cfg = _cfg(tmp_path)
    hint = cp.get_chore_hint(cfg, slot="03:30")
    assert "记账" in hint
    assert "随机" in hint
    st = cp._read_state(cfg)
    assert st["date"] == "2030-06-15"
    assert st["reminders"] == [{"slot": "03:30", "chore": "记账"}]


def test_chore_hint_no_slot_skips_reminder_row(monkeypatch, tmp_path):
    class _FixedNow:
        @staticmethod
        def now():
            return datetime(2030, 6, 15, 12, 0, 0)

    monkeypatch.setattr(cp, "datetime", _FixedNow)
    monkeypatch.setattr(cp.random, "choice", lambda seq: "接水")

    cfg = _cfg(tmp_path)
    cp.get_chore_hint(cfg, slot="")
    st = cp._read_state(cfg)
    assert st["reminders"] == []


def test_render_chore_hint_bad_template_uses_default(monkeypatch):
    monkeypatch.setattr(cp, "_load_chore_hint_template", lambda: "{not_a_placeholder}")
    s = cp._render_chore_hint("吃药", pool_size=8)
    assert "吃药" in s
    assert "8" in s


def test_new_day_clears_reminders(monkeypatch, tmp_path):
    class _FixedNow2030_07_01:
        @staticmethod
        def now():
            return datetime(2030, 7, 1, 0, 0, 0)

    monkeypatch.setattr(cp, "datetime", _FixedNow2030_07_01)

    cfg = _cfg(tmp_path)
    cp._write_state(
        cfg,
        {
            "date": "2030-07-01",
            "reminders": [{"slot": "00:00", "chore": "吃药"}],
        },
    )

    class _FixedNow2030_07_02:
        @staticmethod
        def now():
            return datetime(2030, 7, 2, 0, 0, 0)

    monkeypatch.setattr(cp, "datetime", _FixedNow2030_07_02)
    st = cp._ensure_today_state(cfg)
    assert st["date"] == "2030-07-02"
    assert st["reminders"] == []
