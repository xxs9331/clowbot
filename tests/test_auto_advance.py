"""P1b：LangGraph fast_rule + execute 路径 todo.done_current 推进。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from handlers.base import Handler
from tests.evals.support import EvalDummyWX, MinimalEvalACP, merge_config_for_eval


@pytest.fixture
def p1b_handler(tmp_path: Path) -> Handler:
    cfg = merge_config_for_eval(tmp_path, None)
    h = Handler(acp=MinimalEvalACP(), config=cfg, wechat=EvalDummyWX())  # type: ignore[arg-type]
    h.unified_session_id = "test-p1b-unified"
    h.session_id = h.unified_session_id
    h.todo_session_id = "test-p1b-todo"
    return h


def test_auto_advance_two_items_summary_and_single_apply(p1b_handler: Handler) -> None:
    uid = "u-p1b-1"
    p1b_handler._todo_queues[uid] = {"tasks": ["A 任务", "B 任务"], "idx": 0}

    async def _run() -> dict:
        return await p1b_handler.eval_run_routing_pipeline("搞定了", from_user=uid, context_token="ctx")

    r = asyncio.run(_run())
    decs = r.get("decisions_applied") or []
    assert len(decs) == 1
    assert decs[0].get("tool") == "todo.done_current"
    wx = r.get("wx_sent") or []
    assert len(wx) >= 1
    blob = "\n".join(str(x) for x in wx)
    assert "下一个" in blob or "B 任务" in blob
    assert "A 任务" in blob or "✅" in blob
    st = p1b_handler._todo_queues[uid]
    assert st["idx"] == 1
    assert p1b_handler._get_current_queue_task(uid) == "B 任务"


def test_auto_advance_tail_queue_all_done_text(p1b_handler: Handler) -> None:
    uid = "u-p1b-tail"
    p1b_handler._todo_queues[uid] = {"tasks": ["唯一"], "idx": 0}

    async def _run() -> dict:
        return await p1b_handler.eval_run_routing_pipeline("做完了", from_user=uid, context_token="ctx")

    r = asyncio.run(_run())
    wx = r.get("wx_sent") or []
    blob = "\n".join(str(x) for x in wx)
    assert "全部完成" in blob
    assert "唯一" in blob
