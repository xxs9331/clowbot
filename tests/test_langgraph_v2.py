from __future__ import annotations

import asyncio
import pytest

langgraph = pytest.importorskip("langgraph")

from langgraph_v2.contracts import Decision
from langgraph_v2.graph import GraphDeps, build_chat_graph


class _FakeLLM:
    def __init__(self, decision: Decision):
        self.decision = decision
        self.calls = 0

    async def structured_decide(self, *, user_id: str, text: str, queue_snapshot: list[str]) -> Decision:
        self.calls += 1
        return self.decision


class _FakeVault:
    def __init__(self):
        self.records: list[tuple[str, str, str]] = []
        self.reminders: list[tuple[str, str, str]] = []
        self.timeline: list[tuple[str, str]] = []

    async def append_record(self, *, text: str, category: str, event_date: str) -> str:
        self.records.append((text, category, event_date))
        return f"已记录 {category}：{text}"

    async def append_reminder(self, *, text: str, hhmm: str, event_date: str) -> str:
        self.reminders.append((text, hhmm, event_date))
        return f"⏰ 已设提醒：{hhmm} {text}"

    async def upsert_timeline_slot(self, *, slot: str, text: str) -> str:
        self.timeline.append((slot, text))
        return f"已追加到时间轴 {slot or '当前半格'}：{text}"

    async def read_view(self, *, kind: str) -> str:
        return f"VIEW::{kind}"


class _FakeTodo:
    def __init__(self):
        self.tasks: dict[str, list[str]] = {}

    async def merge(self, *, user_id: str, tasks: list[str]) -> str:
        self.tasks.setdefault(user_id, []).extend(tasks)
        return f"新增待办 {len(tasks)} 项"

    async def done_current(self, *, user_id: str) -> tuple[str, str]:
        items = self.tasks.get(user_id) or []
        if not items:
            return "当前没有进行中的待办。", ""
        done = items.pop(0)
        next_item = items[0] if items else ""
        return f"✅ {done} 完成", next_item

    async def next_task(self, *, user_id: str) -> str:
        items = self.tasks.get(user_id) or []
        return items[0] if items else ""


class _FakeImageLLM:
    async def describe_image(self, *, image_base64: str, image_mime: str) -> str:
        _ = image_base64, image_mime
        return "图片内容：午饭轻食"


class _FakeClassifier:
    def __init__(self):
        self.calls = 0

    async def classify(self, *, user_id: str, text: str, queue_snapshot: list[str]) -> dict:
        _ = user_id, text, queue_snapshot
        self.calls += 1
        return {"intent": "record", "score": 0.8}


def _run(coro):
    return asyncio.run(coro)


def test_fast_route_done_current():
    llm = _FakeLLM(Decision(tool="none", payload={}, reply=""))
    vault = _FakeVault()
    todo = _FakeTodo()
    todo.tasks["u1"] = ["任务A", "任务B"]
    app = build_chat_graph(
        GraphDeps(
            llm=llm,
            image_llm=None,
            classifier=None,
            vault=vault,
            todo=todo,
        )
    )

    out = _run(app.ainvoke({"text": "做完了", "from_user": "u1", "context_token": "ctx"}))
    assert llm.calls == 0
    assert out["wx_out"]
    assert "下一个" in out["wx_out"][0]


def test_llm_record_add_path():
    llm = _FakeLLM(
        Decision(
            tool="record.add",
            payload={"text": "今早体重 72kg", "category": "身体", "event_date": "2026-05-09"},
            reply="",
        )
    )
    vault = _FakeVault()
    todo = _FakeTodo()
    cls = _FakeClassifier()
    app = build_chat_graph(
        GraphDeps(
            llm=llm,
            image_llm=None,
            classifier=cls,
            vault=vault,
            todo=todo,
        )
    )

    out = _run(app.ainvoke({"text": "今早体重72kg", "from_user": "u2", "context_token": "ctx"}))
    assert llm.calls == 1
    assert cls.calls == 1
    assert vault.records == [("今早体重 72kg", "身体", "2026-05-09")]
    assert "已记录 身体" in out["wx_out"][0]


def test_slash_reader_path():
    llm = _FakeLLM(Decision(tool="none", payload={}, reply="不会被调用"))
    vault = _FakeVault()
    todo = _FakeTodo()
    app = build_chat_graph(
        GraphDeps(
            llm=llm,
            image_llm=None,
            classifier=None,
            vault=vault,
            todo=todo,
        )
    )

    out = _run(app.ainvoke({"text": "/待办", "from_user": "u3", "context_token": "ctx"}))
    assert llm.calls == 0
    assert out["wx_out"] == ["VIEW::todo"]


def test_none_reply_fallback():
    llm = _FakeLLM(Decision(tool="none", payload={}, reply=""))
    vault = _FakeVault()
    todo = _FakeTodo()
    app = build_chat_graph(
        GraphDeps(
            llm=llm,
            image_llm=None,
            classifier=None,
            vault=vault,
            todo=todo,
        )
    )

    out = _run(app.ainvoke({"text": "asdfghjkl", "from_user": "u4", "context_token": "ctx"}))
    assert llm.calls == 1
    assert "我没看懂这条要怎么记" in out["wx_out"][0]


def test_image_path_smoke():
    llm = _FakeLLM(
        Decision(
            tool="record.add",
            payload={"text": "图片内容：午饭轻食", "category": "事务", "event_date": "2026-05-09"},
            reply="",
        )
    )
    vault = _FakeVault()
    todo = _FakeTodo()
    app = build_chat_graph(
        GraphDeps(
            llm=llm,
            image_llm=_FakeImageLLM(),
            classifier=None,
            vault=vault,
            todo=todo,
        )
    )

    out = _run(
        app.ainvoke(
            {
                "text": "",
                "image_base64": "dGVzdA==",
                "image_mime": "image/png",
                "from_user": "u5",
                "context_token": "ctx",
            }
        )
    )
    assert llm.calls == 1
    assert vault.records
    assert "已记录" in out["wx_out"][0]
