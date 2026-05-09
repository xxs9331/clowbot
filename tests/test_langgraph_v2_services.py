from __future__ import annotations

import asyncio

from langgraph_v2.services import DomainServices


def _run(coro):
    return asyncio.run(coro)


class _FakeVault:
    async def append_record(self, *, text: str, category: str, event_date: str) -> str:
        return f"R:{category}:{event_date}:{text}"

    async def append_reminder(self, *, text: str, hhmm: str, event_date: str) -> str:
        return f"M:{event_date}:{hhmm}:{text}"

    async def upsert_timeline_slot(self, *, slot: str, text: str) -> str:
        return f"T:{slot}:{text}"

    async def read_view(self, *, kind: str) -> str:
        return f"V:{kind}"


class _FakeTodo:
    async def merge(self, *, user_id: str, tasks: list[str]) -> str:
        return f"merge:{user_id}:{len(tasks)}"

    async def done_current(self, *, user_id: str) -> tuple[str, str]:
        return "done", "next_task"

    async def next_task(self, *, user_id: str) -> str:
        return "next_task"

    async def not_done(self, *, user_id: str) -> str:
        return "not_done"

    async def reorder(self, *, user_id: str, order: list[str]) -> str:
        return "reordered"

    async def reorder_confirm(self, *, user_id: str) -> str:
        return "confirmed"

    async def skip_current(self, *, user_id: str) -> tuple[str, str]:
        return "skip", "nxt"

    async def abandon_current(self, *, user_id: str) -> tuple[str, str]:
        return "abandon", ""


def test_services_none():
    svc = DomainServices(_FakeVault(), _FakeTodo())
    handled, out = _run(svc.execute(user_id="u1", tool="none", payload={}))
    assert handled is True
    assert out == ""


def test_services_record_empty_text():
    svc = DomainServices(_FakeVault(), _FakeTodo())
    handled, out = _run(
        svc.execute(user_id="u1", tool="record.add", payload={"text": "   ", "category": "身体"})
    )
    assert handled is True
    assert out == "我没读懂这条记录。"


def test_services_remind_missing_hhmm():
    svc = DomainServices(_FakeVault(), _FakeTodo())
    handled, out = _run(
        svc.execute(user_id="u1", tool="remind.add", payload={"text": "喝水", "hhmm": ""})
    )
    assert handled is True
    assert out == "几点叫你？请补一个时间。"


def test_services_todo_reorder_invalid_payload():
    svc = DomainServices(_FakeVault(), _FakeTodo())
    handled, out = _run(svc.execute(user_id="u1", tool="todo.reorder", payload={"reorder": "bad"}))
    assert handled is True
    assert "还不能安全改队列" in out


def test_services_unknown_tool():
    svc = DomainServices(_FakeVault(), _FakeTodo())
    handled, out = _run(svc.execute(user_id="u1", tool="unknown.tool", payload={}))
    assert handled is False
    assert out == ""
