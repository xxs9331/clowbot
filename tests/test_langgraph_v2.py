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
        self.last_intent_hint: dict | None = None

    async def structured_decide(
        self,
        *,
        user_id: str,
        text: str,
        queue_snapshot: list[str],
        intent_hint: dict | None = None,
    ) -> Decision:
        self.calls += 1
        self.last_intent_hint = intent_hint
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
        self.pending_reorder: dict[str, list[str]] = {}

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

    async def not_done(self, *, user_id: str) -> str:
        cur = await self.next_task(user_id=user_id)
        if cur:
            return f"先做1分钟版本：{cur}，做好再回我“好了”。"
        return "没问题，你先发几个待办我来排。"

    async def reorder(self, *, user_id: str, order: list[str]) -> str:
        self.pending_reorder[user_id] = list(order)
        return f"我建议顺序：{' -> '.join(order)}。按这个顺序更新吗？"

    async def reorder_confirm(self, *, user_id: str) -> str:
        order = self.pending_reorder.pop(user_id, [])
        if not order:
            return "当前没有待确认的重排建议。"
        self.tasks[user_id] = list(order)
        return "已按确认顺序更新。"

    async def skip_current(self, *, user_id: str) -> tuple[str, str]:
        items = self.tasks.get(user_id) or []
        if not items:
            return "当前没有可跳过的待办。", ""
        cur = items.pop(0)
        items.append(cur)
        return f"先跳过：{cur}。", (items[0] if items else "")

    async def abandon_current(self, *, user_id: str) -> tuple[str, str]:
        items = self.tasks.get(user_id) or []
        if not items:
            return "当前没有可放弃的待办。", ""
        cur = items.pop(0)
        if items:
            return f"已放弃：{cur}。", items[0]
        return f"已放弃：{cur}。当前没有进行中的待办。", ""


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


def test_fast_route_done_current_skips_pre_intent():
    llm = _FakeLLM(Decision(tool="none", payload={}, reply=""))
    vault = _FakeVault()
    todo = _FakeTodo()
    cls = _FakeClassifier()
    todo.tasks["u1"] = ["任务A", "任务B"]
    app = build_chat_graph(
        GraphDeps(
            llm=llm,
            image_llm=None,
            classifier=cls,
            vault=vault,
            todo=todo,
        )
    )

    out = _run(app.ainvoke({"text": "做完了", "from_user": "u1", "context_token": "ctx"}))
    assert llm.calls == 0
    assert cls.calls == 0
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
    assert llm.last_intent_hint == {"intent": "record", "score": 0.8}
    assert vault.records == [("今早体重 72kg", "身体", "2026-05-09")]
    assert "已记录 身体" in out["wx_out"][0]


def test_empty_payload_record_add_backfills_user_text():
    """模型只给 tool、payload 无 text 时，execute 应用用户原文，避免「我没读懂这条记录。」"""
    llm = _FakeLLM(Decision(tool="record.add", payload={}, reply="记上了"))
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
    out = _run(app.ainvoke({"text": "刚才吃药了", "from_user": "u_backfill_r", "context_token": "ctx"}))
    assert vault.records and vault.records[0][0] == "刚才吃药了"
    assert "已记录" in out["wx_out"][0]


def test_empty_payload_timeline_append_backfills_user_text():
    llm = _FakeLLM(Decision(tool="timeline.append", payload={}, reply="好嘞"))
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
    out = _run(app.ainvoke({"text": "吃药了", "from_user": "u_backfill_t", "context_token": "ctx"}))
    assert vault.timeline and vault.timeline[0][1] == "吃药了"
    assert "时间轴" in out["wx_out"][0]


def test_timeline_payload_uses_content_and_time_aliases():
    """模型用 content/time 而非 text/slot 时，执行前归一化。"""
    llm = _FakeLLM(
        Decision(
            tool="timeline.append",
            payload={"time": "03:00", "content": "记账"},
            reply="好嘞",
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
    out = _run(app.ainvoke({"text": "ok，记了", "from_user": "u_alias_tl", "context_token": "ctx"}))
    assert vault.timeline == [("03:00", "记账")]
    assert "时间轴" in out["wx_out"][0]


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
    assert "收到" in out["wx_out"][0]


def test_slash_log_alias_path():
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

    out = _run(app.ainvoke({"text": "/查看日志", "from_user": "u6", "context_token": "ctx"}))
    assert llm.calls == 0
    assert out["wx_out"] == ["VIEW::log"]


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


def test_fast_route_todo_merge_items_skips_llm_and_classifier():
    llm = _FakeLLM(Decision(tool="none", payload={}, reply=""))
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
    out = _run(
        app.ainvoke(
            {"text": "添加待办：买牛奶，写报告", "from_user": "u7", "context_token": "ctx"}
        )
    )
    assert llm.calls == 0
    assert cls.calls == 0
    assert todo.tasks.get("u7") == ["买牛奶", "写报告"]
    assert "新增待办 2 项" in out["wx_out"][0]


def test_remind_path_goes_pre_intent_then_llm_decide():
    llm = _FakeLLM(
        Decision(
            tool="remind.add",
            payload={"text": "开会", "hhmm": "08:00", "event_date": "2026-05-10"},
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
    out = _run(
        app.ainvoke({"text": "明天8点提醒我开会", "from_user": "u8", "context_token": "ctx"})
    )
    assert llm.calls == 1
    assert cls.calls == 1
    assert vault.reminders == [("开会", "08:00", "2026-05-10")]
    assert "已设提醒" in out["wx_out"][0]


def test_none_chat_path_uses_llm_reply_and_calls_classifier():
    llm = _FakeLLM(Decision(tool="none", payload={}, reply="今天辛苦了，先休息一下。"))
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
    out = _run(app.ainvoke({"text": "今天好累", "from_user": "u9", "context_token": "ctx"}))
    assert llm.calls == 1
    assert cls.calls == 1
    assert out["wx_out"] == ["今天辛苦了，先休息一下。"]
