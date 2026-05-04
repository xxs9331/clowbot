"""collect_config_errors 严格配置单测。"""

from __future__ import annotations

from config import collect_config_errors

from tests.helpers import minimal_timeline, minimal_vault


def test_collect_errors_empty_when_full_config():
    cfg = {
        "vault": minimal_vault("/tmp/v"),
        "timeline": minimal_timeline("/tmp/v"),
    }
    assert collect_config_errors(cfg) == []


def test_collect_errors_missing_vault_key():
    cfg = {
        "vault": {"root": "x", "daily_log_dir": "a"},
        "timeline": minimal_timeline("x"),
    }
    errs = collect_config_errors(cfg)
    assert any("diary_dir" in e for e in errs)


def test_collect_errors_missing_timeline_section():
    assert any("timeline" in e.lower() for e in collect_config_errors({"vault": minimal_vault("x")}))


def test_collect_errors_slot_minutes_not_30():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "slot_minutes": 15},
    }
    assert any("slot_minutes" in e for e in collect_config_errors(cfg))
