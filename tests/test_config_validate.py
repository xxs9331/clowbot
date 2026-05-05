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


def test_collect_errors_checkin_poll_interval_invalid():
    base = {"vault": minimal_vault("/x"), "timeline": minimal_timeline("/x")}
    assert collect_config_errors(base) == []
    cfg_bad = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "checkin_poll_interval_sec": 5},
    }
    assert any("checkin_poll_interval" in e for e in collect_config_errors(cfg_bad))


def test_collect_errors_timeline_write_policy_invalid():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "write_policy": "allow_all"},
    }
    assert any("write_policy" in e for e in collect_config_errors(cfg))


def test_collect_errors_checkin_write_if_filled_type():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "checkin_write_if_filled": "false"},
    }
    assert any("checkin_write_if_filled" in e for e in collect_config_errors(cfg))
