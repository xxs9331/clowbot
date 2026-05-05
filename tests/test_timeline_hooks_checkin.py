"""时间轴 checkin 回复写回期望格。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

import utils.timeline_state as timeline_state_mod
from handlers.timeline_hooks import TimelineHooksMixin
from tests.helpers import minimal_timeline, minimal_vault
from utils import timeline_sync as ts
from utils.timeline_state import set_checkin_expect, set_state
from utils.timeline_sync import get_slot_body


class _Wx:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_text(self, msg: str, user: str, token: str) -> None:
        self.messages.append((msg, user, token))


class _H(TimelineHooksMixin):
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.wx = _Wx()


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


def test_reply_goes_to_expect_slot_not_current_time(hook_cfg: dict) -> None:
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    h = _H(hook_cfg)
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "10:00")

    async def run() -> None:
        await h._maybe_consume_checkin_expect("整理论文", "u1", "tok")

    asyncio.run(run())

    assert get_slot_body(hook_cfg, "10:00", tdt) == "整理论文"
    assert h.wx.messages and "10:00" in h.wx.messages[0][0]
