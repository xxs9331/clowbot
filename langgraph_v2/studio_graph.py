"""LangSmith Studio / `langgraph dev` 入口：使用内存假依赖，不连真实微信与 Vault。

运行（仓库根 `.clawbot`）::

    pip install -U "langgraph-cli[inmem]"
    langgraph dev

浏览器打开终端输出的 Studio 链接（baseUrl 指向本机 2024 端口）。

要在 LangSmith 里看到 runs：在仓库根复制 ``.env.example`` 为 ``.env``，填入 ``LANGSMITH_API_KEY``，重启 ``langgraph dev``。

可在本文件顶部修改 ``_DEFAULT_DECISION``，以在 Studio 里走不同 tool 路径。
"""

from __future__ import annotations

from langgraph_v2.contracts import Decision
from langgraph_v2.graph import GraphDeps, build_chat_graph

# Studio 默认走一条完整 LLM→execute 链路；可按需改成 Decision(tool="none", ...)
_DEFAULT_DECISION = Decision(
    tool="record.add",
    payload={"text": "Studio 测试记录", "category": "其他", "event_date": "2026-05-10"},
    reply="",
)


class _StudioFakeLLM:
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
        msg_trace: str = "",
    ) -> Decision:
        _ = text, queue_snapshot, msg_trace
        self.calls += 1
        self.last_intent_hint = intent_hint
        return self.decision


class _StudioFakeVault:
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


class _StudioFakeTodo:
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


class _StudioFakeClassifier:
    async def classify(self, *, user_id: str, text: str, queue_snapshot: list[str]) -> dict:
        _ = user_id, text, queue_snapshot
        return {"intent": "record", "score": 0.8}


graph = build_chat_graph(
    GraphDeps(
        llm=_StudioFakeLLM(_DEFAULT_DECISION),
        image_llm=None,
        classifier=_StudioFakeClassifier(),
        vault=_StudioFakeVault(),
        todo=_StudioFakeTodo(),
    )
)
