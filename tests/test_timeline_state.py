"""timeline_state 单测（临时 vault）。"""

from __future__ import annotations

from pathlib import Path

from tests.helpers import minimal_timeline, minimal_vault
from utils.timeline_state import get_state, set_state


def test_state_roundtrip(tmp_path: Path):
    root = str(tmp_path / "v")
    (tmp_path / "v").mkdir()
    cfg = {"vault": minimal_vault(root), "timeline": minimal_timeline(root)}
    assert get_state(cfg) == "sleep"
    set_state(cfg, "active")
    assert get_state(cfg) == "active"
    set_state(cfg, "sleep")
    assert get_state(cfg) == "sleep"
