"""checkin 注入共享对话窗口摘要。"""

from __future__ import annotations

from scheduler.checkin import _checkin_chat_tail, _checkin_wx_target_user


class _Wx:
    def __init__(self, *, tokens: dict | None = None, user_id: str = "") -> None:
        self._context_tokens = dict(tokens or {})
        self.user_id = user_id


class _Handler:
    def __init__(self, cfg: dict, wx: _Wx) -> None:
        self.cfg = cfg
        self.wx = wx

    def _build_shared_history(self, user_id: str) -> str:
        return f"mock_history_for:{user_id}"


def test_checkin_wx_target_user_prefers_context_last_key() -> None:
    wx = _Wx(tokens={"u-a": "t1", "u-b": "t2"})
    h = _Handler({}, wx)
    assert _checkin_wx_target_user(h) == "u-b"


def test_checkin_wx_target_user_falls_back_to_user_id() -> None:
    wx = _Wx(tokens={}, user_id="fallback-wx")
    h = _Handler({}, wx)
    assert _checkin_wx_target_user(h) == "fallback-wx"


def test_checkin_chat_tail_calls_handler_when_enabled() -> None:
    cfg = {"timeline": {"checkin_include_chat_history": True}}
    h = _Handler(cfg, _Wx())
    assert _checkin_chat_tail(h, cfg, "wx-1") == "mock_history_for:wx-1"


def test_checkin_chat_tail_empty_when_disabled() -> None:
    cfg = {"timeline": {"checkin_include_chat_history": False}}
    h = _Handler(cfg, _Wx())
    assert _checkin_chat_tail(h, cfg, "wx-1") == ""


def test_checkin_chat_tail_empty_without_method() -> None:
    class H:
        def __init__(self) -> None:
            self.wx = _Wx(user_id="x")

    cfg = {"timeline": {"checkin_include_chat_history": True}}
    assert _checkin_chat_tail(H(), cfg, "wx-1") == ""


def test_checkin_chat_tail_default_enabled_omitted_key() -> None:
    cfg = {"timeline": {}}
    h = _Handler(cfg, _Wx())
    assert _checkin_chat_tail(h, cfg, "u") == "mock_history_for:u"
