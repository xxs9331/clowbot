"""路由记忆与意图短路保护单测（无 ACP）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from handlers.base import Handler


class _DummyWX:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str, _to: str, _token: str):
        self.sent.append(text)


def _build_handler(tmp_path: Path) -> Handler:
    cfg = {
        "vault": {"root": str(tmp_path), "daily_log_dir": "2-Areas/习惯养成/生活日志"},
        "bot": {"tool_progress_messages": True, "tool_progress_min_interval_sec": 0.5},
        "opencode": {"memory_spill_chars": 40},
    }
    return Handler(acp=None, config=cfg, wechat=_DummyWX())  # type: ignore[arg-type]


def test_block_record_add_when_global_short_circuit_disabled():
    obj = {"intent": "record_add", "slots": {"text": "任意"}, "confidence": 0.99}
    assert Handler._block_intent_short_circuit(obj, "任意", {"record_high_conf_short_circuit": False}) is True


def test_block_record_add_short_message_when_short_circuit_enabled():
    obj = {"intent": "record_add", "slots": {"text": "短"}, "confidence": 0.99}
    assert (
        Handler._block_intent_short_circuit(obj, "今天吃了面", {"record_high_conf_short_circuit": True})
        is True
    )


def test_no_block_record_add_when_event_date_present():
    obj = {
        "intent": "record_add",
        "slots": {"text": "她也改签了", "event_date": "2026-05-02"},
        "confidence": 0.99,
    }
    assert (
        Handler._block_intent_short_circuit(obj, "她也改签了", {"record_high_conf_short_circuit": True})
        is False
    )


def test_no_block_non_record_intent():
    obj = {"intent": "todo_done", "slots": {}, "confidence": 0.99}
    assert Handler._block_intent_short_circuit(obj, "搞定", {"record_high_conf_short_circuit": False}) is False


def test_spill_large_payload_for_memory(tmp_path: Path):
    h = _build_handler(tmp_path)
    payload = {"text": "x" * 120, "category": "事务"}
    out = h._maybe_spill_payload_for_memory(user_id="u123", tool="record.add", payload=payload)
    assert out.get("_spilled") is True
    fp = Path(str(out.get("spill_file")))
    assert fp.exists()
    assert fp.read_text(encoding="utf-8").startswith("{")


def test_spill_followup_uses_lower_threshold(tmp_path: Path):
    cfg = {
        "vault": {"root": str(tmp_path), "daily_log_dir": "x"},
        "bot": {},
        "opencode": {"memory_spill_chars": 500, "memory_spill_chars_followup": 80},
    }
    h = Handler(acp=None, config=cfg, wechat=_DummyWX())  # type: ignore[arg-type]
    h._user_route_memory["u1"] = [{"tool": "record.add", "payload": {"text": "prior"}}]
    payload = {"text": "y" * 100, "category": "事务"}
    out = h._maybe_spill_payload_for_memory(user_id="u1", tool="record.add", payload=payload)
    assert out.get("_spilled") is True


def test_dispatcher_sanitize_vague_none_keeps_evidence():
    from handlers.dispatcher import DispatcherMixin

    with_evidence = "刚给你列过\n- `9417ccd` feat: x"
    assert "重发完整列表" not in DispatcherMixin._sanitize_vague_none_reply(
        with_evidence, "none"
    )


def test_tool_status_debounce(tmp_path: Path):
    h = _build_handler(tmp_path)
    wx = h.wx
    asyncio.run(
        h._emit_tool_status(
            from_user="u1",
            context_token="",
            tool="record.add",
            phase="executing",
        )
    )
    asyncio.run(
        h._emit_tool_status(
            from_user="u1",
            context_token="",
            tool="record.add",
            phase="executing",
        )
    )
    assert isinstance(wx.sent, list)
    assert len(wx.sent) == 1


def test_structured_state_collects_list_str_summary(tmp_path: Path):
    h = _build_handler(tmp_path)
    decision = {
        "tool": "record.add",
        "payload": {"text": "体重 71.2kg", "category": "身体", "event_date": "2026-05-03"},
        "reply": "",
    }
    h._update_structured_state("u1", decision=decision, handled=True, user_text="体重 71.2kg")
    st = h._get_or_init_structured_state("u1")
    collected = st.get("collected_data") or []
    assert isinstance(collected, list)
    assert any("记录: 体重 71.2kg" in str(x) for x in collected)


def test_structured_state_context_block_contains_collected_data(tmp_path: Path):
    h = _build_handler(tmp_path)
    h._update_structured_state(
        "u1",
        decision={"tool": "remind.add", "payload": {"hhmm": "09:15", "text": "喝水"}, "reply": ""},
        handled=True,
        user_text="09:15 提醒我喝水",
    )
    block = h._structured_state_context_block("u1")
    assert "structured_state" in block
    assert "09:15" in block
