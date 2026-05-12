"""scheduler.briefing 单测。"""

from __future__ import annotations

import asyncio

import pytest

import scheduler.briefing as briefing_mod


def _briefing_cfg() -> dict:
    return {
        "vault": {
            "root": ".",
            "daily_log_dir": "2-Areas/习惯养成/生活日志",
            "diary_dir": "2-Areas/习惯养成/日记",
            "project_dir": "1-Projects/生生",
            "task_dir": "任务系统",
        },
        "timeline": {
            "enabled": False,
            "root_dir": ".",
            "timeline_dir": "2-Areas/习惯养成/时间轴",
            "state_dir": "@bot",
            "slot_minutes": 30,
            "append_separator": "|",
            "checkin_enabled": False,
            "project_overview_path": "overview.md",
            "checkin_ai_timeout_sec": 12,
            "compact_enabled": True,
            "compact_max_chars": 80,
            "write_policy": "whitelist_only",
            "checkin_write_if_filled": False,
        },
        "briefing": {
            "enabled": True,
            "push_hour": 7,
            "push_minute": 0,
            "weather": {
                "api_host": "https://api.test.local",
                "location": "108.08,34.27",
                "indices": [3, 1, 5, 8, 9],
            },
        },
    }


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):  # noqa: ANN001
        return dict(self._payload)


class _FakeSession:
    def __init__(self, responses: list[object], timeout=None):  # noqa: ANN001
        self._responses = responses
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, params: dict):  # noqa: ARG002
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_fetch_weather_missing_key_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEATHER_API_KEY", raising=False)
    out = asyncio.run(briefing_mod._fetch_weather(_briefing_cfg()))
    assert out.get("error") == "missing_api_key"


def test_fetch_weather_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEATHER_API_KEY", "test-key")
    responses: list[object] = [
        _FakeResponse(
            200,
            {
                "code": "200",
                "daily": [
                    {
                        "fxDate": "2026-05-12",
                        "tempMax": "29",
                        "tempMin": "16",
                        "textDay": "阴",
                        "textNight": "小雨",
                        "windScaleDay": "1-3级",
                        "windDirDay": "北风",
                        "precip": "0.0",
                        "humidity": "63",
                        "uvIndex": "5",
                        "sunrise": "05:49",
                        "sunset": "19:40",
                    }
                ],
            },
        ),
        _FakeResponse(
            200,
            {
                "code": "200",
                "daily": [
                    {"name": "穿衣指数", "category": "舒适", "text": "建议着长袖"},
                    {"name": "运动指数", "category": "较适宜", "text": "适度运动"},
                ],
            },
        ),
    ]
    monkeypatch.setattr(
        briefing_mod.aiohttp,
        "ClientSession",
        lambda timeout=None: _FakeSession(responses, timeout=timeout),
    )

    out = asyncio.run(briefing_mod._fetch_weather(_briefing_cfg()))
    assert out.get("error") is None
    assert out.get("date") == "2026-05-12"
    assert out.get("temp_high") == "29"
    assert len(out.get("indices") or []) == 2


def test_fetch_weather_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEATHER_API_KEY", "test-key")
    responses: list[object] = [_FakeResponse(500, {"code": "500"})]
    monkeypatch.setattr(
        briefing_mod.aiohttp,
        "ClientSession",
        lambda timeout=None: _FakeSession(responses, timeout=timeout),
    )
    out = asyncio.run(briefing_mod._fetch_weather(_briefing_cfg()))
    assert out.get("error") == "weather_http_500"


def test_fetch_weather_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEATHER_API_KEY", "test-key")
    responses: list[object] = [asyncio.TimeoutError()]
    monkeypatch.setattr(
        briefing_mod.aiohttp,
        "ClientSession",
        lambda timeout=None: _FakeSession(responses, timeout=timeout),
    )
    out = asyncio.run(briefing_mod._fetch_weather(_briefing_cfg()))
    assert out.get("error") == "timeout"


def test_briefing_loop_emits_immediate_event(monkeypatch: pytest.MonkeyPatch) -> None:
    class _WX:
        async def send_text(self, text: str, user: str, token: str = "") -> None:  # noqa: ARG002
            raise AssertionError("briefing_loop 不应直接发送微信消息")

    class _Handler:
        def __init__(self) -> None:
            self.cfg = _briefing_cfg()
            self.wx = _WX()
            self.session_id = "sid"
            self.unified_session_id = "usid"
            self.acp = object()
            self.events: list[dict] = []

        def add_background_event(self, **kwargs) -> str:
            self.events.append(dict(kwargs))
            raise asyncio.CancelledError()

    async def _noop_sleep(hour: int, minute: int) -> None:  # noqa: ARG001
        return None

    async def _fake_fetch(cfg: dict) -> dict:  # noqa: ARG001
        return {"date": "2026-05-12", "indices": []}

    async def _fake_generate(handler, weather: dict, context: dict) -> str:  # noqa: ANN001, ARG001
        return "早上好，今天也稳步推进。"

    monkeypatch.setattr(briefing_mod, "_sleep_until", _noop_sleep)
    monkeypatch.setattr(briefing_mod, "_fetch_weather", _fake_fetch)
    monkeypatch.setattr(briefing_mod, "_generate_briefing", _fake_generate)
    monkeypatch.setattr(
        briefing_mod,
        "_read_vault_context",
        lambda handler: {"tasks_and_reminders": "暂无", "timeline_recent": "暂无", "diary_tail": "暂无"},
    )

    h = _Handler()
    asyncio.run(briefing_mod.briefing_loop(h))

    assert len(h.events) == 1
    ev = h.events[0]
    assert ev.get("kind") == "daily_briefing"
    assert ev.get("priority") == "immediate"
    assert "早上好" in str(ev.get("summary") or "")
