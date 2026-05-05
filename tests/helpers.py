"""单测共享：满足 ``collect_config_errors`` 的最小 vault/timeline 块。"""

from __future__ import annotations


def minimal_vault(root: str) -> dict[str, str]:
    return {
        "root": root,
        "daily_log_dir": "log",
        "diary_dir": "diary",
        "project_dir": "proj",
        "task_dir": "tasks",
    }


def minimal_timeline(root_dir: str, *, enabled: bool = False) -> dict:
    return {
        "enabled": enabled,
        "root_dir": root_dir,
        "timeline_dir": "时间轴",
        "state_dir": "bot_state",
        "slot_minutes": 30,
        "append_separator": "|",
        "checkin_enabled": False,
        "project_overview_path": "overview.md",
        "checkin_ai_timeout_sec": 12,
        "compact_enabled": True,
        "compact_max_chars": 80,
    }
