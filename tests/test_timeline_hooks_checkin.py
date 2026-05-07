"""时间轴 hooks 前置行为。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

import utils.timeline_state as timeline_state_mod
from handlers.timeline_hooks import TimelineHooksMixin
from tests.helpers import minimal_timeline, minimal_vault
from utils import timeline_sync as ts
from utils.timeline_state import get_checkin_expect, set_checkin_expect, set_state
from utils.timeline_sync import get_slot_body


class _Wx:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_text(self, msg: str, user: str, token: str) -> None:
        self.messages.append((msg, user, token))


class _ACP:
    def __init__(self, out: str | None = None, *, raises: bool = False) -> None:
        self.out = out
        self.raises = raises

    async def prompt(self, *_a, **_kw):
        if self.raises:
            raise RuntimeError("acp down")
        return (self.out if self.out is not None else ""), ""


class _H(TimelineHooksMixin):
    def __init__(self, cfg: dict, acp: _ACP | None = None) -> None:
        self.cfg = cfg
        self.wx = _Wx()
        self.acp = acp or _ACP()
        self.unified_session_id = "sess"


@pytest.fixture
def hook_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
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


def test_timeline_preprocess_no_sleep_text_not_consumed(hook_cfg: dict) -> None:
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    h = _H(hook_cfg, acp=_ACP(out="不应调用"))
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "10:00")

    async def run() -> None:
        consumed = await h._timeline_preprocess("在改 bot", "u1", "tok")
        assert consumed is False

    asyncio.run(run())

    assert get_slot_body(hook_cfg, "10:00", tdt) == ""
    assert get_checkin_expect(hook_cfg, "u1") is not None
    assert h.wx.messages == []

def test_timeline_preprocess_sleep_text_consumed(hook_cfg: dict) -> None:
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    h = _H(hook_cfg, acp=_ACP(out="🌙 睡觉"))

    async def run() -> None:
        consumed = await h._timeline_preprocess("准备睡了", "u1", "tok")
        assert consumed is True

    asyncio.run(run())

    assert h.wx.messages
    assert "已休眠 checkin" in h.wx.messages[0][0]


def test_sleep_fallback_uses_default_line_when_compact_empty(hook_cfg: dict) -> None:
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    h = _H(hook_cfg, acp=_ACP(out=""))

    async def run() -> None:
        consumed = await h._timeline_preprocess("晚安", "u1", "tok")
        assert consumed is True

    asyncio.run(run())

    today = datetime.now()
    slot = ts.slot_at(today)
    body = get_slot_body(hook_cfg, slot, today)
    assert body == "🌙 睡觉"


def test_timeline_disabled_skip_preprocess(hook_cfg: dict) -> None:
    hook_cfg["timeline"]["enabled"] = False
    set_state(hook_cfg, "active")
    h = _H(hook_cfg, acp=_ACP(out="🌙 睡觉"))
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "10:00")

    async def run() -> None:
        consumed = await h._timeline_preprocess("晚安", "u1", "tok")
        assert consumed is False

    asyncio.run(run())
    assert get_checkin_expect(hook_cfg, "u1") is not None
