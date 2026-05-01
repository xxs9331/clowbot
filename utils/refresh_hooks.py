"""写入后副作用钩子表 — 让"写完 X 节后必须 refresh Y 调度器"的责任只在一处声明。

dispatcher 在 _apply_unified_decision 末尾统一调用 run_post_write_hooks，
业务 coach 不再需要记得调 self.notify_reminder_refresh() 之类。

设计要点：
- 钩子按 tool 名注册（而不是 domain），允许同一域不同动作做不同副作用
- 钩子拿到 (handler, payload) 两个参数，handler 暴露 notify_* 接口
- 钩子是同步函数：副作用应当轻量（事件 set / 缓存 invalidate），重活留给被唤醒方
"""

from __future__ import annotations

from typing import Any, Callable

from utils.tool_names import TOOL_REMIND_ADD

# tool_name -> 副作用钩子
PostWriteHook = Callable[[Any, dict], None]
_POST_WRITE_HOOKS: dict[str, list[PostWriteHook]] = {}


def register_post_write_hook(tool: str, fn: PostWriteHook) -> None:
    """注册一个钩子，dispatcher 在该 tool 处理完成后会同步调用。"""
    _POST_WRITE_HOOKS.setdefault(tool, []).append(fn)


def run_post_write_hooks(handler: Any, tool: str, payload: dict | None) -> None:
    """dispatcher 末尾调用；钩子异常不抛出，只打印（避免影响主回复）。"""
    hooks = _POST_WRITE_HOOKS.get(tool)
    if not hooks:
        return
    for fn in hooks:
        try:
            fn(handler, payload or {})
        except Exception as e:  # noqa: BLE001
            print(f"[hooks] post_write hook for {tool} raised: {e}")


# ─── 默认注册：写完提醒后唤醒 reminder 调度器 ───

def _refresh_reminder_scheduler(handler: Any, _payload: dict) -> None:
    fn = getattr(handler, "notify_reminder_refresh", None)
    if callable(fn):
        fn()


register_post_write_hook(TOOL_REMIND_ADD, _refresh_reminder_scheduler)
