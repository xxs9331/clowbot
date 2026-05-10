"""collect_config_errors 严格配置单测。"""

from __future__ import annotations

from pathlib import Path

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


def test_collect_errors_compact_max_chars_out_of_range():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "compact_max_chars": 5},
    }
    assert any("compact_max_chars" in e for e in collect_config_errors(cfg))


def test_collect_errors_checkin_meta_bypass_phrases_type():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "checkin_meta_bypass_phrases": "查看"},
    }
    assert any("checkin_meta_bypass_phrases" in e for e in collect_config_errors(cfg))


def test_collect_errors_checkin_meta_bypass_phrases_empty_string():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "checkin_meta_bypass_phrases": ["  "]},
    }
    assert any("checkin_meta_bypass_phrases[0]" in e for e in collect_config_errors(cfg))


def test_collect_errors_compact_enabled_type():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "compact_enabled": "yes"},
    }
    assert any("compact_enabled" in e for e in collect_config_errors(cfg))


def test_collect_errors_checkin_context_path_type():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "checkin_context_path": 123},
    }
    errs = collect_config_errors(cfg)
    assert any("checkin_context_path" in e for e in errs)


def test_collect_errors_checkin_context_path_absolute():
    abs_path = str(Path.home() / "checkin_context_absolute_test_marker.md")
    assert Path(abs_path).is_absolute()
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "checkin_context_path": abs_path},
    }
    errs = collect_config_errors(cfg)
    assert any("relative to vault.root" in e for e in errs)


def test_collect_errors_checkin_context_path_dotdot():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "checkin_context_path": "a/../../x.md"},
    }
    errs = collect_config_errors(cfg)
    assert any(".." in e for e in errs)


def test_collect_errors_checkin_context_path_ok_relative():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": {**minimal_timeline("/x"), "checkin_context_path": "3-Resources/记忆库/09.md"},
    }
    assert collect_config_errors(cfg) == []


def test_collect_errors_graph_rollout_range():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": minimal_timeline("/x"),
        "graph": {"enabled": True, "shadow_mode": False, "rollout_rate": 1.2, "rollout_users": []},
    }
    errs = collect_config_errors(cfg)
    assert any("graph.rollout_rate" in e for e in errs)


def test_collect_errors_graph_shadow_requires_log_dir():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": minimal_timeline("/x"),
        "graph": {"enabled": True, "shadow_mode": True, "rollout_rate": 0.2, "rollout_users": [], "shadow_log_dir": ""},
    }
    errs = collect_config_errors(cfg)
    assert any("graph.shadow_log_dir" in e for e in errs)


def test_collect_errors_graph_shadow_when_disabled():
    cfg = {
        "vault": minimal_vault("/x"),
        "timeline": minimal_timeline("/x"),
        "graph": {"enabled": False, "shadow_mode": True, "rollout_rate": 0.0, "rollout_users": []},
    }
    errs = collect_config_errors(cfg)
    assert any("graph.shadow_mode must be false when graph.enabled=false" in e for e in errs)
