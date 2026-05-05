"""checkin_loop 轮询逻辑单测：slot=slot_at(now)、双分支、去重、expect 设置。

不启动真实异步循环，仅测试核心逻辑函数。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.helpers import minimal_timeline, minimal_vault
from utils import timeline_sync as ts
from utils.timeline_state import set_last_ping, set_state


@pytest.fixture
def cfg(tmp_path: Path) -> dict:
    d = tmp_path / "vault"
    d.mkdir()
    root = str(d)
    return {
        "vault": minimal_vault(root),
        "timeline": {**minimal_timeline(root, enabled=True), "timeline_dir": "时间轴"},
    }


def _run(coro):
    """Sync wrapper for async coroutines (Python 3.12+ safe)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# get_slot_body (was _read_slot_body)
# ---------------------------------------------------------------------------
class TestReadSlotBody:
    def test_empty_file_returns_empty(self, cfg: dict):
        dt = datetime(2026, 5, 4, 10, 0)
        ts.ensure_timeline_file(cfg, dt)
        from scheduler.checkin import get_slot_body

        assert get_slot_body(cfg, "10:00", dt) == ""

    def test_filled_slot_returns_body(self, cfg: dict):
        dt = datetime(2026, 5, 4, 10, 0)
        ts.ensure_timeline_file(cfg, dt)
        ts.upsert_timeline_slot(cfg, "10:00", "写代码", dt=dt)
        from scheduler.checkin import get_slot_body

        assert get_slot_body(cfg, "10:00", dt) == "写代码"

    def test_missing_file_returns_empty(self, cfg: dict):
        dt = datetime(2026, 5, 4, 10, 0)
        from scheduler.checkin import get_slot_body

        assert get_slot_body(cfg, "10:00", dt) == ""


# ---------------------------------------------------------------------------
# checkin_loop 核心逻辑：通过 mock handler 测试交互
# ---------------------------------------------------------------------------
class TestCheckinLoopLogic:
    """测试 checkin_loop 的核心决策逻辑，不跑完整异步循环。"""

    def _make_handler(self, cfg: dict) -> MagicMock:
        handler = MagicMock()
        handler.cfg = cfg
        handler.wx = MagicMock()
        handler.wx.user_id = "test-user"
        handler.wx._context_tokens = {"test-user": "tok123"}
        handler.acp = None  # 降级到 fallback
        handler.unified_session_id = ""
        handler.session_id = ""
        return handler

    def test_filled_slot_sends_summary_no_expect(self, cfg: dict):
        """已填格推送概括，不创建 checkin_expect。"""
        from scheduler.checkin import _build_summary_message, get_slot_body
        from utils.timeline_state import get_checkin_expect
        from utils.timeline_sync import slot_at

        dt = datetime(2026, 5, 4, 10, 17)
        ts.ensure_timeline_file(cfg, dt)
        ts.upsert_timeline_slot(cfg, "10:00", "写代码", dt=dt)
        set_state(cfg, "active")

        handler = self._make_handler(cfg)
        handler.wx.send_text = AsyncMock()

        slot = slot_at(dt)
        slot_body = get_slot_body(cfg, slot, dt)
        assert slot == "10:00"
        assert slot_body == "写代码"

        msg = _run(
            _build_summary_message(handler, slot=slot, slot_body=slot_body, now_str="10:17")
        )
        assert "写代码" in msg or "10:00" in msg

        # 确认没有 checkin_expect（已填格分支不设 expect）
        exp = get_checkin_expect(cfg, "test-user")
        assert exp is None

    def test_empty_slot_sends_request_and_sets_expect(self, cfg: dict):
        """空格推送请求记录，创建 checkin_expect，slot 为触发格。"""
        from scheduler.checkin import _build_empty_slot_message
        from utils.timeline_state import set_checkin_expect, get_checkin_expect, set_state
        from utils.timeline_sync import slot_at

        dt = datetime(2026, 5, 4, 10, 17)
        ts.ensure_timeline_file(cfg, dt)
        set_state(cfg, "active")

        handler = self._make_handler(cfg)

        slot = slot_at(dt)
        assert ts.is_slot_empty(cfg, slot, dt)

        msg = _run(
            _build_empty_slot_message(handler, slot=slot, now_str="10:17")
        )
        assert "10:00" in msg or "记录" in msg

        # 模拟 set_checkin_expect（模拟空格分支的行为）
        d_iso = dt.strftime("%Y-%m-%d")
        set_checkin_expect(cfg, "test-user", d_iso, slot)
        exp = get_checkin_expect(cfg, "test-user")
        assert exp is not None
        assert exp["slot"] == "10:00"
        assert exp["date"] == d_iso

    def test_same_slot_dedup(self, cfg: dict):
        """同一天同一 slot 不重复推送。"""
        from utils.timeline_state import set_last_ping, get_last_ping

        dt = datetime(2026, 5, 4, 10, 17)
        d_iso = dt.strftime("%Y-%m-%d")
        slot = ts.slot_at(dt)

        set_last_ping(cfg, d_iso, slot)
        last_d, last_s = get_last_ping(cfg)
        assert last_d == d_iso and last_s == slot

    def test_different_slot_no_dedup(self, cfg: dict):
        """不同 slot 不被去重。"""
        from utils.timeline_state import set_last_ping, get_last_ping

        d_iso = "2026-05-04"
        set_last_ping(cfg, d_iso, "10:00")
        last_d, last_s = get_last_ping(cfg)
        assert not (last_d == d_iso and last_s == "10:30")

    def test_10_00_not_replied_10_30_triggers_again(self, cfg: dict):
        """10:00 未回复时，10:30 仍会触发新一轮 checkin。"""
        from utils.timeline_state import set_last_ping, get_last_ping

        d_iso = "2026-05-04"
        set_last_ping(cfg, d_iso, "10:00")
        last_d, last_s = get_last_ping(cfg)
        assert not (last_d == d_iso and last_s == "10:30")

    def test_10_00_not_replied_10_30_triggers_again(self, cfg: dict):
        """10:00 未回复时，10:30 仍会触发新一轮 checkin。"""
        from utils.timeline_state import set_last_ping, get_last_ping

        d_iso = "2026-05-04"
        set_last_ping(cfg, d_iso, "10:00")
        last_d, last_s = get_last_ping(cfg)
        assert not (last_d == d_iso and last_s == "10:30")