"""timeline.append 显式追加与时间轴压缩。"""

from __future__ import annotations

import asyncio
from datetime import datetime

import handlers.coaches  # noqa: F401 — 触发 register_tool_handler
import pytest

from handlers.coaches import timeline as timeline_coach_mod
from handlers.dispatcher import get_registered_tools
from handlers.coaches.timeline import TimelineAppendMixin
from tests.helpers import minimal_timeline, minimal_vault
from utils.timeline_sync import get_slot_body
from utils.tool_names import TOOL_TIMELINE_APPEND


class _Wx:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_text(self, msg: str, user: str, token: str) -> None:
        self.messages.append((msg, user, token))

    async def set_typing(self, *, to_user: str, status: int, context_token: str) -> None:
        pass


class _ACP:
    async def prompt(self, *_a, **_kw):
        return "一行摘要", ""


class _H(TimelineAppendMixin):
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.wx = _Wx()
        self.acp = _ACP()
        self.unified_session_id = "sess"


@pytest.fixture
def append_cfg(tmp_path) -> dict:
    v = tmp_path / "vault"
    v.mkdir()
    root = str(v)
    return {
        "vault": minimal_vault(root),
        "timeline": {
            **minimal_timeline(root, enabled=True),
            "timeline_dir": "时间轴",
            "state_dir": "bot_state",
        },
    }


def test_tool_registered() -> None:
    assert TOOL_TIMELINE_APPEND in get_registered_tools()


def test_append_default_slot(append_cfg: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 5, 4, 14, 7, 0)

    class _Dt:
        @staticmethod
        def now():
            return fixed

    monkeypatch.setattr(timeline_coach_mod, "datetime", _Dt)

    h = _H(append_cfg)

    async def run() -> None:
        await h._coach_timeline_append(
            {"text": "写了一段代码"},
            "",
            "u1",
            "tok",
            user_text="",
        )

    asyncio.run(run())

    assert get_slot_body(append_cfg, "14:00", fixed) == "一行摘要"
    assert h.wx.messages and "14:00" in h.wx.messages[-1][0]


def test_append_explicit_slot(append_cfg: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    dt = datetime(2026, 5, 4, 9, 0, 0)

    class _Dt:
        @staticmethod
        def now():
            return dt

    monkeypatch.setattr(timeline_coach_mod, "datetime", _Dt)

    h = _H(append_cfg)

    async def run() -> None:
        await h._coach_timeline_append(
            {"text": "晨跑", "slot": "08:30"},
            "",
            "u1",
            "tok",
        )

    asyncio.run(run())

    assert get_slot_body(append_cfg, "08:30", dt) == "一行摘要"
