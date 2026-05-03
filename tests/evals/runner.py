"""评测执行器。

两种模式：
  - Mock 模式：用 FakeACP 注入预设 LLM 回复，测 dispatcher 决策链
  - LLM 模式：连真实 ACP，测端到端决策质量（需 config.yaml 配置 agent.debug_model）

用例: pytest tests/evals/ -v
    或 python -m tests.evals.runner --mode mock
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from handlers.dispatcher import DispatcherMixin
from utils.tool_names import TOOL_DECISION_NONE

from .case_schema import EvalCase, load_all_cases
from .metrics import EvalMetrics, classify_failure, compute_metrics

CASES_DIR = Path(__file__).resolve().parent / "cases"


# ────────────────────────────── Mock ACP ──────────────────────────────


class MockACP:
    """可编程 Fake ACP：每个 case 注入预设的 LLM structured/prompt 回复。"""

    def __init__(self, structured: dict | None = None, prompt_text: str = ""):
        self.structured = structured
        self.prompt_text = prompt_text
        self.prompt_calls: list[str] = []

    async def prompt_structured(self, *args, **kwargs) -> dict | None:
        return self.structured

    async def prompt(self, sid: str, msg: str, *, trace_tag: str = "x") -> tuple[str, str]:
        self.prompt_calls.append(trace_tag)
        return self.prompt_text, ""


class EvalDispatcher(DispatcherMixin):
    """最小 Dispatcher：只暴露 _llm_unified_decide，不依赖 Handler/WeChat。"""

    def __init__(self, acp: MockACP):
        self.acp = acp
        self.cfg = {
            "opencode": {"structured_retry_count": 2},
            "bot": {"max_reply_length": 2000},
        }
        self.unified_session_id = "eval-sid"
        self._pending_reorders: dict[str, list[str]] = {}

    def _get_current_queue_task(self, _uid: str) -> str:
        return ""

    def _get_remaining_queue_tasks(self, _uid: str) -> list[str]:
        return []

    @staticmethod
    def _extract_json_object(text: str) -> dict:
        try:
            return json.loads(text)
        except Exception:
            return {}


# ────────────────────────────── Runner ──────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def eval_case_mock(case: EvalCase, mock_structured: dict | None) -> EvalCase:
    """用 Mock ACP 跑单个 case，返回带 actual 字段的 case。"""
    acp = MockACP(structured=mock_structured)
    disp = EvalDispatcher(acp)

    try:
        decision = _run(disp._llm_unified_decide("eval-user", case.input))
    except Exception as e:
        case.actual_tool = "error"
        case.actual_payload = {}
        case.actual_reply = str(e)[:200]
        case.passed = False
        case.failure_mode_hit = "reasoning_break"
        return case

    if not isinstance(decision, dict):
        case.actual_tool = "error"
        case.actual_reply = "LLM 返回空/非预期格式"
        case.passed = False
        case.failure_mode_hit = "reasoning_break"
        return case

    case.actual_tool = decision.get("tool", "")
    case.actual_payload = decision.get("payload", {})
    case.actual_reply = decision.get("reply", "")

    # ── 判定逻辑 ──
    # 1. 工具名匹配
    tool_match = case.actual_tool == case.expected_tool

    # 2. payload 关键字段存在
    payload_ok = True
    if case.expected_payload_keys:
        payload_ok = all(
            k in case.actual_payload for k in case.expected_payload_keys
        )

    # 3. category 匹配（如有要求）
    category_ok = True
    if case.expected_category:
        actual_cat = (case.actual_payload.get("category") or "").strip()
        category_ok = actual_cat == case.expected_category

    case.passed = tool_match and payload_ok and category_ok
    case.failure_mode_hit = "" if case.passed else classify_failure(case)

    return case


def run_all_mock(cases: list[EvalCase]) -> EvalMetrics:
    """Mock 模式：为每个 case 构造理想 LLM 回复，跑全量。

    这测的是 dispatcher 的 JSON 解析、字段规整、兜底路径，
    而不是 LLM 本身的决策质量（那是 LLM 模式的任务）。
    """
    for case in cases:
        # 构造对应 case 的 mock structured 回复
        payload: dict[str, Any] = {}
        if case.expected_tool == "record.add":
            payload = {
                "text": case.input,
                "category": case.expected_category or "事务",
                "event_date": "2026-05-03",
            }
        elif case.expected_tool == "remind.add":
            payload = {
                "text": case.input,
                "hhmm": "15:00",
                "event_date": "2026-05-04",
            }
        elif case.expected_tool.startswith("todo."):
            payload = {}
        else:
            payload = {}

        mock = {
            "tool": case.expected_tool,
            "payload": payload,
            "reply": f"已处理: {case.description[:30]}",
        }
        eval_case_mock(case, mock)

    return compute_metrics(cases)


# ────────────────────────────── pytest 入口 ──────────────────────────────


def test_eval_simple():
    """简单任务（5 case）—— Mock 模式验证 dispatcher 决策链。"""
    cases = load_all_cases(CASES_DIR)
    simple = [c for c in cases if c.type == "simple"]
    run_all_mock(simple)
    metrics = compute_metrics(simple)
    assert metrics.passed == metrics.total, (
        f"简单任务应全部通过 Mock 测试，实际 {metrics.passed}/{metrics.total}\n"
        + metrics.summary()
    )


def test_eval_medium():
    """中等任务（10 case）—— Mock 模式。"""
    cases = load_all_cases(CASES_DIR)
    medium = [c for c in cases if c.type == "medium"]
    run_all_mock(medium)
    metrics = compute_metrics(medium)
    assert metrics.pass_rate >= 0.7, (
        f"中等任务通过率应 >= 70%，实际 {metrics.pass_rate:.0%}\n"
        + metrics.summary()
    )


def test_eval_long():
    """长任务（5 case）—— Mock 模式。"""
    cases = load_all_cases(CASES_DIR)
    long_cases = [c for c in cases if c.type == "long"]
    run_all_mock(long_cases)
    metrics = compute_metrics(long_cases)
    assert metrics.pass_rate >= 0.5, (
        f"长任务通过率应 >= 50%，实际 {metrics.pass_rate:.0%}\n"
        + metrics.summary()
    )


def test_eval_all():
    """全量 20 case —— Mock 模式。"""
    cases = load_all_cases(CASES_DIR)
    run_all_mock(cases)
    metrics = compute_metrics(cases)
    print("\n" + metrics.summary())
    # 全量通过率阈值（Mock 模式应 100%，因 LLM 回复是预设正确的）
    assert metrics.passed == metrics.total, (
        f"Mock 模式应全量通过，实际 {metrics.passed}/{metrics.total}\n"
        + metrics.summary()
    )


# ────────────────────────────── CLI ──────────────────────────────

if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "mock"
    cases = load_all_cases(CASES_DIR)

    if mode == "mock":
        run_all_mock(cases)
        metrics = compute_metrics(cases)
        print(metrics.summary())
    else:
        print(f"未知模式: {mode}。可用: mock")
