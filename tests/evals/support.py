"""评测辅助：假微信、用例加载、断言；可选真实 OpenCodeACP + config 合并。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from handlers import Handler
from utils.log_sync import get_log_path
from utils.tool_names import TOOL_DECISION_NONE

# tests/evals -> .clawbot 根
EVAL_ROOT = Path(__file__).resolve().parent.parent.parent


class EvalDummyWX:
    """收集 `send_text`；`set_typing` 为空操作。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str, _to: str, _token: str) -> None:
        self.sent.append(text)

    async def set_typing(self, **_kwargs) -> None:
        return


class MinimalEvalACP:
    """无真 OpenCode 时仍可走 unified 管道：固定返回「不处理、无回复」以触发后续规则/兜底。"""

    model = "eval-stub/minimal"

    async def create_session(self, model: str | None = None) -> str:  # noqa: ARG002
        return "eval-stub-session"

    async def prompt(self, session_id: str, message: str, *, trace_tag: str = "prompt") -> tuple[str, str]:  # noqa: ARG002
        return "", ""

    async def prompt_structured(
        self,
        session_id: str,
        message: str,
        *,
        json_schema: dict,
        retry_count: int = 3,
        trace_tag: str = "prompt_structured",
    ) -> dict | None:  # noqa: ARG002
        return {"tool": TOOL_DECISION_NONE, "payload": {}, "reply": ""}


def try_load_config(path: Path) -> dict | None:
    """读取 config.yaml；不存在则返回 None（不 sys.exit）。"""
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else None


def merge_config_for_eval(tmp_vault_root: Path, loaded: dict | None) -> dict:
    """合并仓库配置与评测沙箱：vault.root 固定为 tmp，避免写生产日记。"""
    base: dict[str, Any] = {
        "vault": {
            "root": str(tmp_vault_root),
            "daily_log_dir": "2-Areas/习惯养成/生活日志",
            "project_dir": "",
            "task_dir": "",
        },
        "bot": {
            "max_reply_length": 2000,
            "tool_progress_messages": False,
            "tool_progress_min_interval_sec": 0.8,
            "reply_on_record": True,
            "reply_on_error": True,
        },
        "opencode": {
            "memory_spill_chars": 2000,
            "memory_spill_chars_followup": 1200,
            "structured_retry_count": 3,
            "reply_merge_enabled": True,
        },
        "categories": ["身体", "运动", "阅读", "事务"],
    }
    if not loaded:
        return deepcopy(base)
    out = deepcopy(base)
    if isinstance(loaded.get("bot"), dict):
        out["bot"] = {**out["bot"], **loaded["bot"]}
    if isinstance(loaded.get("opencode"), dict):
        out["opencode"] = {**out["opencode"], **loaded["opencode"]}
    if isinstance(loaded.get("categories"), list) and loaded["categories"]:
        out["categories"] = list(loaded["categories"])
    if isinstance(loaded.get("agent"), dict):
        out["agent"] = deepcopy(loaded["agent"])
    lv = loaded.get("vault")
    if isinstance(lv, dict):
        out["vault"] = {
            "root": str(tmp_vault_root),
            "daily_log_dir": str(lv.get("daily_log_dir") or out["vault"]["daily_log_dir"]),
            "project_dir": str(lv.get("project_dir") or ""),
            "task_dir": str(lv.get("task_dir") or ""),
        }
    return out


def load_cases_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"cases file must be a mapping: {path}")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"cases file must contain a list 'cases': {path}")
    return data


def build_eval_handler(merged_cfg: dict, acp: Any) -> Handler:
    return Handler(acp=acp, config=merged_cfg, wechat=EvalDummyWX())  # type: ignore[arg-type]


def ensure_minimal_today_log_for_eval(handler: Handler) -> None:
    """避免 local_view 在无日记文件时走 LLM fallback（acp 不可用时）。"""
    vault = handler.cfg.get("vault") or {}
    root = vault.get("root")
    daily = vault.get("daily_log_dir")
    if not root or not daily:
        return
    p = get_log_path(str(root), str(daily))
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "## 📝 记录\n\n"
        "## ⏰ 提醒\n\n"
        "## 📋 待办\n"
        "- [ ] eval 占位待办\n",
        encoding="utf-8",
    )


def _last_tool(result: dict) -> str | None:
    dec = result.get("decisions_applied") or []
    if not dec:
        return None
    last = dec[-1]
    if isinstance(last, dict):
        return str(last.get("tool") or "").strip() or None
    return None


def _joined_wx(result: dict) -> str:
    parts = result.get("wx_sent") or []
    return "\n".join(str(x) for x in parts if x)


def _joined_state_collected(result: dict) -> str:
    snap = result.get("structured_state_snapshot") or {}
    items = snap.get("collected_data") if isinstance(snap, dict) else []
    if not isinstance(items, list):
        return ""
    return "\n".join(str(x) for x in items if x)


def _norm_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return list(val)
    if isinstance(val, str):
        return [val]
    return [str(val)]


def assert_case_expectations(case: dict[str, Any], result: dict) -> list[str]:
    """返回失败原因列表；空表示通过。"""
    failures: list[str] = []
    cid = case.get("id", "?")

    exp_tool = case.get("expect_tool")
    if exp_tool:
        got = _last_tool(result)
        if got != exp_tool:
            failures.append(f"[{cid}] expect_tool={exp_tool!r} got={got!r}")

    one_of = _norm_list(case.get("expect_tool_one_of"))
    if one_of:
        want = {str(x) for x in one_of}
        got = _last_tool(result)
        if got not in want:
            failures.append(f"[{cid}] expect_tool_one_of {sorted(want)!r} got={got!r}")

    for t in _norm_list(case.get("expect_tools_any")):
        tools = {str(d.get("tool")) for d in (result.get("decisions_applied") or []) if isinstance(d, dict)}
        if str(t) not in tools:
            failures.append(f"[{cid}] expect_tools_any missing {t!r} in {tools!r}")

    for sub in _norm_list(case.get("expect_reply_contains")):
        blob = _joined_wx(result)
        if sub not in blob:
            failures.append(f"[{cid}] expect_reply_contains substring missing: {sub!r}")

    exp_branch = case.get("expect_eval_extra_branch")
    if exp_branch:
        extras = result.get("eval_extras") or []
        kinds = {str(e.get("branch")) for e in extras if isinstance(e, dict)}
        if str(exp_branch) not in kinds:
            failures.append(f"[{cid}] expect_eval_extra_branch={exp_branch!r} got extras={extras!r}")

    if case.get("expect_wx_nonempty"):
        wx_blob = _joined_wx(result).strip()
        decs = result.get("decisions_applied") or []
        if not wx_blob and not decs:
            failures.append(f"[{cid}] expect_wx_nonempty but wx_sent and decisions are empty")

    for sub in _norm_list(case.get("expect_state_contains")):
        blob = _joined_state_collected(result)
        if sub not in blob:
            failures.append(f"[{cid}] expect_state_contains substring missing: {sub!r}; state={blob!r}")

    return failures


async def run_yaml_file(
    path: Path,
    *,
    tmp_path: Path,
    acp: Any | None = None,
    use_real_acp: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """跑完整 YAML。`use_real_acp=True` 时从 config 启动 OpenCodeACP 并 `await handler.init_session()`。"""
    data = load_cases_yaml(path)
    loaded_cfg: dict | None = None
    started: Any = None

    try:
        if use_real_acp:
            from acp.opencode_client import OpenCodeACP

            cp = config_path or (EVAL_ROOT / "config.yaml")
            loaded_cfg = try_load_config(cp)
            if not loaded_cfg:
                raise FileNotFoundError(
                    f"未找到或未读入配置文件: {cp}（请复制 config.example.yaml 为 config.yaml）"
                )
            oc = loaded_cfg.get("opencode") or {}
            started = OpenCodeACP(
                cwd=str(oc.get("cwd", str(EVAL_ROOT))),
                port=int(oc.get("port", 0) or 0),
                hostname=str(oc.get("hostname", "127.0.0.1")),
                model=str(oc.get("model", "deepseek/deepseek-v4-flash")),
                max_tokens=int(oc.get("max_tokens", 4096) or 4096),
                reply_merge_enabled=bool(oc.get("reply_merge_enabled", True)),
            )
            await started.start()
            acp = started
        elif acp is None:
            acp = MinimalEvalACP()

        merged = merge_config_for_eval(tmp_path, loaded_cfg)
        h = build_eval_handler(merged, acp)
        ensure_minimal_today_log_for_eval(h)

        if use_real_acp:
            await h.init_session()
        else:
            h.unified_session_id = "eval-unified-sid"
            h.session_id = h.unified_session_id

        per_case: list[dict[str, Any]] = []
        all_failures: list[str] = []

        for case in data.get("cases") or []:
            if not isinstance(case, dict):
                continue
            if hasattr(h.wx, "sent") and isinstance(getattr(h.wx, "sent", None), list):
                h.wx.sent.clear()  # type: ignore[union-attr]
            text = str(case.get("input") or "").strip()
            from_user = str(case.get("from_user") or "eval-001")
            ctx = str(case.get("context_token") or "eval-ctx")

            pre_setup = case.get("pre_setup")
            if isinstance(pre_setup, dict):
                if pre_setup.get("active_todo_queue"):
                    h._todo_queues[from_user] = {
                        "tasks": list(pre_setup.get("tasks") or ["项A", "项B"]),
                        "idx": int(pre_setup.get("idx", 0)),
                    }

            result = await h.eval_run_routing_pipeline(text, from_user=from_user, context_token=ctx)
            fails = assert_case_expectations(case, result)
            all_failures.extend(fails)
            per_case.append({"id": case.get("id"), "ok": not fails, "failures": fails, "result": result})

        return {
            "cases_file": str(path),
            "total": len(per_case),
            "failed": sum(1 for x in per_case if not x["ok"]),
            "failures": all_failures,
            "per_case": per_case,
            "use_real_acp": use_real_acp,
        }
    finally:
        if started is not None:
            with suppress(Exception):
                await started.stop()


def run_yaml_file_sync(
    path: Path,
    *,
    tmp_path: Path,
    acp: Any | None = None,
    use_real_acp: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        run_yaml_file(
            path,
            tmp_path=tmp_path,
            acp=acp,
            use_real_acp=use_real_acp,
            config_path=config_path,
        )
    )
