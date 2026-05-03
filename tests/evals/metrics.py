"""评估指标计算：工具错误率 / 推理断裂 / 幻觉 / 上下文丢失 / 自愈率。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .case_schema import EvalCase


@dataclass
class EvalMetrics:
    """单次运行的全量指标。"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    # 按类型分
    simple_passed: int = 0
    simple_total: int = 0
    medium_passed: int = 0
    medium_total: int = 0
    long_passed: int = 0
    long_total: int = 0
    # 失败模式分布
    tool_selection_errors: int = 0
    reasoning_breaks: int = 0
    hallucinations: int = 0
    context_losses: int = 0
    self_heals: int = 0
    # 详情
    failures: list[dict] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.passed / self.total

    @property
    def tool_error_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.tool_selection_errors / self.total

    @property
    def reasoning_break_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.reasoning_breaks / self.total

    @property
    def hallucination_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.hallucinations / self.total

    @property
    def context_loss_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.context_losses / self.total

    @property
    def self_heal_rate(self) -> float:
        """自愈率 = 从错误决策中恢复的比例。

        Note: 需多步 case 才能计算，单步 case 不参与。
        """
        total_errors = self.tool_selection_errors + self.reasoning_breaks
        if total_errors == 0:
            return 0.0
        return self.self_heals / total_errors

    def summary(self) -> str:
        lines = [
            "=" * 50,
            "评测结果摘要",
            "=" * 50,
            f"总用例: {self.total}  |  通过: {self.passed}  |  失败: {self.failed}  |  通过率: {self.pass_rate:.1%}",
            "",
            "按类型:",
            f"  简单 ({self.simple_total}):  通过 {self.simple_passed}  ({_pct(self.simple_passed, self.simple_total)})",
            f"  中等 ({self.medium_total}):  通过 {self.medium_passed}  ({_pct(self.medium_passed, self.medium_total)})",
            f"  长任务 ({self.long_total}): 通过 {self.long_passed}  ({_pct(self.long_passed, self.long_total)})",
            "",
            "失败模式分布:",
            f"  工具选择错误:  {self.tool_selection_errors}  ({self.tool_error_rate:.1%})",
            f"  推理链断裂:    {self.reasoning_breaks}  ({self.reasoning_break_rate:.1%})",
            f"  幻觉(无证据):  {self.hallucinations}  ({self.hallucination_rate:.1%})",
            f"  上下文丢失:    {self.context_losses}  ({self.context_loss_rate:.1%})",
            f"  自愈成功:      {self.self_heals}  (自愈率 {self.self_heal_rate:.1%})",
            "",
        ]
        if self.failures:
            lines.append("失败详情:")
            for f in self.failures:
                lines.append(
                    f"  [{f['id']}] {f['type']}: {f['description'][:40]}"
                    f"  → 期望 {f['expected_tool']}, 实际 {f['actual_tool']}"
                    f"  ({f.get('failure_mode', '?')})"
                )
        return "\n".join(lines)


def _pct(passed: int, total: int) -> str:
    if total == 0:
        return "N/A"
    return f"{passed / total:.0%}"


# ── 失败模式 → 类别映射 ──

_FAILURE_LABELS = {
    "tool_selection": "工具选择错误",
    "reasoning_break": "推理链断裂",
    "hallucination": "幻觉",
    "context_loss": "上下文丢失",
    "self_heal": "自愈",
}


def classify_failure(case: EvalCase) -> str:
    """根据 case 的 actual 与 expected 推断主要失败类别。"""
    # 工具选错 → tool_selection
    if case.actual_tool != case.expected_tool:
        return "tool_selection"

    # 工具对但 payload 缺关键字段 → reasoning_break
    if case.expected_payload_keys:
        missing = [k for k in case.expected_payload_keys if k not in case.actual_payload]
        if missing:
            return "reasoning_break"

    # 工具对但 reply 包含假证据 → hallucination
    if _has_fabricated_evidence(case):
        return "hallucination"

    # 长的 context_before 但实际结果未引用 → context_loss
    if len(case.context_before) >= 2 and not _references_context(case):
        return "context_loss"

    return ""


def _has_fabricated_evidence(case: EvalCase) -> bool:
    """检测 reply 中是否编造了不存在的数据/列表。"""
    reply = (case.actual_reply or "").lower()
    fabricated_markers = [
        "共 5 条记录", "已找到 3", "当前体重", "累计跑了",
    ]
    # 只有当 case 无 context_before 且 reply 出现数据时才触发
    if not case.context_before:
        return any(m in reply for m in fabricated_markers)
    return False


def _references_context(case: EvalCase) -> bool:
    """reply 中是否引用了 context_before 中的内容。"""
    if not case.context_before:
        return True  # 无上下文依赖，不算丢失
    reply = (case.actual_reply or "").lower()
    for ctx in case.context_before:
        payload = ctx.get("payload", {})
        text = (payload.get("text") or "").lower()
        if text and text[:10] in reply:
            return True
    # 兜底：检查 reply 是否有任何引用标记
    ref_markers = ["上次", "刚才", "之前", "继续", "那个"]
    return any(m in reply for m in ref_markers)


def compute_metrics(cases: list[EvalCase]) -> EvalMetrics:
    """从已运行的 case 列表计算全量指标。"""
    m = EvalMetrics()
    m.total = len(cases)
    m.passed = sum(1 for c in cases if c.passed)
    m.failed = sum(1 for c in cases if c.passed is False)

    for c in cases:
        if c.type == "simple":
            m.simple_total += 1
            if c.passed:
                m.simple_passed += 1
        elif c.type == "medium":
            m.medium_total += 1
            if c.passed:
                m.medium_passed += 1
        elif c.type == "long":
            m.long_total += 1
            if c.passed:
                m.long_passed += 1

        fm = c.failure_mode_hit or classify_failure(c)
        if fm == "tool_selection":
            m.tool_selection_errors += 1
        elif fm == "reasoning_break":
            m.reasoning_breaks += 1
        elif fm == "hallucination":
            m.hallucinations += 1
        elif fm == "context_loss":
            m.context_losses += 1
        elif fm == "self_heal":
            m.self_heals += 1

        if c.passed is False:
            m.failures.append(c.to_report_row())

    return m
