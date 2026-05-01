"""消息主入口与 Handler 组合"""

import asyncio
import json
import re

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
        self._reminded_ids: set[str] = set()
        self._reminder_refresh: asyncio.Event = asyncio.Event()
        self._todo_queues: dict[str, dict] = {}
        self._pending_reorders: dict[str, list[str]] = {}
        self._local_view_last: dict[tuple[str, str], tuple[float, str]] = {}
        self._local_view_debounce_sec: float = 2.0

    async def init_session(self):
        """初始化 ACP sessions（按域分流，减少跨域上下文污染）。"""
        self.unified_session_id = await self.acp.create_session()
        self.todo_session_id = await self.acp.create_session()
        self.record_session_id = await self.acp.create_session()
        self.remind_session_id = await self.acp.create_session()

        # 域会话启动时一次性注入角色约束，后续写盘只传 JSON envelope。
        v = self.cfg["vault"]
        sys_prompt = build_system_prompt(v["root"], v["daily_log_dir"])
        await asyncio.gather(
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
        )
        # 兼容旧字段：默认代表 unified 会话
        self.session_id = self.unified_session_id
        print(
            "[Bot] sessions:"
            f" unified={self.unified_session_id} todo={self.todo_session_id}"
            f" record={self.record_session_id} remind={self.remind_session_id}"
        )

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
        try:
            return json.loads(text)
        except Exception:
            pass

        # 容错：从每个 "{" 起尝试 raw_decode，拿到首个可解析对象。
        # 这样在回复前后夹杂说明文字或其它垃圾片段时，仍能尽量提取决策 JSON。
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", text):
            start = m.start()
            try:
                obj, _end = decoder.raw_decode(text[start:])
            except Exception:
                continue
            if isinstance(obj, dict):
                return obj

        # 最后兜底：修复常见的“近似 JSON 但 reply 未转义换行导致坏掉”的 unified 回复。
        # 典型输入：{"tool":"none","payload":{},"reply":"困了...<换行>
        tool_m = re.search(
            r"""["']tool["']\s*:\s*["']([^"'\r\n]+)""",
            text,
            flags=re.I | re.S,
        )
        if not tool_m:
            return {}
        tool = (tool_m.group(1) or "").strip()
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
            intent_early, _ = detect_intent(text)
            if intent_early == INTENT_QUERY_TODO:
                log_flow_event(
                    stage="route",
                    route="query_todo_intent",
                    user_text=text,
                    from_user=from_user,
                    session_id=self.session_id,
                    extra={"intent": intent_early},
                )
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
                log_flow_event(
                    stage="exit",
                    route="fast_unified",
                    user_text=text,
                    from_user=from_user,
                    session_id=self.session_id,
                    extra={"handled": handled_fast},
                )
                if handled_fast:
                    return

            # ─── 小模型意图分类兜底（只在规则/fast 都未命中时触发）───
            oc_cfg = self.cfg.get("opencode", {}) or {}
            high_th = float(oc_cfg.get("intent_threshold_high", 0.8) or 0.8)
            mid_th = float(oc_cfg.get("intent_threshold_mid", 0.5) or 0.5)
            intent_model = oc_cfg.get("intent_model") or None
            queue_state = snapshot_todo_queue(self._todo_queues, from_user)
            intent_obj = await classify_intent(
                self.acp,
                model=intent_model,
                text=text,
                queue_state=queue_state,
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
                        "intent": intent_obj.get("intent"),
                        "slots": intent_obj.get("slots", {}),
                        "confidence": conf,
                        "queue": queue_state,
                    },
                )
                if conf >= high_th:
                    decision = intent_to_decision(intent_obj)
                    if decision:
                        handled_sm = await self._apply_unified_decision(
                            decision,
                            from_user,
                            context_token,
                            user_text=text,
                        )
                        log_flow_event(
                            stage="exit",
                            route="intent_llm",
                            user_text=text,
                            from_user=from_user,
                            session_id=self.session_id,
                            extra={
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
                extra={
                    "queue": queue_state,
                    "intent_hint": intent_hint,
                },
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
            log_flow_event(
                stage="exit",
                route="llm_unified",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={
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
                    extra={"remind_payload": data},
                )
                # data 形如 "07:30：上班" 或 "上班"（无时间则缺 hhmm，由 remind.add 兜底要时间）
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
                print(f"[Bot] ⏰ 提醒: {data}")
                return
            if intent == INTENT_QUERY_REMIND:
                log_flow_event(
                    stage="route",
                    route="query_remind_intent",
                    user_text=text,
                    from_user=from_user,
                    session_id=self.session_id,
                )
                await self._local_view_with_optional_llm_fallback(
                    "remind", from_user, context_token, text
                )
                return

            # 安全兜底：所有路由都没接住 → 不让 LLM 自由写盘，回固定模板，
            # 同时把意图信息写进 flow log，便于事后复盘补充规则。
            log_flow_event(
                stage="route",
                route="safe_fallback",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={
                    "intent": intent,
                    "intent_data": data,
                    "intent_source": "safe_fallback",
                    "intent_hint": intent_hint,
                },
            )
            await self.wx.send_text(
                "我没看懂这条要怎么记。要我把它当作生活记录写进今日日记吗？回「记一下」即可。",
                from_user,
                context_token,
            )
            print(f"[Bot] >>> safe_fallback: {text[:60]}")
        except Exception as e:
            print(f"[Bot] error: {e}")
            if self.cfg["bot"].get("reply_on_error", True):
                await self.wx.send_text(f"处理出错: {str(e)[:100]}", from_user, context_token)
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
