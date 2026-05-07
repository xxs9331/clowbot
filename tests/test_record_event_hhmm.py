"""record.add 的 event_hhmm：Vault now_hhmm 与时间轴半格一致。"""

from __future__ import annotations

import asyncio

import handlers.coaches  # noqa: F401 — 触发 register_tool_handler
import pytest

import handlers.coaches.record as record_mod
from handlers.coaches.record import RecordCoachMixin
from tests.helpers import minimal_timeline, minimal_vault
from utils.timeline_sync import ensure_timeline_file, get_slot_body


class _Wx:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_text(self, msg: str, user: str, token: str) -> None:
        self.messages.append((msg, user, token))

    async def set_typing(self, *, to_user: str, status: int, context_token: str) -> None:
        pass


class _ACP:
    async def prompt(self, *_a, **_kw):
        return "已记下", ""


class _H(RecordCoachMixin):
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.wx = _Wx()
        self.acp = _ACP()
        self.record_session_id = "rec"
        self.unified_session_id = "uni"


@pytest.fixture
def record_cfg(tmp_path) -> dict:
    v = tmp_path / "vault"
    v.mkdir()
    root = str(v)
    return {
        "vault": minimal_vault(root),
        "bot": {"max_reply_length": 2000},
        "timeline": {
            **minimal_timeline(root, enabled=True),
            "timeline_dir": "时间轴",
            "state_dir": "bot_state",
        },
    }


def test_event_hhmm_writes_15_slot_not_wall_clock(
    record_cfg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = __import__("datetime").datetime(2026, 5, 4, 20, 0, 0)

    class _Dt:
        @staticmethod
        def now():
            return fixed

        @staticmethod
        def strptime(s: str, fmt: str):
            return __import__("datetime").datetime.strptime(s, fmt)

    monkeypatch.setattr(record_mod, "datetime", _Dt)
    monkeypatch.setattr(record_mod, "time_str", lambda *_a, **_k: "20:00")

    payloads_seen: list[dict] = []

    def spy_build(domain, *, vault_root, daily_log_dir, payload, output_contract):
        payloads_seen.append(dict(payload))
        return "prompt"

    monkeypatch.setattr(record_mod, "build_coach_write_prompt", spy_build)

    h = _H(record_cfg)
    ensure_timeline_file(record_cfg, fixed)

    async def run() -> None:
        await h._coach_record_add(
            {"text": "吃了弥宁", "category": "身体", "event_hhmm": "15:00"},
            "",
            "u1",
            "tok",
        )

    asyncio.run(run())

    assert payloads_seen and payloads_seen[0]["now_hhmm"] == "15:00"
    assert get_slot_body(record_cfg, "15:00", fixed) == "已记下"
    assert get_slot_body(record_cfg, "20:00", fixed) == ""


def test_invalid_event_hhmm_falls_back_to_time_str(
    record_cfg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = __import__("datetime").datetime(2026, 5, 4, 12, 0, 0)

    class _Dt:
        @staticmethod
        def now():
            return fixed

        @staticmethod
        def strptime(s: str, fmt: str):
            return __import__("datetime").datetime.strptime(s, fmt)

    monkeypatch.setattr(record_mod, "datetime", _Dt)
    monkeypatch.setattr(record_mod, "time_str", lambda *_a, **_k: "12:00")

    payloads_seen: list[dict] = []

    def spy_build(domain, *, vault_root, daily_log_dir, payload, output_contract):
        payloads_seen.append(dict(payload))
        return "ok"

    monkeypatch.setattr(record_mod, "build_coach_write_prompt", spy_build)

    h = _H(record_cfg)
    ensure_timeline_file(record_cfg, fixed)

    async def run() -> None:
        await h._coach_record_add(
            {"text": "测试", "event_hhmm": "99:00"},
            "",
            "u1",
            "tok",
        )

    asyncio.run(run())

    assert payloads_seen[0]["now_hhmm"] == "12:00"
    assert get_slot_body(record_cfg, "12:00", fixed) == "已记下"
