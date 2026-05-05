"""timeline_state 单测（临时 vault）。"""

from __future__ import annotations

from pathlib import Path

import utils.timeline_state as timeline_state_mod
from tests.helpers import minimal_timeline, minimal_vault
from utils.timeline_state import get_state, mark_daily_opening_chat, set_state


def test_state_roundtrip(tmp_path: Path):
    root = str(tmp_path / "v")
    (tmp_path / "v").mkdir()
    cfg = {"vault": minimal_vault(root), "timeline": minimal_timeline(root)}
    assert get_state(cfg) == "sleep"
    set_state(cfg, "active")
    assert get_state(cfg) == "active"
    set_state(cfg, "sleep")
    assert get_state(cfg) == "sleep"


def test_state_dir_at_bot_writes_under_bot_root(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(timeline_state_mod, "_BOT_ROOT", tmp_path)
    cfg = {
        "vault": minimal_vault(str(tmp_path / "v")),
        "timeline": {**minimal_timeline(str(tmp_path / "v")), "state_dir": "@bot"},
    }
    (tmp_path / "v").mkdir()
    assert get_state(cfg) == "sleep"
    set_state(cfg, "active")
    fp = tmp_path / "timeline_checkin_state.json"
    assert fp.is_file()
    assert get_state(cfg) == "active"


def test_mark_daily_opening_chat_woke_once_per_day(tmp_path: Path):
    root = str(tmp_path / "v")
    (tmp_path / "v").mkdir()
    cfg = {"vault": minimal_vault(root), "timeline": minimal_timeline(root)}
    uid = "wx-user-1"
    assert get_state(cfg) == "sleep"
    assert mark_daily_opening_chat(cfg, uid) == "woke"
    assert get_state(cfg) == "active"
    assert mark_daily_opening_chat(cfg, uid) == "noop"
    assert mark_daily_opening_chat(cfg, uid) == "noop"
    assert mark_daily_opening_chat(cfg, "") == "no_user"
