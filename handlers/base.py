"""消息主入口与 Handler 组合"""

import asyncio
import json
import re

from acp.opencode_client import OpenCodeACP, build_system_prompt
from config import _log_reasoning
from utils.intent import (
    INTENT_QUERY_REMIND,
    INTENT_QUERY_TODO,
    INTENT_REMIND,
    detect_intent,
)
from utils.flow_log import log_flow_event, snapshot_todo_queue
from utils.route_fast import build_fast_unified_decision
from wechat.client import ClawBotClient

from .commands import CommandsMixin
from .image import ImageMixin
from .local_view import LocalViewMixin
from .todo import TodoMixin


class Handler(LocalViewMixin, TodoMixin, ImageMixin, CommandsMixin):
    def __init__(self, acp: OpenCodeACP, config: dict, wechat: ClawBotClient):
        self.acp = acp
        self.cfg = config
        self.wx = wechat
        self.session_id = ""
        self._reminded_ids = set()  # 已触发的提醒（避免重复推送）
        self._reminder_refresh = asyncio.Event()
        self._todo_queues = {}  # 用户维度短期待办队列
        self._pending_reorders = {}  # 用户维度重排确认缓存
        self._local_view_last = {}
        self._local_view_debounce_sec = 2.0

    async def init_session(self):
        """初始化 ACP session 并设置模型"""
        self.session_id = await self.acp.create_session()
        print(f"[Bot] session: {self.session_id}")

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
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}

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

        if text.startswith("/"):
            await self._cmd(text, from_user, context_token)
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

            log_flow_event(
                stage="route",
                route="llm_unified_enter",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={"queue": snapshot_todo_queue(self._todo_queues, from_user)},
            )
            decision = await self._llm_unified_decide(from_user, text)
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
                vault = self.cfg["vault"]
                system_prefix = build_system_prompt(
                    vault_root=vault["root"],
                    daily_log_dir=vault["daily_log_dir"],
                )
                prompt = (
                    f"{system_prefix}\n"
                    f"用户要设置提醒：「{data}」\n"
                    f"请在日志的「## ⏰ 提醒」节追加一行：\n"
                    f"- [ ] {data}\n"
                    f"如果该节不存在则创建。只回复确认信息。"
                )
                reply, _ = await self.acp.prompt(
                    self.session_id, prompt, trace_tag="intent_remind_append"
                )
                reply = (reply or "").strip()[:2000]
                await self.wx.send_text(reply or f"⏰ 已记录提醒：{data}", from_user, context_token)
                print(f"[Bot] ⏰ 提醒: {data}")
                self.notify_reminder_refresh()
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

            log_flow_event(
                stage="route",
                route="life_fallback",
                user_text=text,
                from_user=from_user,
                session_id=self.session_id,
                extra={"intent": intent, "intent_data": data},
            )
            vault = self.cfg["vault"]
            system_prefix = build_system_prompt(
                vault_root=vault["root"],
                daily_log_dir=vault["daily_log_dir"],
                project_dir=vault.get("project_dir", ""),
                task_dir=vault.get("task_dir", ""),
            )
            reply, reasoning = await self.acp.prompt(
                self.session_id,
                f"{system_prefix}\n用户发来：「{text}」",
                trace_tag="life_fallback",
            )
            if reasoning:
                print(f"[Bot] 🧠 {reasoning[:200]}")
                _log_reasoning(text, reasoning)
            reply = (reply or "").strip()[: self.cfg["bot"].get("max_reply_length", 2000)]
            await self.wx.send_text(reply or "收到", from_user, context_token)
            print(f"[Bot] >>> {(reply or '收到')[:80]}")
        except Exception as e:
            print(f"[Bot] error: {e}")
            if self.cfg["bot"].get("reply_on_error", True):
                await self.wx.send_text(f"处理出错: {str(e)[:100]}", from_user, context_token)
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
