"""时间轴写入前的短版压缩：ACP 生成一行摘要，失败则按配置截断。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from utils.flow_log import log_flow_event

from config import PACKAGE_ROOT

if TYPE_CHECKING:
    from acp.opencode_client import OpenCodeACP


def _max_chars(cfg: dict) -> int:
    tl = cfg.get("timeline") or {}
    try:
        n = int(tl.get("compact_max_chars") or 80)
    except (TypeError, ValueError):
        return 80
    return     max(10, min(200, n))

PROMPTS_DIR = PACKAGE_ROOT / "prompts"
_TIMELINE_COMPACT_FILE = "timeline_compact.md"

_DEFAULT_TIMELINE_COMPACT = """你是时间轴一行摘要助手。把下面用户原文压成一条适合写在当日时间轴里的短句。
规则：
- 只用一行中文，不要换行、不要 markdown、不要引号包裹。
- 睡眠/体重等保留关键数字与单位；口语改为简短动词短语。
- 总长度不超过 {max_chars} 个字符（含标点）。

原文：
{raw_text}

仅输出压缩后的一行："""


def _load_timeline_compact_prompt() -> str:
    """从 timeline_compact.md 读取模板，热更新。缺失 → fallback 默认值。"""
    path = PROMPTS_DIR / _TIMELINE_COMPACT_FILE
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return _DEFAULT_TIMELINE_COMPACT


def compact_timeline_line_fallback(cfg: dict, raw: str) -> str:
    """不调 ACP：按 compact_max_chars 截断（用于 compact_enabled=false 或兜底）。"""
    s = (raw or "").strip()
    if not s:
        return ""
    lim = _max_chars(cfg)
    if len(s) <= lim:
        return s
    return s[:lim]


async def compact_timeline_line(
    acp: Any,
    cfg: dict,
    session_id: str,
    raw: str,
    *,
    trace_tag: str = "timeline_compact",
) -> str:
    """开启 compact 时走 ACP 压成口语短句；关闭则原样（仍受长度上限时可在外层控制）。

    失败或模型返回空：fallback 截断到 compact_max_chars。
    """
    s = (raw or "").strip()
    if not s:
        return ""

    tl = cfg.get("timeline") or {}
    if not bool(tl.get("compact_enabled", True)):
        return s

    lim = _max_chars(cfg)
    tmpl = _load_timeline_compact_prompt()
    prompt = tmpl.format(max_chars=lim, raw_text=s)
    try:
        out, _reason = await acp.prompt(session_id, prompt, trace_tag=trace_tag)
    except Exception as e:  # noqa: BLE001
        log_flow_event(
            stage="timeline",
            route="compact_fail",
            user_text=s[:200],
            extra={"error": str(e)[:200], "trace_tag": trace_tag},
        )
        return compact_timeline_line_fallback(cfg, s)

    out_s = (out or "").strip().replace("\n", " ").strip()
    if not out_s:
        log_flow_event(
            stage="timeline",
            route="compact_fail",
            user_text=s[:200],
            extra={"note": "empty_model_output", "trace_tag": trace_tag},
        )
        return compact_timeline_line_fallback(cfg, s)

    if len(out_s) > lim:
        out_s = out_s[:lim]
    return out_s
