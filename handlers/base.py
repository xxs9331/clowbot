"""消息主入口与 Handler 组合"""

import asyncio
import json
import re
import time
import uuid
from pathlib import Path

from acp.opencode_client import OpenCodeACP, build_system_prompt
from utils.intent import (
    INTENT_QUERY_REMIND,
    INTENT_QUERY_TODO,
    INTENT_REMIND,
    detect_intent,
)
from utils.flow_log import log_flow_event, snapshot_todo_queue
from utils.intent_bridge import intent_to_decision
from utils.intent_llm import classify_intent
from utils.route_fast import build_fast_unified_decision
from wechat.client import ClawBotClient

from .coaches import RecordCoachMixin, RemindCoachMixin, TodoCoachMixin
from .dispatcher import DispatcherMixin
from .image import ImageMixin
from .local_view import LocalViewMixin


class Handler(
    LocalViewMixin,
    ImageMixin,
    DispatcherMixin,
    TodoCoachMixin,
    RecordCoachMixin,
    RemindCoachMixin,
):
    """ClawBot 业务总入口（Mixin 组合）。

    Mixin 职责：
      - LocalViewMixin    本地读今日 md，输出查看类回复
      - ImageMixin        图片消息：多模态描述 → 走 record.add
      - DispatcherMixin   决策与分发（_llm_unified_decide / _apply_unified_decision）
      - TodoCoachMixin    待办 8 个 coach + 内存队列
      - RecordCoachMixin  生活记录写入
      - RemindCoachMixin  提醒写入（写完由 hooks 自动唤醒调度器）

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

        # 收集事实（仅对写操作）
        if tool in ("record.add", "remind.add"):
            fact = (payload.get("text") or user_text)[:120]
            collected = state.setdefault("collected_data", [])
            if fact not in collected:
                collected.append(fact)
                if len(collected) > 10:
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
        await asyncio.gather(*prime_tasks)

        # 兼容旧字段：默认代表 unified 会话
        self.session_id = self.unified_session_id
        parts = [
            f"unified={self.unified_session_id} todo={self.todo_session_id}",
            f"record={self.record_session_id} remind={self.remind_session_id}",
        ]
        if self.debug_session_id:
            parts.append(f"debug={self.debug_session_id}")
        print(f"[Bot] sessions: {' '.join(parts)}")

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

    async def eval_run_routing_pipeline(
        self,
        text: str,
        *,
        from_user: str = "eval-001",
        context_token: str = "eval-ctx",
    ) -> dict:
        """离线评测：走 `_run_chat_routing_pipeline`，不经过 `handle` 的 typing/本地视图前置。

        返回 `decisions_applied`（每次进入 `_apply_unified_decision` 的 tool/payload 摘要）、
        `wx_sent`（DummyWX 收集的可见回复）、`eval_extras`（未走 apply 的分支标记）。
        """
        self._eval_mode = True
        self._eval_pipeline_trace = []
        self._eval_extras: list[dict] = []
        try:
            await self._run_chat_routing_pipeline(text, from_user, context_token)
        finally:
            self._eval_mode = False
        sent = getattr(self.wx, "sent", [])
        if not isinstance(sent, list):
            sent = []
        extras = getattr(self, "_eval_extras", []) or []
        return {
            "user_text": text,
            "from_user": from_user,
            "decisions_applied": list(self._eval_pipeline_trace),
            "eval_extras": list(extras),
            "wx_sent": list(sent),
        }

    async def _run_chat_routing_pipeline(
        self, text: str, from_user: str, context_token: str
    ) -> None:
        """规则守卫 → 快通道 → 意图分类 → 统一决策 → 安全兜底。"""
        msg_trace = uuid.uuid4().hex[:12]
        log_flow_event(
            stage="route",
            route="chat_pipeline",
            user_text=text,
            from_user=from_user,
            session_id=self.session_id,
            extra={"trace": msg_trace},
        )

        # ── 步数硬上限检测 ──
        max_steps = int(self._get_agent_config("max_steps", 15) or 15)
        state = self._get_or_init_structured_state(from_user)
        auto_steps = state.get("consecutive_auto_steps", 0)
        if auto_steps >= max_steps:
            log_flow_event(
                stage="route",
                route="agent_step_limit_reached",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={
                    "trace": msg_trace,
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
            return

        intent_early, _ = detect_intent(text)
        if intent_early == INTENT_QUERY_TODO:
            log_flow_event(
                stage="route",
                route="query_todo_intent",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={"intent": intent_early, "trace": msg_trace},
            )
            if getattr(self, "_eval_mode", False):
                ex = getattr(self, "_eval_extras", None)
                if isinstance(ex, list):
                    ex.append({"branch": "local_view", "kind": "todo"})
            await self._local_view_with_optional_llm_fallback(
                "todo", from_user, context_token, text
            )
            return

        fast_decision = build_fast_unified_decision(text, from_user, self._todo_queues)
        if fast_decision:
            log_flow_event(
                stage="route",
                route="fast_unified",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={
                    "trace": msg_trace,
                    "decision": fast_decision,
                    "queue": snapshot_todo_queue(self._todo_queues, from_user),
                },
            )
            handled_fast = await self._apply_unified_decision(
                fast_decision,
                from_user,
                context_token,
                user_text=text,
            )
            self._update_structured_state(
                from_user,
                decision=fast_decision,
                handled=handled_fast,
                user_text=text,
            )
            await self._maybe_checkpoint(from_user)
            log_flow_event(
                stage="exit",
                route="fast_unified",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={"trace": msg_trace, "handled": handled_fast},
            )
            if handled_fast:
                return

        # ─── 小模型意图分类兜底（只在规则/fast 都未命中时触发）───
        oc_cfg = self.cfg.get("opencode", {}) or {}
        high_th = float(oc_cfg.get("intent_threshold_high", 0.8) or 0.8)
        mid_th = float(oc_cfg.get("intent_threshold_mid", 0.5) or 0.5)
        intent_model = oc_cfg.get("intent_model") or None
        queue_state = snapshot_todo_queue(self._todo_queues, from_user)
        mem_block = self._intent_classifier_memory_block(from_user)
        intent_obj = await classify_intent(
            self.acp,
            model=intent_model,
            text=text,
            queue_state=queue_state,
            memory_context=mem_block or None,
            from_user=from_user,
        )
        intent_hint = None
        if intent_obj:
            conf = float(intent_obj.get("confidence", 0.0))
            log_flow_event(
                stage="route",
                route="intent_llm",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={
                    "trace": msg_trace,
                    "intent": intent_obj.get("intent"),
                    "slots": intent_obj.get("slots", {}),
                    "confidence": conf,
                    "queue": queue_state,
                },
            )
            if conf >= high_th:
                decision = intent_to_decision(intent_obj)
                if decision and self._block_intent_short_circuit(intent_obj, text, oc_cfg):
                    log_flow_event(
                        stage="route",
                        route="intent_high_deferred",
                        user_text=text,
                        from_user=from_user,
                        session_id=self.session_id,
                        extra={
                            "trace": msg_trace,
                            "intent": intent_obj.get("intent"),
                            "confidence": conf,
                        },
                    )
                    decision = None
                if decision:
                    handled_sm = await self._apply_unified_decision(
                        decision,
                        from_user,
                        context_token,
                        user_text=text,
                    )
                    self._update_structured_state(
                        from_user,
                        decision=decision,
                        handled=handled_sm,
                        user_text=text,
                    )
                    await self._maybe_checkpoint(from_user)
                    log_flow_event(
                        stage="exit",
                        route="intent_llm",
                        user_text=text,
                        from_user=from_user,
                        session_id=self.session_id,
                        extra={
                            "trace": msg_trace,
                            "handled": handled_sm,
                            "intent_source": "small_model",
                            "intent_confidence": conf,
                            "intent_slots": intent_obj.get("slots", {}),
                            "decision": decision,
                        },
                    )
                    if handled_sm:
                        return
            if conf >= mid_th:
                intent_hint = intent_obj

        log_flow_event(
            stage="route",
            route="llm_unified_enter",
            user_text=text,
            from_user=from_user,
            session_id=self.session_id,
            extra={"trace": msg_trace, "queue": queue_state, "intent_hint": intent_hint},
        )
        decision = await self._llm_unified_decide(
            from_user, text, intent_hint=intent_hint
        )
        handled = await self._apply_unified_decision(
            decision,
            from_user,
            context_token,
            user_text=text,
        )
        self._update_structured_state(
            from_user,
            decision=decision,
            handled=handled,
            user_text=text,
        )
        await self._maybe_checkpoint(from_user)
        log_flow_event(
            stage="exit",
            route="llm_unified",
            user_text=text,
            from_user=from_user,
            session_id=self.session_id,
            extra={
                "trace": msg_trace,
                "handled": handled,
                "decision": decision,
                "intent_source": "unified",
                "intent_hint": intent_hint,
            },
        )
        if handled:
            return

        intent, data = detect_intent(text)
        if intent == INTENT_REMIND:
            log_flow_event(
                stage="route",
                route="intent_remind",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={"trace": msg_trace, "remind_payload": data},
            )
            hhmm, _, remind_text = data.partition("：")
            remind_text = remind_text.strip() or data
            hhmm = hhmm.strip()
            decision = {
                "tool": "remind.add",
                "payload": {"text": remind_text, "hhmm": hhmm},
                "reply": "",
            }
            await self._apply_unified_decision(
                decision, from_user, context_token, user_text=text
            )
            self._update_structured_state(
                from_user,
                decision=decision,
                handled=True,
                user_text=text,
            )
            await self._maybe_checkpoint(from_user)
            print(f"[Bot] ⏰ 提醒: {data}")
            return
        if intent == INTENT_QUERY_REMIND:
            log_flow_event(
                stage="route",
                route="query_remind_intent",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={"trace": msg_trace},
            )
            await self._local_view_with_optional_llm_fallback(
                "remind", from_user, context_token, text
            )
            return

        log_flow_event(
            stage="route",
            route="safe_fallback",
            user_text=text,
            from_user=from_user,
            session_id=self.session_id,
            extra={
                "trace": msg_trace,
                "intent": intent,
                "intent_data": data,
                "intent_source": "safe_fallback",
                "intent_hint": intent_hint,
            },
        )
        if getattr(self, "_eval_mode", False):
            ex = getattr(self, "_eval_extras", None)
            if isinstance(ex, list):
                ex.append({"branch": "safe_fallback"})
        await self.wx.send_text(
            "我没看懂这条要怎么记。要我把它当作生活记录写进今日日记吗？回「记一下」即可。",
            from_user,
            context_token,
        )
        print(f"[Bot] >>> safe_fallback: {text[:60]}")

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

        # 个人使用不发斜杠命令；以 / 开头的统一忽略，避免被 LLM 当成自然语言乱解释。
        if text.startswith("/"):
            log_flow_event(
                stage="route",
                route="ignore_slash",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
            )
            return

        local_kind = self._detect_local_view_kind(text)
        if local_kind:
            log_flow_event(
                stage="route",
                route=f"local_view:{local_kind}",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
            )
            await self._local_view_with_optional_llm_fallback(
                local_kind, from_user, context_token, text
            )
            return

        print(f"[Bot] <<< {text[:50]}")
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            await self._run_chat_routing_pipeline(text, from_user, context_token)
        except Exception as e:
            print(f"[Bot] error: {e}")
            if self.cfg["bot"].get("reply_on_error", True):
                await self.wx.send_text(f"处理出错: {str(e)[:100]}", from_user, context_token)
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
