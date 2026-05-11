"""消息主入口与 Handler 组合"""

import asyncio
from datetime import datetime
import json
import re
import time
import uuid
from pathlib import Path

from acp.opencode_client import OpenCodeACP, build_system_prompt
from utils.context_injector import InjectedContextStore
from utils.flow_log import log_flow_event, snapshot_todo_queue
from utils.timeline_state import mark_daily_opening_chat
from utils.timeline_sync import timeline_enabled, timeline_path
from utils.route_fast import build_fast_unified_decision
from wechat.client import ClawBotClient

from .coaches import RecordCoachMixin, RemindCoachMixin, TimelineAppendMixin, TodoCoachMixin
from .dispatcher import DispatcherMixin
from .image import ImageMixin
from .local_view import LocalViewMixin
from .timeline_hooks import TimelineHooksMixin


class Handler(
    TimelineHooksMixin,
    LocalViewMixin,
    ImageMixin,
    DispatcherMixin,
    TodoCoachMixin,
    RecordCoachMixin,
    RemindCoachMixin,
    TimelineAppendMixin,
):
    """ClawBot 业务总入口（Mixin 组合）。

    Mixin 职责：
      - TimelineHooksMixin 时间轴睡觉、checkin 回填前置（起床=每日首条微信，见 handle）
      - LocalViewMixin    本地读今日 md，输出查看类回复
      - ImageMixin        图片消息：多模态描述 → 走 record.add
      - DispatcherMixin   工具执行注册表（_apply_unified_decision；主对话由 LangGraph 调度）
      - TodoCoachMixin    待办 8 个 coach + 内存队列
      - RecordCoachMixin  生活记录写入
      - RemindCoachMixin  提醒写入（写完由 hooks 自动唤醒调度器）
      - TimelineAppendMixin  timeline.append 显式追加时间轴

    共享状态（按读写者标注）：
      - _reminded_ids:        set[str]                    scheduler/reminders 读写（写后行去重）
      - _reminder_refresh:    asyncio.Event               RemindCoach 写 / scheduler 读
      - _todo_queues:         {user_id: {tasks, idx}}     TodoCoachMixin 读写
      - _pending_reorders:    {user_id: [tasks]}          TodoCoachMixin 读写
      - _local_view_last:     {(user, kind): (ts, msg)}   LocalViewMixin 读写
      - _local_view_debounce_sec: float                   LocalViewMixin 只读
    """

    def __init__(self, acp: OpenCodeACP, config: dict, wechat: ClawBotClient):
        self.acp = acp
        self.cfg = config
        self.wx = wechat
        self.session_id: str = ""
        self.unified_session_id: str = ""
        self.todo_session_id: str = ""
        self.record_session_id: str = ""
        self.remind_session_id: str = ""
        self.agent_session_id: str = ""
        self.debug_session_id: str = ""  # 调试模型专用 session
        self._reminded_ids: set[str] = set()
        self._reminder_refresh: asyncio.Event = asyncio.Event()
        self._todo_queues: dict[str, dict] = {}
        self._pending_reorders: dict[str, list[str]] = {}
        self._local_view_last: dict[tuple[str, str], tuple[float, str]] = {}
        self._local_view_debounce_sec: float = 2.0
        # 每用户最近若干条结构化决策（供 unified / 意图分类续写对齐）
        self._user_route_memory: dict[str, list[dict]] = {}
        # 工具状态提示防抖（避免 200ms 内反复切换）
        self._tool_status_last: dict[tuple[str, str], tuple[float, str]] = {}
        # 每用户结构化工作记忆：回答"现在在做什么"（跨消息持久）
        self._user_structured_state: dict[str, dict] = {}
        # fast 路径 todo.done_current：抑制 coach 逐条回复，编排层发「处理中」+ 汇总
        self._auto_advance_active: bool = False
        self._auto_advance_results: list[str] = []
        self._auto_advance_used_steps: int = 0
        # agent permission 挂起：每用户仅维护一个待确认请求（v1）
        self._pending_agent_permission: dict[str, int] = {}
        # 共享对话历史窗口：按 from_user 分桶，注入 unified prompt 让 reply 自带跨轮上下文
        self._conversation_window: dict[str, list[dict]] = {}
        self._conversation_window_max: int = 10
        # 单用户后台事件注入（统一聊天出口）
        self._bg_events = InjectedContextStore()
        self._chat_lock: asyncio.Lock = asyncio.Lock()
        self._signal_trigger_lock: asyncio.Lock = asyncio.Lock()
        self._last_user_msg_ts: float = 0.0
        self._chat_graph_dispatcher = None
        if hasattr(self.acp, "set_permission_request_handler"):
            self.acp.set_permission_request_handler(self._on_acp_permission_request)

    def _wx_agent_allowlist(self) -> set[str]:
        bot_cfg = self.cfg.get("bot") or {}
        raw = bot_cfg.get("wx_agent_allowlist")
        if isinstance(raw, str):
            return {x.strip() for x in raw.split(",") if x.strip()}
        if isinstance(raw, list):
            return {str(x).strip() for x in raw if str(x).strip()}
        return set()

    def _is_wx_agent_user(self, from_user: str) -> bool:
        allow = self._wx_agent_allowlist()
        if not allow:
            return False
        return "*" in allow or from_user in allow

    def _unified_chat_mode_enabled(self) -> bool:
        bot_cfg = self.cfg.get("bot") or {}
        return bool(bot_cfg.get("unified_chat_mode", False))

    @staticmethod
    def _normalize_bg_priority(priority: str) -> str:
        p = str(priority or "deferred").strip().lower()
        if p not in ("immediate", "deferred", "silent"):
            return "deferred"
        return p

    def add_background_event(
        self,
        *,
        kind: str,
        summary: str,
        priority: str = "deferred",
        ttl_sec: float | None = None,
        source: str = "",
    ) -> str:
        """调度器/后台流程统一入口：仅在 unified_chat_mode 打开时记录。"""
        if not self._unified_chat_mode_enabled():
            return ""
        eid = self._bg_events.add_event(
            kind=kind,
            summary=summary,
            priority=self._normalize_bg_priority(priority),
            ttl_sec=ttl_sec,
            source=source,
        )
        log_flow_event(
            stage="route",
            route="event_queued",
            user_text="",
            from_user="",
            session_id=self.unified_session_id,
            extra={
                "event_id": eid,
                "kind": kind,
                "priority": priority,
                "summary": str(summary or "")[:120],
            },
        )
        return eid

    def _peek_background_events_summary_for_compose(self, *, max_items: int = 3) -> str:
        if not self._unified_chat_mode_enabled():
            return ""
        self._bg_events.prune_expired()
        summary, _ids = self._bg_events.peek_compose_summary(max_items=max_items)
        return summary

    def _has_pending_compose_events(self) -> bool:
        """是否存在待在聊天表达层自然提及的后台事件。"""
        if not self._unified_chat_mode_enabled():
            return False
        self._bg_events.prune_expired()
        return bool(
            self._bg_events.peek_events(priority="deferred")
            or self._bg_events.peek_events(priority="silent")
        )

    def _resolve_recent_target(self) -> tuple[str, str]:
        tok = getattr(self.wx, "_context_tokens", None) or {}
        if isinstance(tok, dict) and tok:
            last_user, last_token = list(tok.items())[-1]
            return str(last_user or "").strip(), str(last_token or "")
        return str(getattr(self.wx, "user_id", "") or "").strip(), ""

    def _user_recently_active(self, sec: float = 5.0) -> bool:
        if self._last_user_msg_ts <= 0:
            return False
        return (time.monotonic() - self._last_user_msg_ts) < float(sec)

    async def _background_event_pump(self) -> None:
        """后台事件泵：检测 immediate 事件并尝试主动触达。"""
        while True:
            try:
                await asyncio.sleep(2)
                if not self._unified_chat_mode_enabled():
                    continue
                self._bg_events.prune_expired()
                if not self._bg_events.has_immediate():
                    continue
                if self._user_recently_active(5.0):
                    continue
                await self.signal_trigger()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                log_flow_event(
                    stage="route",
                    route="event_pump_error",
                    user_text="",
                    from_user="",
                    session_id=self.unified_session_id,
                    extra={"error": str(e)[:200]},
                )

    async def _signal_trigger_immediate(self) -> bool:
        """主动触达 immediate 事件；发送成功即 consumed。"""
        events = self._bg_events.peek_events(priority="immediate")
        if not events:
            return False
        user_id, context_token = self._resolve_recent_target()
        if not user_id:
            return False
        top = events[0]
        prompt = (
            "你是微信个人助手。请发一条自然中文短消息给用户（1~2句）。\n"
            "这不是用户刚发来的消息，而是系统提醒事件。\n"
            f"事件：{top.summary}\n"
            "要求：自然、不生硬，不要罗列多个事项。"
        )
        reply, _ = await self.acp.prompt(
            self.unified_session_id,
            prompt,
            trace_tag="background_signal",
        )
        body = (reply or "").strip() or top.summary
        await self.wx.send_text(body, user_id, context_token)
        self._append_conversation_assistant(user_id, body)
        self._bg_events.mark_consumed([top.id])
        log_flow_event(
            stage="route",
            route="event_sent",
            user_text="",
            from_user=user_id,
            session_id=self.unified_session_id,
            extra={"event_id": top.id, "kind": top.kind, "priority": top.priority},
        )
        return True

    async def signal_trigger(self) -> bool:
        """统一主动触达入口：不抢占 handle()。"""
        if not self._unified_chat_mode_enabled():
            return False
        if self._chat_lock.locked():
            return False
        if self._user_recently_active(5.0):
            return False
        async with self._signal_trigger_lock:
            if self._chat_lock.locked():
                return False
            async with self._chat_lock:
                return await self._signal_trigger_immediate()

    async def _on_acp_permission_request(self, req: dict) -> str:
        """ACP 权限请求回调：agent session 一律挂起待微信确认，其它会话自动批准。"""
        session_id = str(req.get("session_id") or "")
        if not session_id or session_id != self.agent_session_id:
            return "approved"
        ctx = req.get("context") if isinstance(req.get("context"), dict) else {}
        from_user = str(ctx.get("from_user") or "").strip()
        if not from_user:
            return "denied"
        request_id = int(req.get("request_id") or 0)
        if request_id <= 0:
            return "denied"
        self._pending_agent_permission[from_user] = request_id
        summary = str(req.get("params_summary") or "").strip()[:500]
        await self.wx.send_text(
            "检测到高权限操作申请，是否继续？\n"
            f"request_id={request_id}\n"
            f"摘要：{summary}\n\n"
            "回复「确认」继续，回复「取消」拒绝。",
            from_user,
            str(ctx.get("context_token") or ""),
        )
        log_flow_event(
            stage="route",
            route="agent_permission_pending",
            user_text="",
            from_user=from_user,
            session_id=session_id,
            extra={"request_id": request_id, "summary": summary},
        )
        return "defer"

    @staticmethod
    def _is_confirm_reply(text: str) -> bool:
        t = (text or "").strip().lower()
        return t in {"确认", "同意", "ok", "yes", "y", "继续"}

    @staticmethod
    def _is_cancel_reply(text: str) -> bool:
        t = (text or "").strip().lower()
        return t in {"取消", "拒绝", "no", "n", "停", "停止"}

    async def _maybe_handle_agent_permission_reply(
        self, text: str, from_user: str, context_token: str
    ) -> bool:
        request_id = self._pending_agent_permission.get(from_user)
        if not request_id:
            return False
        if not hasattr(self.acp, "has_pending_permission_request") or not hasattr(
            self.acp, "resolve_permission_request"
        ):
            self._pending_agent_permission.pop(from_user, None)
            return False
        if not self.acp.has_pending_permission_request(request_id):
            self._pending_agent_permission.pop(from_user, None)
            return False
        if self._is_confirm_reply(text):
            ok = self.acp.resolve_permission_request(request_id, approved=True)
            self._pending_agent_permission.pop(from_user, None)
            await self.wx.send_text(
                "已确认，继续执行。",
                from_user,
                context_token,
            )
            log_flow_event(
                stage="route",
                route="agent_permission_resolved",
                user_text=text,
                from_user=from_user,
                session_id=self.agent_session_id,
                extra={"request_id": request_id, "approved": bool(ok)},
            )
            return True
        if self._is_cancel_reply(text):
            ok = self.acp.resolve_permission_request(request_id, approved=False)
            self._pending_agent_permission.pop(from_user, None)
            await self.wx.send_text(
                "已取消本次高权限操作。",
                from_user,
                context_token,
            )
            log_flow_event(
                stage="route",
                route="agent_permission_resolved",
                user_text=text,
                from_user=from_user,
                session_id=self.agent_session_id,
                extra={"request_id": request_id, "approved": False, "resolved": bool(ok)},
            )
            return True
        return False

    async def _handle_wx_opencode_agent(
        self, *, from_user: str, text: str, context_token: str, msg_trace: str
    ) -> bool:
        if not self.agent_session_id:
            return False
        log_flow_event(
            stage="route",
            route="wx_agent_enter",
            user_text=text,
            from_user=from_user,
            session_id=self.agent_session_id,
            extra={"trace": msg_trace},
        )
        reply, _ = await self.acp.prompt(
            self.agent_session_id,
            text,
            trace_tag="wx_agent_turn",
            session_context={"from_user": from_user, "context_token": context_token},
        )
        body = (reply or "").strip()
        if body:
            await self.wx.send_text(body, from_user, context_token)
            self._append_conversation_assistant(from_user, body)
        log_flow_event(
            stage="exit",
            route="wx_agent_turn",
            user_text=text,
            from_user=from_user,
            session_id=self.agent_session_id,
            extra={"trace": msg_trace, "has_reply": bool(body)},
        )
        return bool(body)

    def _context_spill_dir(self) -> Path:
        root = Path(self.cfg["vault"]["root"]).resolve()
        p = root / ".clawbot_tmp" / "context_spills"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _maybe_spill_payload_for_memory(
        self, *, user_id: str, tool: str, payload: dict
    ) -> dict:
        """大体积 payload 不直接塞进 memory 上下文，改为文件句柄。"""
        oc = self.cfg.get("opencode") or {}
        max_first = int(oc.get("memory_spill_chars", 2000) or 2000)
        max_follow = int(
            oc.get("memory_spill_chars_followup")
            or oc.get("memory_spill_chars_round2", 1200)
            or 1200
        )
        lst = self._user_route_memory.get(user_id, [])
        max_chars = max_follow if len(lst) >= 1 else max_first
        try:
            raw = json.dumps(payload or {}, ensure_ascii=False)
        except Exception:
            return payload
        if len(raw) <= max_chars:
            return payload
        sid = uuid.uuid4().hex[:10]
        fp = self._context_spill_dir() / f"{user_id[-6:] or 'anon'}-{tool.replace('.', '_')}-{sid}.json"
        fp.write_text(raw, encoding="utf-8")
        return {
            "_spilled": True,
            "spill_file": str(fp),
            "summary": raw[:200],
            "chars": len(raw),
        }

    async def _emit_tool_status(
        self,
        *,
        from_user: str,
        context_token: str,
        tool: str,
        phase: str,
        detail: str = "",
    ) -> None:
        """可选中间态提示：idle→executing→done（带防抖，默认关闭）。"""
        bot_cfg = self.cfg.get("bot") or {}
        if not bool(bot_cfg.get("tool_progress_messages", False)):
            return
        min_iv = float(bot_cfg.get("tool_progress_min_interval_sec", 0.8) or 0.8)
        key = (from_user, tool)
        now = time.monotonic()
        prev = self._tool_status_last.get(key)
        if prev and (now - prev[0] < min_iv) and prev[1] == phase:
            return
        self._tool_status_last[key] = (now, phase)
        text = {
            "executing": f"正在执行 {tool}...",
            "result_ready": f"{tool} 已返回结果，正在整理...",
            "done": f"{tool} 处理完成。",
            "failed": f"{tool} 处理失败。",
        }.get(phase, f"{tool}: {phase}")
        if detail:
            text = f"{text}\n{detail}"
        await self.wx.send_text(text, from_user, context_token)

    def _auto_advance_append(self, line: str) -> None:
        body = (line or "").strip()
        if not body:
            return
        self._auto_advance_results.append(body)

    def _auto_advance_begin(
        self,
        *,
        from_user: str,
        user_text: str,
        msg_trace: str,
        completed_preview: str,
    ) -> None:
        self._auto_advance_active = True
        self._auto_advance_results = []
        self._auto_advance_used_steps = 0
        log_flow_event(
            stage="route",
            route="auto_advance_begin",
            user_text=user_text,
            from_user=from_user,
            session_id=self.session_id,
            extra={
                "trace": msg_trace,
                "completed_preview": (completed_preview or "")[:120],
                "queue": snapshot_todo_queue(self._todo_queues, from_user),
            },
        )

    async def _auto_advance_finalize(
        self,
        *,
        from_user: str,
        context_token: str,
        user_text: str,
        msg_trace: str,
        completed_label: str,
        max_steps: int,
        next_task: str,
    ) -> None:
        st_after = self._get_or_init_structured_state(from_user)
        used = int(st_after.get("consecutive_auto_steps", 0) or 0)
        self._auto_advance_used_steps = used
        log_flow_event(
            stage="route",
            route="auto_advance_next_once",
            user_text=user_text,
            from_user=from_user,
            session_id=self.session_id,
            extra={
                "trace": msg_trace,
                "next_preview": (next_task or "")[:120],
                "read_only": True,
            },
        )
        done_line = completed_label or "（当前项）"
        next_block = f"- [ ] {next_task}" if next_task else "- 全部完成"
        summary = (
            f"✅ 连续处理完成（{used}/{max_steps} 步）\n\n"
            f"已完成：\n- done: {done_line}\n\n"
            f"下一个：\n{next_block}\n\n"
            f"（步数限制: {max_steps}，已用 {used} 步）"
        )
        suppressed = list(self._auto_advance_results)
        log_flow_event(
            stage="route",
            route="auto_advance_summary",
            user_text=user_text,
            from_user=from_user,
            session_id=self.session_id,
            extra={
                "trace": msg_trace,
                "completed": done_line[:120],
                "next_preview": (next_task or "")[:120],
                "used_steps": used,
                "max_steps": max_steps,
                "suppressed": suppressed,
            },
        )
        await self.wx.send_text(summary, from_user, context_token)
        self._auto_advance_results = []

    async def _todo_emit_reply(
        self, text: str, from_user: str, context_token: str
    ) -> None:
        """待办 coach 统一出口：auto_advance 时只累积文本，由 _auto_advance_finalize 汇总。"""
        body = (text or "").strip()
        if not body:
            return
        if getattr(self, "_auto_advance_active", False):
            self._auto_advance_append(body)
            return
        await self.wx.send_text(body, from_user, context_token)

    # ── 结构化工作记忆 state ──

    def _get_or_init_structured_state(self, user_id: str) -> dict:
        """取或创建用户结构化 state；仅在访问时惰性 init。"""
        if user_id not in self._user_structured_state:
            # 尝试从持久化恢复
            restored = self._restore_structured_state(user_id)
            self._user_structured_state[user_id] = restored
        return self._user_structured_state[user_id]

    def _update_structured_state(
        self,
        user_id: str,
        *,
        decision: dict,
        handled: bool,
        user_text: str,
    ) -> None:
        """工具执行后更新结构化 state（成功/失败路径均调用）。"""
        if not user_id:
            return
        state = self._get_or_init_structured_state(user_id)
        tool = (decision.get("tool") or "").strip()
        payload = decision.get("payload") if isinstance(decision.get("payload"), dict) else {}

        # 步数：仅当 tool 不是 none 且已处理时递增
        was_auto_step = tool and tool != "none" and handled
        if was_auto_step:
            state["consecutive_auto_steps"] = state.get("consecutive_auto_steps", 0) + 1
        else:
            # tool=none 或未处理 → 判断是否闲聊重置
            state["consecutive_auto_steps"] = state.get("consecutive_auto_steps", 0)
            if tool == "none" and self._is_idle_chat(user_text, decision):
                state["task"] = ""
                state["findings_so_far"] = []
                state["next_step"] = ""

        # 记录最近工具与结果
        state["last_tool"] = tool or "none"
        state["last_outcome"] = "success" if handled else "failed"
        state["updated_at"] = time.time()

        # 收集事实（P1a 首版：list[str]，仅保存可读摘要）
        collected_line = self._build_collected_data_line(
            tool=tool,
            payload=payload,
            user_text=user_text,
            handled=handled,
        )
        if collected_line:
            collected = state.setdefault("collected_data", [])
            if collected_line not in collected:
                collected.append(collected_line)
                while len(collected) > 5:
                    collected.pop(0)

        # 更新下一步提示（轻量）
        if tool.startswith("todo."):
            state["next_step"] = f"待办操作: {tool}"
        elif tool == "none":
            state["next_step"] = "等待用户下一条消息"
        else:
            state["next_step"] = f"已完成: {tool}"

        # 持久化（静默失败）
        self._persist_structured_state(user_id)

    @staticmethod
    def _build_collected_data_line(
        *,
        tool: str,
        payload: dict,
        user_text: str,
        handled: bool,
    ) -> str:
        """P1a：把本轮结果压成可直接注入 prompt 的单行文本。"""
        if not handled or not tool or tool == "none":
            return ""
        if tool == "record.add":
            text = str(payload.get("text") or user_text or "").strip()[:80]
            event_date = str(payload.get("event_date") or "").strip()
            category = str(payload.get("category") or "").strip()
            suffix = []
            if category:
                suffix.append(category)
            if event_date:
                suffix.append(event_date)
            tail = f" ({'|'.join(suffix)})" if suffix else ""
            return f"记录: {text}{tail}".strip()
        if tool == "remind.add":
            hhmm = str(payload.get("hhmm") or "").strip()
            text = str(payload.get("text") or user_text or "").strip()[:80]
            prefix = f"{hhmm} " if hhmm else ""
            return f"提醒: {prefix}{text}".strip()
        if tool == "todo.merge_new_items":
            tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
            if tasks:
                brief = "、".join(str(x)[:20] for x in tasks[:3])
                extra = "…" if len(tasks) > 3 else ""
                return f"待办新增: {brief}{extra}"
            return "待办新增"
        if tool in ("todo.done_current", "todo.not_done", "todo.next", "todo.skip_current"):
            return f"待办推进: {tool}"
        return ""

    def _structured_state_context_block(self, user_id: str) -> str:
        """生成结构化 state 上下文块（注入 prompt 顶部）。"""
        state = self._get_or_init_structured_state(user_id)
        # 检查 TTL
        ttl = self._get_agent_config("state_ttl_minutes", 30)
        if time.time() - state.get("updated_at", 0) > ttl * 60:
            state.clear()
            self._user_structured_state[user_id] = self._default_state()

        parts: list[str] = []
        if state.get("task"):
            parts.append(f"- task: {state['task']}")
        collected = state.get("collected_data", [])
        if collected:
            parts.append(f"- collected_data: {json.dumps(collected[-5:], ensure_ascii=False)}")
        findings = state.get("findings_so_far", [])
        if findings:
            parts.append(f"- findings_so_far: {json.dumps(findings[-5:], ensure_ascii=False)}")
        if state.get("next_step"):
            parts.append(f"- next_step: {state['next_step']}")
        parts.append(f"- auto_steps: {state.get('consecutive_auto_steps', 0)}")

        if not parts:
            return ""
        return "structured_state:\n" + "\n".join(parts) + "\n"

    @staticmethod
    def _default_state() -> dict:
        return {
            "task": "",
            "collected_data": [],
            "findings_so_far": [],
            "next_step": "",
            "consecutive_auto_steps": 0,
            "last_tool": "none",
            "last_outcome": "none",
            "updated_at": 0.0,
        }

    @staticmethod
    def _is_idle_chat(user_text: str, decision: dict) -> bool:
        """判断本轮 tool=none 是否属于闲聊（应重置 task），而非追问/确认。

        规则：reply 中不含原文引用标记（``、列表、提交号）→ 认定为闲聊。
        """
        reply = str(decision.get("reply", "") or "")
        # 含反引号或列表 → 有实质内容，不是纯闲聊
        if "`" in reply or "\n-" in reply:
            return False
        # 含 hex hash → 有技术引用
        if re.search(r"\b[0-9a-f]{7,40}\b", reply, re.I):
            return False
        # 追问标记词
        t = (user_text or "").strip()
        followup_words = ("刚才", "上次", "前面", "继续", "那个", "这条", "那", "它", "这")
        if len(t) < 40 and any(w in t for w in followup_words):
            return False
        return True

    async def _maybe_checkpoint(self, user_id: str) -> None:
        """每 N 步触发轻量 checkpoint：记录"当前到哪了/缺什么/下一步"。"""
        every = int(self._get_agent_config("checkpoint_every", 5) or 5)
        state = self._get_or_init_structured_state(user_id)
        steps = state.get("consecutive_auto_steps", 0)
        if steps <= 0 or steps % every != 0:
            return
        # 用固定模板生成自检摘要（不调 LLM，降低成本）
        collected = state.get("collected_data", [])
        summary = (
            f"step={steps} | task={state.get('task', '无')[:60]} | "
            f"collected={len(collected)}条 | next={state.get('next_step', '待定')[:40]}"
        )
        findings = state.setdefault("findings_so_far", [])
        findings.append(summary)
        if len(findings) > 10:
            findings.pop(0)
        state["findings_so_far"] = findings
        self._persist_structured_state(user_id)
        log_flow_event(
            stage="route",
            route="agent_checkpoint",
            user_text="",
            from_user=user_id,
            session_id=self.session_id,
            extra={"step": steps, "summary": summary},
        )

    def _get_agent_config(self, key: str, default=None):
        return (self.cfg.get("agent") or {}).get(key, default)

    # ── State 持久化 ──

    def _structured_state_dir(self) -> Path:
        root = Path(self.cfg["vault"]["root"]).resolve()
        p = root / ".clawbot_tmp" / "structured_state"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _restore_structured_state(self, user_id: str) -> dict:
        """从 JSON 文件恢复 state；失败或无文件时返回默认空 state。"""
        fp = self._structured_state_dir() / f"{user_id[-8:] or 'anon'}.json"
        if not fp.exists():
            return self._default_state()
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return self._default_state()
        base = self._default_state()
        # 只恢复最小必要字段，其他按默认
        for k in ("task", "findings_so_far", "collected_data", "consecutive_auto_steps"):
            if k in raw:
                base[k] = raw[k]
        return base

    def _persist_structured_state(self, user_id: str) -> None:
        """把当前 state 最小必要字段写入 JSON（静默失败）。"""
        state = self._user_structured_state.get(user_id)
        if not state:
            return
        fp = self._structured_state_dir() / f"{user_id[-8:] or 'anon'}.json"
        slim = {
            "task": state.get("task", ""),
            "findings_so_far": state.get("findings_so_far", []),
            "collected_data": state.get("collected_data", []),
            "consecutive_auto_steps": state.get("consecutive_auto_steps", 0),
        }
        try:
            fp.write_text(json.dumps(slim, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _conversation_window_dir(self) -> Path:
        root = Path(self.cfg["vault"]["root"]).resolve()
        p = root / ".clawbot_tmp" / "conversation_window"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _persist_conversation_window(self, user_id: str) -> None:
        bucket = self._conversation_window.get(user_id)
        if not bucket:
            return
        fp = self._conversation_window_dir() / f"{user_id[-8:] or 'anon'}.json"
        try:
            fp.write_text(
                json.dumps(bucket[-10:], ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _restore_conversation_window(self, user_id: str) -> None:
        fp = self._conversation_window_dir() / f"{user_id[-8:] or 'anon'}.json"
        if not fp.exists():
            return
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._conversation_window[user_id] = data[-10:]
        except Exception:
            pass

    def _append_conversation_user(self, user_id: str, text: str) -> None:
        """用户消息进入共享对话窗口（按 from_user 分桶）。"""
        t = (text or "").strip()
        if not user_id or not t or t.startswith("/") or len(t) < 2:
            return
        bucket = self._conversation_window.setdefault(user_id, [])
        bucket.append({"role": "user", "text": t[:300], "ts": time.time()})
        while len(bucket) > self._conversation_window_max:
            bucket.pop(0)
        self._persist_conversation_window(user_id)

    def _append_conversation_assistant(self, user_id: str, text: str) -> None:
        """助手 reply 进入共享对话窗口（仅 LLM unified 决策路径调用）。"""
        t = (text or "").strip()
        if not user_id or not t:
            return
        bucket = self._conversation_window.setdefault(user_id, [])
        bucket.append({"role": "assistant", "text": t[:300], "ts": time.time()})
        while len(bucket) > self._conversation_window_max:
            bucket.pop(0)
        self._persist_conversation_window(user_id)

    def _build_shared_history(self, user_id: str | None = None) -> str:
        """供 unified prompt 注入的对话历史块（最近 5 轮）。无 user_id 返回空串。"""
        if not user_id:
            return ""
        bucket = self._conversation_window.get(user_id) or []
        if not bucket:
            return ""
        lines = ["共享对话历史（最近 5 轮）:"]
        for m in bucket[-5:]:
            role_label = "用户" if m.get("role") == "user" else "助手"
            lines.append(f"- {role_label}: {str(m.get('text', ''))[:200]}")
        return "\n".join(lines) + "\n"

    def _remember_unified_decision(self, from_user: str, decision: dict) -> None:
        """成功执行 tool 后写入短期记忆（仅关键字段，控制体积）。"""
        if not from_user:
            return
        tool = (decision.get("tool") or "").strip()
        keys_by_tool = {
            "record.add": ("text", "category", "event_date"),
            "remind.add": ("text", "hhmm", "event_date"),
            "todo.merge_new_items": ("tasks",),
        }
        keys = keys_by_tool.get(tool)
        if not keys:
            return
        payload = decision.get("payload")
        if not isinstance(payload, dict):
            return
        slim = {k: payload.get(k) for k in keys if k in payload}
        slim = self._maybe_spill_payload_for_memory(
            user_id=from_user, tool=tool, payload=slim
        )
        entry = {"tool": tool, "payload": slim}
        lst = self._user_route_memory.setdefault(from_user, [])
        lst.append(entry)
        while len(lst) > 5:
            lst.pop(0)

    def _extra_unified_context(self, user_id: str) -> str:
        """拼到 unified prompt 的上下文块（JSON 行，便于模型解析）。"""
        items = self._user_route_memory.get(user_id, [])
        if not items:
            return ""
        tail = items[-5:]
        return (
            "- recent_decisions_json: "
            + json.dumps(tail, ensure_ascii=False)
            + "\n（续写/代指时请继承其中的 event_date 与主题，除非用户本句明确给出新日期。）\n"
        )

    def _intent_classifier_memory_block(self, user_id: str) -> str:
        """意图小模型用的短记忆（只取最近生活记录，避免 prompt 过长）。"""
        items = [
            x
            for x in self._user_route_memory.get(user_id, [])
            if x.get("tool") == "record.add"
        ][-2:]
        if not items:
            return ""
        lines = ["近期生活记录（结构化摘要，供续写句对齐日期）:"]
        for it in items:
            p = it.get("payload") or {}
            ed = p.get("event_date") or ""
            tx = (p.get("text") or "")[:80]
            lines.append(f"- event_date={ed or '(未显式)'} text={tx!r}")
        return "\n".join(lines)

    @staticmethod
    def _block_intent_short_circuit(
        intent_obj: dict, text: str, oc_cfg: dict
    ) -> bool:
        """True = 阻止意图高分短路，交给 unified（续写/缺日期等）。"""
        intent = (intent_obj.get("intent") or "").strip().lower()
        allow_record = bool(oc_cfg.get("record_high_conf_short_circuit", False))
        if intent == "record_add" and not allow_record:
            return True
        if intent != "record_add":
            return False
        slots = intent_obj.get("slots") if isinstance(intent_obj.get("slots"), dict) else {}
        if (slots.get("event_date") or "").strip():
            return False
        t = text.strip()
        if len(t) <= 16:
            return True
        hints = ("她", "他", "它", "这", "那", "也", "同样", "跟", "还", "又", "刚才", "之前")
        if len(t) < 40 and any(h in t for h in hints):
            return True
        return False

    async def init_session(self):
        """初始化 ACP sessions（按域分流，减少跨域上下文污染）。"""
        self.unified_session_id = await self.acp.create_session()
        self.todo_session_id = await self.acp.create_session()
        self.record_session_id = await self.acp.create_session()
        self.remind_session_id = await self.acp.create_session()
        if self._wx_agent_allowlist():
            bot_cfg = self.cfg.get("bot") or {}
            agent_model = (bot_cfg.get("agent_model") or "").strip()
            self.agent_session_id = await self.acp.create_session(agent_model or None)
        else:
            self.agent_session_id = ""

        # 调试模式：额外创建一个 debug session，使用 debug model
        agent_cfg = self.cfg.get("agent") or {}
        debug_model = (agent_cfg.get("debug_model") or "").strip()
        if debug_model and bool(agent_cfg.get("debug_enabled", False)):
            self.debug_session_id = await self.acp.create_session(model=debug_model)
        else:
            self.debug_session_id = ""

        # 域会话启动时一次性注入角色约束，后续写盘只传 JSON envelope。
        v = self.cfg["vault"]
        sys_prompt = build_system_prompt(v["root"], v["daily_log_dir"])
        prime_tasks = [
            self.acp.prompt(
                self.todo_session_id,
                (
                    f"{sys_prompt}\n\n"
                    "这是待办域上下文。后续输入主要是 JSON 工具调用，请严格遵循对应 SKILL 执行。"
                ),
                trace_tag="prime_todo",
            ),
            self.acp.prompt(
                self.record_session_id,
                (
                    f"{sys_prompt}\n\n"
                    "这是记录域上下文。后续输入主要是 JSON 工具调用，请严格遵循对应 SKILL 执行。"
                ),
                trace_tag="prime_record",
            ),
            self.acp.prompt(
                self.remind_session_id,
                (
                    f"{sys_prompt}\n\n"
                    "这是提醒域上下文。后续输入主要是 JSON 工具调用，请严格遵循对应 SKILL 执行。"
                ),
                trace_tag="prime_remind",
            ),
        ]
        if self.debug_session_id:
            prime_tasks.append(
                self.acp.prompt(
                    self.debug_session_id,
                    (
                        f"{sys_prompt}\n\n"
                        "这是调试域上下文。使用更便宜的模型，用于 prompt 迭代与流程验证。"
                    ),
                    trace_tag="prime_debug",
                )
            )
        prime_tasks.append(
            self.acp.prompt(
                self.unified_session_id,
                (
                    f"{sys_prompt}\n\n"
                    "这是统一决策域上下文。负责分析用户意图、选择工具、回复自然中文。"
                    "待办格式与催办以 todo-coach 为准，任务分解以 task-decompose 为准，"
                    "日报总结以 daily-summary 为准。"
                ),
                trace_tag="prime_unified",
            )
        )
        await asyncio.gather(*prime_tasks)

        # 兼容旧字段：默认代表 unified 会话
        self.session_id = self.unified_session_id
        if hasattr(self.acp, "set_session_permission_mode"):
            self.acp.set_session_permission_mode(self.unified_session_id, "auto")
            self.acp.set_session_permission_mode(self.todo_session_id, "auto")
            self.acp.set_session_permission_mode(self.record_session_id, "auto")
            self.acp.set_session_permission_mode(self.remind_session_id, "auto")
        parts = [
            f"unified={self.unified_session_id} todo={self.todo_session_id}",
            f"record={self.record_session_id} remind={self.remind_session_id}",
        ]
        if self.agent_session_id:
            if hasattr(self.acp, "set_session_permission_mode"):
                self.acp.set_session_permission_mode(self.agent_session_id, "confirm")
            parts.append(f"agent={self.agent_session_id}")
        if self.debug_session_id:
            if hasattr(self.acp, "set_session_permission_mode"):
                self.acp.set_session_permission_mode(self.debug_session_id, "auto")
            parts.append(f"debug={self.debug_session_id}")
        print(f"[Bot] sessions: {' '.join(parts)}")
        self._init_chat_graph()

    def _ensure_chat_graph(self) -> None:
        if self._chat_graph_dispatcher is None:
            self._init_chat_graph()

    def _init_chat_graph(self) -> None:
        try:
            from langgraph_v2.adapters import (
                ACPImageDescriber,
                RuleIntentClassifier,
                UnifiedDecideLLM,
            )
            from langgraph_v2.contracts import ACPSessionPool
            from langgraph_v2.dispatcher import ChatGraphDispatcher
            from langgraph_v2.graph import GraphDeps, build_chat_graph

            llm = UnifiedDecideLLM(
                acp=self.acp,
                session_id=self.unified_session_id,
                retry_count=int(
                    (self.cfg.get("opencode") or {}).get("structured_retry_count", 3) or 3
                ),
                background_summary_provider=self._peek_background_events_summary_for_compose,
                unified_chat_mode_provider=self._unified_chat_mode_enabled,
            )
            classifier = RuleIntentClassifier()
            sessions = ACPSessionPool(
                unified=self.unified_session_id,
                todo=self.todo_session_id,
                record=self.record_session_id,
                remind=self.remind_session_id,
                agent=self.agent_session_id,
                debug=self.debug_session_id,
            )
            mm_model = str(
                (self.cfg.get("opencode") or {}).get(
                    "multimodal_model", "opencode-go/mimo-v2-omni"
                )
                or ""
            )
            image_llm = ACPImageDescriber(self.acp, mm_model)
            deps = GraphDeps(
                llm=llm,
                image_llm=image_llm,
                classifier=classifier,
                vault=self,
                todo=self,
                sessions=sessions,
                acl=None,
            )
            graph = build_chat_graph(deps)
            self._chat_graph_dispatcher = ChatGraphDispatcher(graph=graph)
            log_flow_event(stage="graph", route="chat_graph_ready", user_text="")
        except Exception as e:
            self._chat_graph_dispatcher = None
            log_flow_event(
                stage="graph",
                route="chat_graph_init_failed",
                user_text="",
                extra={"error": str(e)[:200]},
            )

    def _remaining_queue_tasks(self, user_id: str) -> list[str]:
        st = self._todo_queues.get(user_id)
        if not st:
            return []
        tasks = st.get("tasks") or []
        idx = int(st.get("idx", 0))
        out: list[str] = []
        for t in tasks[idx:]:
            s = str(t).strip()
            if s:
                out.append(s)
        return out

    async def _maybe_block_agent_step_limit(
        self, text: str, from_user: str, context_token: str
    ) -> bool:
        max_steps = int(self._get_agent_config("max_steps", 15) or 15)
        state = self._get_or_init_structured_state(from_user)
        auto_steps = state.get("consecutive_auto_steps", 0)
        if auto_steps < max_steps:
            return False
        log_flow_event(
            stage="route",
            route="agent_step_limit_reached",
            user_text=text,
            from_user=from_user,
            session_id=self.session_id,
            extra={
                "consecutive_auto_steps": auto_steps,
                "max_steps": max_steps,
                "task": state.get("task", "")[:120],
                "next_step": state.get("next_step", "")[:120],
            },
        )
        task = (state.get("task") or "无")[:80]
        findings = state.get("findings_so_far", []) or []
        findings_line = "、".join(str(f)[:60] for f in findings[-3:]) or "无"
        await self.wx.send_text(
            "🏁 已连续执行太多步，先停一下。\n\n"
            f"当前任务：{task}\n"
            f"已确认：{findings_line}\n"
            f"下一步计划：{state.get('next_step', '待定')[:80]}\n\n"
            "要继续的话，直接告诉我要做什么~",
            from_user,
            context_token,
        )
        return True

    async def _run_chat_graph_turn(
        self,
        text: str,
        from_user: str,
        context_token: str,
        *,
        image_base64: str = "",
        image_mime: str = "",
    ) -> None:
        self._ensure_chat_graph()
        disp = self._chat_graph_dispatcher
        if disp is None:
            await self.wx.send_text(
                "对话引擎初始化失败，请检查依赖（如 langgraph）与日志。",
                from_user,
                context_token,
            )
            return
        msg_trace = uuid.uuid4().hex[:12]
        log_flow_event(
            stage="route",
            route="chat_graph_turn",
            user_text=text,
            from_user=from_user,
            session_id=self.session_id,
            extra={"trace": msg_trace},
        )
        qs = self._remaining_queue_tasks(from_user)
        state = await disp.dispatch(
            text=text,
            from_user=from_user,
            context_token=context_token,
            queue_snapshot=qs,
            send_text=self.wx.send_text,
            image_base64=image_base64,
            image_mime=image_mime,
            agent_mode=False,
        )
        reply_sent = str(state.get("_dispatch_reply_sent") or "").strip()
        if reply_sent:
            self._append_conversation_assistant(from_user, reply_sent)
        decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
        handled = bool(state.get("handled", False))
        if decision:
            self._update_structured_state(
                from_user,
                decision=decision,
                handled=handled,
                user_text=text,
            )
        await self._maybe_checkpoint(from_user)
        log_flow_event(
            stage="exit",
            route="chat_graph_turn",
            user_text=text,
            from_user=from_user,
            session_id=self.session_id,
            extra={"trace": msg_trace, "handled": handled},
        )

    # ── LangGraph v2 repository adapter methods ──

    async def append_record(self, *, text: str, category: str, event_date: str) -> str:
        from utils.log_sync import append_to_markdown_section, get_log_path
        from utils.time_utils import time_str
        from datetime import datetime as _dt

        v = self.cfg["vault"]
        dt_obj = None
        if event_date:
            try:
                dt_obj = _dt.strptime(event_date, "%Y-%m-%d")
            except ValueError:
                dt_obj = None
        log_path = get_log_path(v["root"], v["daily_log_dir"], dt_obj)
        line = f"- [x] {time_str()} {text} （{category or '事务'}）"
        append_to_markdown_section(log_path, "## 📝 记录", line)
        return f"已记录 {category or '事务'}：{text}"

    async def append_reminder(self, *, text: str, hhmm: str, event_date: str) -> str:
        from utils.log_sync import append_to_markdown_section, get_log_path
        from datetime import datetime as _dt

        v = self.cfg["vault"]
        dt_obj = None
        if event_date:
            try:
                dt_obj = _dt.strptime(event_date, "%Y-%m-%d")
            except ValueError:
                dt_obj = None
        log_path = get_log_path(v["root"], v["daily_log_dir"], dt_obj)
        line = f"- [ ] {hhmm}：{text}"
        append_to_markdown_section(log_path, "## ⏰ 提醒", line)
        return f"⏰ 已设提醒：{hhmm} {text}"

    async def upsert_timeline_slot(self, *, slot: str, text: str) -> str:
        from utils.timeline_sync import ensure_timeline_file, slot_at, upsert_timeline_slot

        now = datetime.now()
        ensure_timeline_file(self.cfg, now)
        target_slot = slot or slot_at(now)
        ok = upsert_timeline_slot(self.cfg, target_slot, text, dt=now)
        if not ok:
            return "时间轴写入失败。"
        return f"已追加到时间轴 {target_slot}：{text}"

    async def read_view(self, *, kind: str) -> str:
        from utils.log_sync import get_log_path

        k = (kind or "").strip().lower()
        if k == "timeline":
            tp = timeline_path(self.cfg, datetime.now())
            if tp.exists():
                return tp.read_text(encoding="utf-8")[:2000]
            return "今日时间轴文件还不存在。"
        v = self.cfg["vault"]
        lp = get_log_path(v["root"], v["daily_log_dir"])
        if not lp.exists():
            return "今日日志文件还不存在。"
        body = lp.read_text(encoding="utf-8")
        if k in ("log", "brief", "record_recent"):
            return self._compose_local_view_body(k, body)
        if k == "record":
            anchor = "## 📝 记录"
        elif k == "remind":
            anchor = "## ⏰ 提醒"
        else:
            anchor = "## 📋 待办"
        idx = body.find(anchor)
        if idx < 0:
            return "暂无内容。"
        tail = body[idx : idx + 2000]
        return tail

    async def merge(self, *, user_id: str, tasks: list[str]) -> str:
        cleaned = [str(x).strip() for x in tasks if str(x).strip()]
        if not cleaned:
            return "待办内容为空，请重发。"
        self._todo_queues[user_id] = {"tasks": cleaned, "idx": 0}
        return f"新增待办 {len(cleaned)} 项"

    async def done_current(self, *, user_id: str) -> tuple[str, str]:
        cur = self._get_current_queue_task(user_id)
        decision = {"tool": "todo.done_current", "payload": {}, "reply": ""}
        await self._apply_unified_decision(decision, user_id, "", user_text="done")
        nxt = self._get_current_queue_task(user_id)
        return (f"✅ {cur} 完成" if cur else "当前没有进行中的待办。"), (nxt or "")

    async def next_task(self, *, user_id: str) -> str:
        return self._get_current_queue_task(user_id)

    async def not_done(self, *, user_id: str) -> str:
        cur = self._get_current_queue_task(user_id)
        if cur:
            return f"先做1分钟版本：{cur}，做好再回我“好了”。"
        return "没问题，你先发几个待办我来排。"

    async def reorder(self, *, user_id: str, order: list[str]) -> str:
        self._pending_reorders[user_id] = list(order)
        return f"我建议顺序：{' -> '.join(order)}。按这个顺序更新吗？"

    async def reorder_confirm(self, *, user_id: str) -> str:
        order = self._pending_reorders.pop(user_id, [])
        if not order:
            return "当前没有待确认的重排建议。"
        self._todo_queues[user_id] = {"tasks": list(order), "idx": 0}
        return "已按确认顺序更新。"

    async def skip_current(self, *, user_id: str) -> tuple[str, str]:
        state = self._todo_queues.get(user_id)
        if not state:
            return "当前没有可跳过的待办。", ""
        tasks = state.get("tasks", [])
        idx = int(state.get("idx", 0))
        if idx >= len(tasks):
            return "当前没有可跳过的待办。", ""
        cur = tasks.pop(idx)
        tasks.append(cur)
        nxt = tasks[idx] if idx < len(tasks) else ""
        return f"先跳过：{cur}。", nxt

    async def abandon_current(self, *, user_id: str) -> tuple[str, str]:
        state = self._todo_queues.get(user_id)
        if not state:
            return "当前没有可放弃的待办。", ""
        tasks = state.get("tasks", [])
        idx = int(state.get("idx", 0))
        if idx >= len(tasks):
            return "当前没有可放弃的待办。", ""
        cur = tasks.pop(idx)
        if not tasks:
            self._todo_queues.pop(user_id, None)
            return f"已放弃：{cur}。当前没有进行中的待办。", ""
        state["idx"] = min(idx, len(tasks) - 1)
        nxt = tasks[state["idx"]]
        return f"已放弃：{cur}。", nxt

    def _resolve_acp_session(self, *, prefer_debug: bool = False) -> str:
        """返回当前应使用的 ACP session id。

        当 agent.debug_enabled 为 true 且 prefer_debug 时返回 debug session；
        否则返回 unified session。
        """
        agent_cfg = self.cfg.get("agent") or {}
        if (
            prefer_debug
            and self.debug_session_id
            and bool(agent_cfg.get("debug_enabled", False))
        ):
            model_role = "debug"
            sid = self.debug_session_id
        else:
            model_role = "primary"
            sid = self.unified_session_id
        log_flow_event(
            stage="acp",
            route="session_resolve",
            user_text="",
            from_user="",
            session_id=sid,
            extra={"model_role": model_role, "prefer_debug": prefer_debug},
        )
        return sid

    def notify_reminder_refresh(self):
        """提醒列表发生变化时，唤醒调度器重建索引"""
        self._reminder_refresh.set()

    @staticmethod
    def _split_todo_items(text: str) -> list[str]:
        """把一句话拆成多个待办项，适配微信口语分隔"""
        cleaned = text.strip().strip("。.!！")
        cleaned = cleaned.replace("然后", "，").replace("再", "，")
        parts = re.split(r"[,，、;；\n]+", cleaned)
        items = []
        for part in parts:
            item = re.sub(r"^[-\d\.\)\(、\s]+", "", part).strip()
            item = item.strip("：: ")
            if item:
                items.append(item)
        return items

    @staticmethod
    def _extract_json_object(text: str) -> dict:
        """从模型回复中提取 JSON 对象"""
        text = (text or "").strip()
        if not text:
            return {}
        # 常见噪声：markdown 围栏、弯引号（会导致 strict JSON 与正则双双失效）
        fence = re.search(
            r"```(?:json)?\s*([\s\S]*?)\s*```",
            text,
            flags=re.I,
        )
        if fence:
            text = fence.group(1).strip()
        text = (
            text.replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2018", "'")
            .replace("\u2019", "'")
        )
        try:
            return json.loads(text)
        except Exception:
            pass

        # 容错：从每个 "{" 起尝试 raw_decode，拿到首个可解析对象。
        # 这样在回复前后夹杂说明文字或其它垃圾片段时，仍能尽量提取决策 JSON。
        # 必须含 top-level "tool"（unified 信封），否则会误把 payload 里的 {} 当成整段 JSON。
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", text):
            start = m.start()
            try:
                obj, _end = decoder.raw_decode(text[start:])
            except Exception:
                continue
            if isinstance(obj, dict) and "tool" in obj:
                return obj

        # 最后兜底：修复常见的“近似 JSON 但 reply 未转义换行导致坏掉”的 unified 回复。
        # 典型输入：{"tool":"none","payload":{},"reply":"困了...<换行>
        tool_m = re.search(
            r"""["']tool["']\s*:\s*["']([^"'\r\n]+)""",
            text,
            flags=re.I | re.S,
        )
        tool = (tool_m.group(1) or "").strip() if tool_m else ""
        if not tool:
            # 最后一层朴素提取，兼容正则没命中的脏文本
            lo = text.lower()
            key_idx = lo.find('"tool"')
            if key_idx < 0:
                key_idx = lo.find("'tool'")
            if key_idx >= 0:
                colon_idx = text.find(":", key_idx)
                if colon_idx >= 0:
                    q = ""
                    q_idx = -1
                    for cand in ('"', "'"):
                        i = text.find(cand, colon_idx + 1)
                        if i >= 0 and (q_idx < 0 or i < q_idx):
                            q_idx = i
                            q = cand
                    if q_idx >= 0:
                        end_idx = text.find(q, q_idx + 1)
                        if end_idx > q_idx:
                            tool = text[q_idx + 1:end_idx].strip()
        if not tool:
            return {}

        payload: dict = {}
        payload_m = re.search(
            r"""["']payload["']\s*:\s*(\{[\s\S]*?\})""",
            text,
            flags=re.I | re.S,
        )
        if payload_m:
            try:
                maybe_payload = json.loads(payload_m.group(1))
                if isinstance(maybe_payload, dict):
                    payload = maybe_payload
            except Exception:
                payload = {}

        reply = ""
        reply_m = re.search(
            r"""["']reply["']\s*:\s*["']([\s\S]*)""",
            text,
            flags=re.I | re.S,
        )
        if reply_m:
            reply = reply_m.group(1).strip()
            # 裁掉末尾可能残留的 JSON 结束符与引号
            reply = re.sub(r"""["']\s*\}?\s*$""", "", reply).strip()

        return {"tool": tool, "payload": payload, "reply": reply}

    def note_eval_tool_execution(
        self, *, tool: str, payload: dict, reply_preview: str = ""
    ) -> None:
        """LangGraph execute 节点写入评测 trace（不经 `_apply_unified_decision`）。"""
        if not getattr(self, "_eval_mode", False):
            return
        tr = getattr(self, "_eval_pipeline_trace", None)
        if not isinstance(tr, list):
            return
        snap = dict(payload) if isinstance(payload, dict) else {}
        raw = json.dumps(snap, ensure_ascii=False)
        if len(raw) > 800:
            snap = {"_truncated": True, "keys": list(snap.keys())}
        tr.append(
            {
                "tool": tool,
                "payload": snap,
                "reply_preview": (reply_preview or "")[:240],
            }
        )

    async def eval_run_routing_pipeline(
        self,
        text: str,
        *,
        from_user: str = "eval-001",
        context_token: str = "eval-ctx",
    ) -> dict:
        """离线评测：走 LangGraph 主链路（与线上一致），不经 `handle` 的 typing/ slash 分支。"""
        self._eval_mode = True
        self._eval_pipeline_trace = []
        self._eval_extras: list[dict] = []
        try:
            self._ensure_chat_graph()
            if from_user not in self._conversation_window:
                self._restore_conversation_window(from_user)
            self._append_conversation_user(from_user, text)
            if await self._maybe_block_agent_step_limit(text, from_user, context_token):
                return {
                    "user_text": text,
                    "from_user": from_user,
                    "decisions_applied": [],
                    "eval_extras": [],
                    "wx_sent": list(getattr(self.wx, "sent", []) or []),
                    "structured_state_snapshot": {},
                }
            msg_trace = uuid.uuid4().hex[:12]
            ran_agent = False
            if (
                build_fast_unified_decision(text, from_user, self._todo_queues) is None
                and self._is_wx_agent_user(from_user)
                and not self._has_pending_compose_events()
            ):
                ran_agent = await self._handle_wx_opencode_agent(
                    from_user=from_user,
                    text=text,
                    context_token=context_token,
                    msg_trace=msg_trace,
                )
            if not ran_agent:
                await self._run_chat_graph_turn(text, from_user, context_token)
        finally:
            self._eval_mode = False
        sent = getattr(self.wx, "sent", [])
        if not isinstance(sent, list):
            sent = []
        extras = getattr(self, "_eval_extras", []) or []
        state_snapshot = {}
        try:
            st = self._get_or_init_structured_state(from_user)
            state_snapshot = {
                "collected_data": list((st.get("collected_data") or [])[-5:]),
                "consecutive_auto_steps": st.get("consecutive_auto_steps", 0),
            }
        except Exception:
            state_snapshot = {}
        return {
            "user_text": text,
            "from_user": from_user,
            "decisions_applied": list(self._eval_pipeline_trace),
            "eval_extras": list(extras),
            "wx_sent": list(sent),
            "structured_state_snapshot": state_snapshot,
        }

    async def handle(self, msg: dict):
        text = msg.get("text", "").strip()
        msg_type = msg.get("type", "text")
        from_user = msg.get("from", "")
        context_token = msg.get("context_token", "")

        if msg_type == "image":
            await self._handle_image(msg)
            return

        if not text:
            return

        self._last_user_msg_ts = time.monotonic()

        if await self._maybe_handle_agent_permission_reply(text, from_user, context_token):
            return

        tl = self.cfg.get("timeline") or {}
        if timeline_enabled(self.cfg) and bool(tl.get("checkin_enabled")):
            r = mark_daily_opening_chat(self.cfg, from_user)
            if r == "woke":
                # 重启后清理旧 expect，避免盲等前一天的 expect 消费首条消息
                from utils.timeline_state import _read_state, _write_state

                st = _read_state(self.cfg)
                if st.get("checkin_expect"):
                    st["checkin_expect"] = {}
                    _write_state(self.cfg, st)
                log_flow_event(
                    stage="checkin",
                    route="first_chat_wake",
                    user_text=text[:200],
                    from_user=from_user,
                    session_id=self.session_id,
                )

        if await self._timeline_preprocess(text, from_user, context_token):
            return

        # slash 命令：优先走本地命令路由，其余保持忽略，避免进入 LLM 误判。
        if text.startswith("/"):
            slash_body = text[1:].strip()
            if not slash_body:
                log_flow_event(
                    stage="route",
                    route="ignore_slash",
                    user_text=text,
                    from_user=from_user,
                    session_id=self.session_id,
                )
                return
            cmd, _, args_tail = slash_body.partition(" ")
            cmd = cmd.strip()
            args = args_tail.strip()
            cmd = {
                "查看模板": "模板",
                "看模板": "模板",
                "查看时间轴": "时间轴",
                "看时间轴": "时间轴",
                "查看待办": "待办",
                "看待办": "待办",
                "查看代办": "代办",
                "看代办": "代办",
                "查看提醒": "提醒",
                "看提醒": "提醒",
                "查看日志": "日志",
                "看日志": "日志",
                "查看简报": "简报",
                "看简报": "简报",
            }.get(cmd, cmd)

            kind_map = {
                "待办": "todo",
                "代办": "todo",
                "提醒": "remind",
                "记录": "record",
                "日志": "log",
                "简报": "brief",
            }
            kind = kind_map.get(cmd)
            if kind:
                await self._local_view_with_optional_llm_fallback(
                    kind, from_user, context_token, text
                )
                return

            if cmd == "找":
                query = args if args else "最近5条"
                await self._local_view_with_optional_llm_fallback(
                    "record_recent", from_user, context_token, query
                )
                return

            if cmd == "时间轴":
                tp = timeline_path(self.cfg, datetime.now())
                if tp.exists():
                    await self.wx.send_text(
                        tp.read_text(encoding="utf-8")[:2000],
                        from_user,
                        context_token,
                    )
                else:
                    await self.wx.send_text(
                        "今日时间轴文件还不存在。",
                        from_user,
                        context_token,
                    )
                return

            if cmd == "模板":
                root = Path(self.cfg["vault"]["root"])
                tp = root / "3-Resources" / "模板库" / "待办模板总表.md"
                if tp.exists():
                    await self.wx.send_text(
                        tp.read_text(encoding="utf-8")[:2000],
                        from_user,
                        context_token,
                    )
                else:
                    await self.wx.send_text(
                        "模板总表文件还不存在。",
                        from_user,
                        context_token,
                    )
                return

            if cmd == "睡觉":
                await self._maybe_timeline_wake_sleep("睡觉了", from_user, context_token)
                return

            log_flow_event(
                stage="route",
                route="ignore_slash",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
            )
            return

        print(f"[Bot] <<< {text[:50]}")
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            if self._unified_chat_mode_enabled():
                self._bg_events.prune_expired()
            if from_user not in self._conversation_window:
                self._restore_conversation_window(from_user)
            self._append_conversation_user(from_user, text)
            if await self._maybe_block_agent_step_limit(text, from_user, context_token):
                return
            msg_trace = uuid.uuid4().hex[:12]
            if self._unified_chat_mode_enabled():
                async with self._chat_lock:
                    ran_agent = False
                    if (
                        build_fast_unified_decision(text, from_user, self._todo_queues) is None
                        and self._is_wx_agent_user(from_user)
                        and not self._has_pending_compose_events()
                    ):
                        ran_agent = await self._handle_wx_opencode_agent(
                            from_user=from_user,
                            text=text,
                            context_token=context_token,
                            msg_trace=msg_trace,
                        )
                    if not ran_agent:
                        await self._run_chat_graph_turn(text, from_user, context_token)
            else:
                ran_agent = False
                if (
                    build_fast_unified_decision(text, from_user, self._todo_queues) is None
                    and self._is_wx_agent_user(from_user)
                ):
                    ran_agent = await self._handle_wx_opencode_agent(
                        from_user=from_user,
                        text=text,
                        context_token=context_token,
                        msg_trace=msg_trace,
                    )
                if not ran_agent:
                    await self._run_chat_graph_turn(text, from_user, context_token)
        except Exception as e:
            print(f"[Bot] error: {e}")
            if self.cfg["bot"].get("reply_on_error", True):
                await self.wx.send_text(f"处理出错: {str(e)[:100]}", from_user, context_token)
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
        if self._unified_chat_mode_enabled():
            self._bg_events.prune_expired()
            if self._bg_events.has_immediate():
                await self.signal_trigger()
