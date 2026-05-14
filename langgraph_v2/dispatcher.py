from __future__ import annotations

import uuid
from typing import Awaitable, Callable

from utils.flow_log import log_flow_event


class ChatGraphDispatcher:
    """仅 LangGraph：调用编译图并发送 wx_out / reply。"""

    def __init__(self, *, graph):
        self._graph = graph

    async def ainvoke_graph(
        self,
        *,
        text: str,
        from_user: str,
        context_token: str,
        request_id: str = "",
        msg_trace: str = "",
        image_base64: str = "",
        image_mime: str = "",
        queue_snapshot: list[str] | None = None,
        agent_mode: bool = False,
    ) -> dict:
        qs = list(queue_snapshot or [])
        payload = {
            "text": text,
            "image_base64": image_base64,
            "image_mime": image_mime,
            "from_user": from_user,
            "context_token": context_token,
            "agent_mode": agent_mode,
            "queue_snapshot": qs,
            "request_id": request_id,
            "msg_trace": msg_trace,
        }
        out = await self._graph.ainvoke(payload)
        return out if isinstance(out, dict) else {}

    @staticmethod
    def _extract_reply(state: dict) -> str:
        wx_out = state.get("wx_out")
        if isinstance(wx_out, list) and wx_out:
            return str(wx_out[0] or "").strip()
        return str(state.get("reply") or "").strip()

    async def dispatch(
        self,
        *,
        text: str,
        from_user: str,
        context_token: str,
        queue_snapshot: list[str] | None,
        send_text: Callable[[str, str, str], Awaitable[object]],
        image_base64: str = "",
        image_mime: str = "",
        agent_mode: bool = False,
    ) -> dict:
        """执行图并发送首条可见回复；返回最终 state（供评测/结构化状态更新）。"""
        request_id = uuid.uuid4().hex
        msg_trace = request_id[:12]
        try:
            state = await self.ainvoke_graph(
                text=text,
                from_user=from_user,
                context_token=context_token,
                request_id=request_id,
                msg_trace=msg_trace,
                image_base64=image_base64,
                image_mime=image_mime,
                queue_snapshot=queue_snapshot,
                agent_mode=agent_mode,
            )
            reply = self._extract_reply(state)
            if reply:
                await send_text(reply, from_user, context_token)
            else:
                await send_text("处理完成。", from_user, context_token)
                reply = "处理完成。"
            return {
                **state,
                "request_id": str(state.get("request_id") or request_id),
                "msg_trace": str(state.get("msg_trace") or msg_trace),
                "_dispatch_reply_sent": reply,
            }
        except Exception as e:  # noqa: BLE001
            log_flow_event(
                stage="graph",
                route="dispatch_failed",
                user_text=text[:200],
                from_user=from_user,
                request_id=request_id,
                extra={
                    "msg_trace": msg_trace,
                    "request_id": request_id,
                    "error": str(e)[:200],
                },
            )
            err_reply = f"处理出错: {str(e)[:100]}"
            await send_text(err_reply, from_user, context_token)
            return {
                "request_id": request_id,
                "msg_trace": msg_trace,
                "error": str(e),
                "_dispatch_reply_sent": err_reply,
            }
