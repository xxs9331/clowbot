from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from langgraph_v2.dispatcher import DualPathDispatcher


def _run(coro):
    return asyncio.run(coro)


class _FakeGraph:
    def __init__(self, reply: str = "graph-reply"):
        self.calls: list[dict] = []
        self.reply = reply

    async def ainvoke(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {"reply": self.reply, "wx_out": [self.reply], "decision": {"tool": "none"}}


async def _legacy_counter(counter: dict):
    counter["n"] += 1


async def _send_collector(buf: list[str], text: str, to_user: str, context_token: str):
    _ = to_user, context_token
    buf.append(text)


def _tmp_dir() -> Path:
    p = Path(".clawbot_tmp") / "pytest-shadow" / f"clawbot-v2-{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_dispatcher_disabled_uses_legacy_only():
    tmp_path = _tmp_dir()
    graph = _FakeGraph()
    d = DualPathDispatcher(graph=graph, config={"graph": {"enabled": False}})
    legacy = {"n": 0}
    sent: list[str] = []
    _run(
        d.dispatch_text(
            text="hello",
            from_user="u1",
            context_token="c1",
            legacy_runner=lambda: _legacy_counter(legacy),
            send_text=lambda t, u, c: _send_collector(sent, t, u, c),
        )
    )
    assert legacy["n"] == 1
    assert sent == []
    assert not graph.calls


def test_dispatcher_visible_rollout_uses_graph_reply():
    tmp_path = _tmp_dir()
    graph = _FakeGraph("visible")
    d = DualPathDispatcher(
        graph=graph,
        config={"graph": {"enabled": True, "shadow_mode": False, "rollout_users": ["u2"]}},
    )
    legacy = {"n": 0}
    sent: list[str] = []
    _run(
        d.dispatch_text(
            text="hello",
            from_user="u2",
            context_token="c2",
            legacy_runner=lambda: _legacy_counter(legacy),
            send_text=lambda t, u, c: _send_collector(sent, t, u, c),
        )
    )
    assert legacy["n"] == 0
    assert sent == ["visible"]
    assert len(graph.calls) == 1


def test_dispatcher_shadow_keeps_legacy_and_runs_graph():
    tmp_path = _tmp_dir()
    graph = _FakeGraph("shadow")
    d = DualPathDispatcher(
        graph=graph,
        config={
            "graph": {
                "enabled": True,
                "shadow_mode": True,
                "rollout_users": ["u3"],
                "shadow_log_dir": str(tmp_path),
            }
        },
    )
    legacy = {"n": 0}
    sent: list[str] = []
    _run(
        d.dispatch_text(
            text="hello",
            from_user="u3",
            context_token="c3",
            legacy_runner=lambda: _legacy_counter(legacy),
            send_text=lambda t, u, c: _send_collector(sent, t, u, c),
        )
    )
    # Shadow task is async fire-and-forget; wait a short tick.
    _run(asyncio.sleep(0.05))
    assert legacy["n"] == 1
    assert sent == []
    assert len(graph.calls) == 1
    logs = list(tmp_path.glob("shadow-*.log"))
    assert logs, "shadow log should be written"


def test_dispatcher_rollout_rate_zero_non_whitelist_falls_back_legacy():
    tmp_path = _tmp_dir()
    graph = _FakeGraph("x")
    d = DualPathDispatcher(
        graph=graph,
        config={"graph": {"enabled": True, "shadow_mode": False, "rollout_rate": 0.0, "rollout_users": []}},
    )
    legacy = {"n": 0}
    sent: list[str] = []
    _run(
        d.dispatch_text(
            text="hello",
            from_user="u4",
            context_token="c4",
            legacy_runner=lambda: _legacy_counter(legacy),
            send_text=lambda t, u, c: _send_collector(sent, t, u, c),
        )
    )
    assert legacy["n"] == 1
    assert sent == []
    assert not graph.calls
