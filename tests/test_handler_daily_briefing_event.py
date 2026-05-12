"""daily_briefing immediate 事件渲染测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from handlers.base import Handler
from tests.helpers import minimal_timeline, minimal_vault


class _DummyACP:
    def __init__(self) -> None:
        self.prompt_called = False
        self.permission_handler = None

    def set_permission_request_handler(self, handler) -> None:  # noqa: ANN001
        self.permission_handler = handler

    async def prompt(self, session_id: str, prompt: str, trace_tag: str = ""):  # noqa: ARG002
        self.prompt_called = True
        return "fallback", ""


class _DummyWX:
    def __init__(self) -> None:
        self.user_id = "wx-user"
        self._context_tokens: dict[str, str] = {}


def test_render_immediate_daily_briefing_uses_summary_directly() -> None:
    cfg = {
        "vault": minimal_vault("/tmp/v"),
        "timeline": minimal_timeline("/tmp/v"),
        "bot": {"unified_chat_mode": True},
    }
    acp = _DummyACP()
    h = Handler(acp=acp, config=cfg, wechat=_DummyWX())  # type: ignore[arg-type]
    h.unified_session_id = "unified-sid"

    top = SimpleNamespace(kind="daily_briefing", summary="早上好，今天记得先做最重要的一件事。")
    out = asyncio.run(h._render_immediate_event_message(top, "wx-user"))
    assert "早上好" in out
    assert acp.prompt_called is False
