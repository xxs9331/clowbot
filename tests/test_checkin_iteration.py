"""checkin 单轮推送 _checkin_iteration 行为。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

import utils.timeline_state as timeline_state_mod
from scheduler.checkin import _checkin_iteration
from tests.helpers import minimal_timeline, minimal_vault
from utils import timeline_sync as ts
from utils.timeline_state import (
    get_checkin_expect,
    get_last_ping,
    set_checkin_expect,
    set_state,
)
from utils.timeline_sync import get_slot_body


class _Wx:
    def __init__(self) -> None:
        self.user_id = "wx-user-1"
        self._context_tokens: dict[str, str] = {"wx-user-1": "tok"}
        self.messages: list[tuple[str, str, str]] = []

    async def send_text(self, msg: str, user: str, token: str) -> None:
        self.messages.append((msg, user, token))


class _Handler:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.wx = _Wx()
        self.session_id = ""
        self.acp = None
        self.bg_events: list[dict] = []

    def add_background_event(self, **kwargs) -> str:
        self.bg_events.append(dict(kwargs))
        return "evt-test"


@pytest.fixture
def checkin_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(timeline_state_mod, "_BOT_ROOT", tmp_path)
    d = tmp_path / "v"
    d.mkdir()
    root = str(d)
    return {
        "vault": minimal_vault(root),
        "timeline": {
            **minimal_timeline(root, enabled=True),
            "checkin_enabled": True,
            "timeline_dir": "时间轴",
            "state_dir": "@bot",
        },
    }


def test_empty_slot_sets_expect_and_ping(checkin_cfg: dict) -> None:
    set_state(checkin_cfg, "active")
    h = _Handler(checkin_cfg)
    dt = datetime(2026, 5, 4, 10, 0, 0)

    async def run() -> None:
        await _checkin_iteration(h, now=dt)

    asyncio.run(run())

    exp = get_checkin_expect(checkin_cfg, "wx-user-1")
    assert exp is not None
    assert exp.get("slot") == "10:00"
    assert exp.get("date") == "2026-05-04"
    assert get_last_ping(checkin_cfg) == ("2026-05-04", "10:00")
    assert len(h.wx.messages) == 1


def test_filled_ping_clears_stale_checkin_expect(checkin_cfg: dict) -> None:
    """上一轮空格催填留下的 expect，在下一轮「已填格概括」后应清除，避免误写旧半格。"""
    set_state(checkin_cfg, "active")
    h = _Handler(checkin_cfg)
    dt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(checkin_cfg, dt)
    ts.upsert_timeline_slot(checkin_cfg, "10:00", "写代码", dt=dt)
    set_checkin_expect(checkin_cfg, "wx-user-1", "2026-05-04", "09:30")

    async def run() -> None:
        await _checkin_iteration(h, now=dt)

    asyncio.run(run())

    assert get_checkin_expect(checkin_cfg, "wx-user-1") is None
    assert len(h.wx.messages) == 1


def test_filled_slot_no_expect(checkin_cfg: dict) -> None:
    set_state(checkin_cfg, "active")
    h = _Handler(checkin_cfg)
    dt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(checkin_cfg, dt)
    ts.upsert_timeline_slot(checkin_cfg, "10:00", "写代码", dt=dt)
    assert get_slot_body(checkin_cfg, "10:00", dt) == "写代码"

    async def run() -> None:
        await _checkin_iteration(h, now=dt)

    asyncio.run(run())

    assert get_checkin_expect(checkin_cfg, "wx-user-1") is None
    assert get_last_ping(checkin_cfg) == ("2026-05-04", "10:00")
    assert len(h.wx.messages) == 1
    assert "追加" in h.wx.messages[0][0]


def test_same_slot_skipped_second_time(checkin_cfg: dict) -> None:
    set_state(checkin_cfg, "active")
    h = _Handler(checkin_cfg)
    dt = datetime(2026, 5, 4, 10, 0, 0)

    async def run() -> None:
        await _checkin_iteration(h, now=dt)
        assert len(h.wx.messages) == 1
        await _checkin_iteration(h, now=dt)

    asyncio.run(run())
    assert len(h.wx.messages) == 1


def test_next_half_hour_not_skipped(checkin_cfg: dict) -> None:
    set_state(checkin_cfg, "active")
    h = _Handler(checkin_cfg)
    ts.ensure_timeline_file(checkin_cfg, datetime(2026, 5, 4, 10, 0))

    async def run() -> None:
        await _checkin_iteration(h, now=datetime(2026, 5, 4, 10, 0, 0))
        assert len(h.wx.messages) == 1
        await _checkin_iteration(h, now=datetime(2026, 5, 4, 10, 30, 0))

    asyncio.run(run())
    assert len(h.wx.messages) == 2


def test_unified_empty_slot_emits_immediate_event(checkin_cfg: dict) -> None:
    set_state(checkin_cfg, "active")
    checkin_cfg["bot"] = {"unified_chat_mode": True}
    h = _Handler(checkin_cfg)
    dt = datetime(2026, 5, 4, 10, 0, 0)

    async def run() -> None:
        await _checkin_iteration(h, now=dt)

    asyncio.run(run())

    assert len(h.wx.messages) == 0
    assert len(h.bg_events) == 1
    ev = h.bg_events[0]
    assert ev.get("kind") == "checkin_slot_empty"
    assert ev.get("priority") == "immediate"
    exp = get_checkin_expect(checkin_cfg, "wx-user-1")
    assert exp is not None
    assert exp.get("slot") == "10:00"
