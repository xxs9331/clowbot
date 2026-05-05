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


def test_checkin_meta_bypass_keeps_expect_and_slot_empty(hook_cfg: dict) -> None:
    """元指令不写轴、不清 expect，下一条仍可正常回填。"""
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    h = _H(hook_cfg, acp=_ACP(out="不应调用"))
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "10:00")

    async def run() -> None:
        consumed = await h._maybe_consume_checkin_expect("查看时间轴。", "u1", "tok")
        assert consumed is False

    asyncio.run(run())

    assert get_slot_body(hook_cfg, "10:00", tdt) == ""
    assert get_checkin_expect(hook_cfg, "u1") is not None
    assert h.wx.messages == []

    h2 = _H(hook_cfg, acp=_ACP(out="写代码"))

    async def run2() -> None:
        await h2._maybe_consume_checkin_expect("在改 bot", "u1", "tok")

    asyncio.run(run2())
    assert get_slot_body(hook_cfg, "10:00", tdt) == "写代码"
    assert get_checkin_expect(hook_cfg, "u1") is None


def test_checkin_meta_bypass_custom_phrase_from_config(hook_cfg: dict) -> None:
    hook_cfg["timeline"]["checkin_meta_bypass_phrases"] = ["仅看轴"]
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 12, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    h = _H(hook_cfg)
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "12:00")

    async def run() -> None:
        assert await h._maybe_consume_checkin_expect("仅看轴", "u1", "tok") is False

    asyncio.run(run())
    assert get_slot_body(hook_cfg, "12:00", tdt) == ""
    assert get_checkin_expect(hook_cfg, "u1") is not None


def test_reply_goes_to_expect_slot_not_current_time(hook_cfg: dict) -> None:
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    h = _H(hook_cfg, acp=_ACP(out="整理论文"))
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "10:00")

    async def run() -> None:
        await h._maybe_consume_checkin_expect("整理论文", "u1", "tok")

    asyncio.run(run())

    assert get_slot_body(hook_cfg, "10:00", tdt) == "整理论文"
    assert h.wx.messages and "10:00" in h.wx.messages[0][0]
    assert "整理论文" in h.wx.messages[0][0]


def test_reply_blocked_when_slot_filled(hook_cfg: dict) -> None:
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    ts.upsert_timeline_slot(hook_cfg, "10:00", "身体·睡眠7h", dt=tdt)
    h = _H(hook_cfg)
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "10:00")

    async def run() -> None:
        consumed = await h._maybe_consume_checkin_expect("补一句状态", "u1", "tok")
        assert consumed is True

    asyncio.run(run())

    assert get_slot_body(hook_cfg, "10:00", tdt) == "身体·睡眠7h"
    assert h.wx.messages and "已有记录" in h.wx.messages[0][0]
    # 拒绝写入后应清除 expect，避免后续每条消息都被当 checkin 消费
    assert get_checkin_expect(hook_cfg, "u1") is None


def test_filled_slot_explicit_append(hook_cfg: dict) -> None:
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    ts.upsert_timeline_slot(hook_cfg, "10:00", "身体·睡眠7h", dt=tdt)
    h = _H(hook_cfg, acp=_ACP(out="补一句"))
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "10:00")

    async def run() -> None:
        await h._maybe_consume_checkin_expect("追加 补一句", "u1", "tok")

    asyncio.run(run())

    body = get_slot_body(hook_cfg, "10:00", tdt)
    assert "身体·睡眠7h" in body
    assert "补一句" in body


def test_filled_slot_legacy_append_phrase(hook_cfg: dict) -> None:
    """旧版「追加到时间轴 …」仍视为显式追加。"""
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 11, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    ts.upsert_timeline_slot(hook_cfg, "11:00", "番茄中", dt=tdt)
    h = _H(hook_cfg, acp=_ACP(out="续写"))
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "11:00")

    async def run() -> None:
        await h._maybe_consume_checkin_expect("追加到时间轴 续写", "u1", "tok")

    asyncio.run(run())

    body = get_slot_body(hook_cfg, "11:00", tdt)
    assert "番茄中" in body
    assert "续写" in body


def test_checkin_long_reply_compacted(hook_cfg: dict) -> None:
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    long_in = "冗长" * 50
    short_out = "压缩短句"
    h = _H(hook_cfg, acp=_ACP(out=short_out))
    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "10:00")

    async def run() -> None:
        await h._maybe_consume_checkin_expect(long_in, "u1", "tok")

    asyncio.run(run())

    assert get_slot_body(hook_cfg, "10:00", tdt) == short_out


def test_checkin_long_reply_fallback_truncation(hook_cfg: dict) -> None:
    set_state(hook_cfg, "active")
    tdt = datetime(2026, 5, 4, 10, 0, 0)
    ts.ensure_timeline_file(hook_cfg, tdt)
    hook_cfg["timeline"]["compact_max_chars"] = 12
    long_in = "abcdefghijklmnopqrstuvwxyz"
    h = _H(hook_cfg, acp=_ACP(raises=True))

    set_checkin_expect(hook_cfg, "u1", "2026-05-04", "10:00")

    async def run() -> None:
        await h._maybe_consume_checkin_expect(long_in, "u1", "tok")

    asyncio.run(run())

    slot = get_slot_body(hook_cfg, "10:00", tdt)
    assert len(slot) == 12
    assert slot == long_in[:12]
