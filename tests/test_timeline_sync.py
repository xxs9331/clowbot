"""timeline_sync 单测（临时目录，不碰真实 vault）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.helpers import minimal_timeline, minimal_vault
from utils import timeline_sync as ts


@pytest.fixture
def cfg(tmp_path: Path) -> dict:
    d = tmp_path / "vault"
    d.mkdir()
    root = str(d)
    return {
        "vault": minimal_vault(root),
        "timeline": {**minimal_timeline(root, enabled=True), "timeline_dir": "时间轴"},
    }


def test_timeline_path_and_ensure(cfg):
    dt = datetime(2026, 5, 4, 14, 17)
    p = ts.timeline_path(cfg, dt)
    assert p.name == "2026-05-04.md"
    assert "2026" in str(p) and "05" in str(p)
    ts.ensure_timeline_file(cfg, dt)
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "# 🕐 2026-05-04" in text
    assert "23:30" in text


def test_upsert_fill_and_append(cfg):
    dt = datetime(2026, 5, 4, 10, 0)
    ts.ensure_timeline_file(cfg, dt)
    assert ts.is_slot_empty(cfg, "10:00", dt)
    assert ts.upsert_timeline_slot(cfg, "10:00", "A", dt=dt)
    assert not ts.is_slot_empty(cfg, "10:00", dt)
    assert ts.upsert_timeline_slot(cfg, "10:00", "B", dt=dt)
    text = ts.timeline_path(cfg, dt).read_text(encoding="utf-8")
    assert "10:00" in text and "A" in text and "B" in text and "|" in text


def test_last_completed_slot_at_boundary():
    assert ts.last_completed_slot_at_boundary(datetime(2026, 5, 4, 10, 30, 0)) == "10:00"
    assert ts.last_completed_slot_at_boundary(datetime(2026, 5, 4, 0, 30, 0)) == "00:00"


def test_slot_for_hhmm():
    dt = datetime(2026, 5, 4, 12, 0)
    assert ts.slot_for_hhmm("14:17", dt) == "14:00"


def test_get_last_non_empty_slot(cfg):
    dt = datetime(2026, 5, 4, 15, 0)
    ts.ensure_timeline_file(cfg, dt)
    ts.upsert_timeline_slot(cfg, "12:00", "午饭", dt=dt)
    last = ts.get_last_non_empty_slot(cfg, dt)
    assert "午饭" in last
