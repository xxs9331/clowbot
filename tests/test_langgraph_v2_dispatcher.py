from __future__ import annotations

import asyncio

from langgraph_v2.dispatcher import ChatGraphDispatcher


def _run(coro):
    return asyncio.run(coro)


class _FakeGraph:
    def __init__(self, reply: str = "graph-reply"):
        self.calls: list[dict] = []
        self.reply = reply

    async def ainvoke(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {"reply": self.reply, "wx_out": [self.reply], "decision": {"tool": "none"}}


async def _send_collector(buf: list[str], text: str, to_user: str, context_token: str):
    _ = to_user, context_token
    buf.append(text)


def test_chat_graph_dispatcher_sends_reply_and_passes_queue_snapshot():
    graph = _FakeGraph("hi")
    d = ChatGraphDispatcher(graph=graph)
    sent: list[str] = []
    state = _run(
        d.dispatch(
            text="hello",
            from_user="u1",
            context_token="c1",
            queue_snapshot=["a", "b"],
            send_text=lambda t, u, c: _send_collector(sent, t, u, c),
        )
    )
    assert sent == ["hi"]
    assert len(graph.calls) == 1
    assert graph.calls[0]["queue_snapshot"] == ["a", "b"]
    rid = str(graph.calls[0].get("request_id") or "")
    mt = str(graph.calls[0].get("msg_trace") or "")
    assert len(rid) == 32
    assert mt == rid[:12]
    assert state.get("request_id") == rid
    assert state.get("msg_trace") == mt
    assert state.get("_dispatch_reply_sent") == "hi"


def test_chat_graph_dispatcher_empty_reply_gets_fallback():
    class _G:
        async def ainvoke(self, payload: dict) -> dict:
            return {"reply": "", "wx_out": [], "decision": {"tool": "todo.done_current"}}

    d = ChatGraphDispatcher(graph=_G())
    sent: list[str] = []
    _run(
        d.dispatch(
            text="x",
            from_user="u1",
            context_token="c1",
            queue_snapshot=[],
            send_text=lambda t, u, c: _send_collector(sent, t, u, c),
        )
    )
    assert sent == ["处理完成。"]


def test_chat_graph_dispatcher_records_error_reply():
    class _Bad:
        async def ainvoke(self, payload: dict) -> dict:
            raise RuntimeError("boom")

    d = ChatGraphDispatcher(graph=_Bad())
    sent: list[str] = []
    state = _run(
        d.dispatch(
            text="x",
            from_user="u1",
            context_token="c1",
            queue_snapshot=[],
            send_text=lambda t, u, c: _send_collector(sent, t, u, c),
        )
    )
    assert sent and "处理出错" in sent[0]
    assert "boom" in state.get("error", "")
