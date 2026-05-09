from __future__ import annotations

import uuid
from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph

from .contracts import (
    ACLProvider,
    ACPSessionPool,
    Decision,
    ImageLLMProvider,
    IntentClassifier,
    LLMProvider,
    TodoRepository,
    VaultRepository,
)
from .services import DomainServices
from .state import ClawBotState


@dataclass
class GraphDeps:
    llm: LLMProvider
    image_llm: ImageLLMProvider | None
    classifier: IntentClassifier | None
    vault: VaultRepository
    todo: TodoRepository
    sessions: ACPSessionPool | None = None
    acl: ACLProvider | None = None


def _split_tasks(text: str) -> list[str]:
    raw = text.replace("：", ":")
    for prefix in ("添加待办:", "待办:", "todo:"):
        if raw.startswith(prefix):
            body = raw[len(prefix) :]
            return [x.strip() for x in body.replace("、", ",").split(",") if x.strip()]
    return []


def build_chat_graph(deps: GraphDeps):
    services = DomainServices(deps.vault, deps.todo)
    graph = StateGraph(ClawBotState)

    async def normalize(state: ClawBotState) -> ClawBotState:
        text = str(state.get("text") or "").strip()
        cmd = ""
        if text.startswith("/"):
            cmd = text[1:].strip()
        return {
            "text": text,
            "command_kind": cmd,
            "msg_trace": str(state.get("msg_trace") or uuid.uuid4().hex[:12]),
            "handled": bool(state.get("handled", False)),
            "queue_snapshot": list(state.get("queue_snapshot") or []),
            "wx_out": list(state.get("wx_out") or []),
            "tool_result": str(state.get("tool_result") or ""),
            "error": str(state.get("error") or ""),
            "agent_mode": bool(state.get("agent_mode", False)),
            "image_base64": str(state.get("image_base64") or "").strip(),
            "image_mime": str(state.get("image_mime") or "").strip(),
            "intent_hint": dict(state.get("intent_hint") or {}),
        }

    async def pre_intent(state: ClawBotState) -> ClawBotState:
        if deps.classifier is None:
            return {}
        text = str(state.get("text") or "").strip()
        if not text:
            return {}
        hint = await deps.classifier.classify(
            user_id=str(state.get("from_user") or ""),
            text=text,
            queue_snapshot=list(state.get("queue_snapshot") or []),
        )
        return {"intent_hint": hint if isinstance(hint, dict) else {}}

    async def describe_img(state: ClawBotState) -> ClawBotState:
        img = str(state.get("image_base64") or "").strip()
        if not img:
            return {}
        if deps.image_llm is None:
            return {"text": str(state.get("text") or "").strip() or "收到图片"}
        desc = await deps.image_llm.describe_image(
            image_base64=img,
            image_mime=str(state.get("image_mime") or "").strip(),
        )
        desc = str(desc or "").strip()
        if not desc:
            desc = "收到图片"
        return {"text": desc}

    async def local_view(state: ClawBotState) -> ClawBotState:
        cmd = str(state.get("command_kind") or "")
        if cmd in ("待办", "提醒", "记录", "时间轴"):
            view_map = {
                "待办": "todo",
                "提醒": "remind",
                "记录": "record",
                "时间轴": "timeline",
            }
            body = await deps.vault.read_view(kind=view_map[cmd])
            return {"handled": True, "reply": str(body or "").strip(), "tool_result": str(body or "").strip()}
        return {"handled": False}

    async def fast_rule(state: ClawBotState) -> ClawBotState:
        text = str(state.get("text") or "")
        low = text.lower()
        if text in ("做完了", "好了", "完成了", "done"):
            return {"decision": {"tool": "todo.done_current", "payload": {}, "reply": ""}}
        if text in ("下一个", "next"):
            return {"decision": {"tool": "todo.next", "payload": {}, "reply": ""}}
        tasks = _split_tasks(low)
        if tasks:
            return {
                "decision": {
                    "tool": "todo.merge_new_items",
                    "payload": {"tasks": tasks},
                    "reply": "",
                }
            }
        return {}

    async def llm_decide(state: ClawBotState) -> ClawBotState:
        if state.get("decision"):
            return {}
        d: Decision = await deps.llm.structured_decide(
            user_id=str(state.get("from_user") or ""),
            text=str(state.get("text") or ""),
            queue_snapshot=list(state.get("queue_snapshot") or []),
        )
        return {"decision": {"tool": d.tool, "payload": d.payload, "reply": d.reply}}

    async def execute(state: ClawBotState) -> ClawBotState:
        decision = state.get("decision") or {}
        tool = str(decision.get("tool") or "none")
        payload = decision.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        handled, out = await services.execute(
            user_id=str(state.get("from_user") or ""),
            tool=tool,
            payload=payload,
        )
        return {"handled": handled, "tool_result": str(out or "")}

    async def compose(state: ClawBotState) -> ClawBotState:
        decision = state.get("decision") or {}
        tool = str(decision.get("tool") or "none")
        decision_reply = str(decision.get("reply") or "").strip()
        tool_result = str(state.get("tool_result") or "").strip()
        local_reply = str(state.get("reply") or "").strip()
        if local_reply:
            return {"reply": local_reply, "wx_out": [local_reply]}
        if tool_result:
            return {"reply": tool_result, "wx_out": [tool_result]}
        if tool == "none":
            if decision_reply:
                return {"reply": decision_reply, "wx_out": [decision_reply]}
            fallback = "我没看懂这条要怎么记。要我把它当作生活记录写进今日日志吗？"
            return {"reply": fallback, "wx_out": [fallback]}
        if decision_reply:
            return {"reply": decision_reply, "wx_out": [decision_reply]}
        fallback = "处理完成。"
        return {"reply": fallback, "wx_out": [fallback]}

    def _route_after_image_router(state: ClawBotState) -> str:
        if str(state.get("image_base64") or "").strip():
            return "describe_img"
        return "pre_intent"

    def _route_after_commander(state: ClawBotState) -> str:
        if str(state.get("command_kind") or "").strip():
            return "local_view"
        return "fast_rule"

    def _route_after_fast_rule(state: ClawBotState) -> str:
        if state.get("decision"):
            return "execute"
        return "llm_decide"

    graph.add_node("normalize", normalize)
    graph.add_node("image_router", lambda s: s)
    graph.add_node("pre_intent", pre_intent)
    graph.add_node("describe_img", describe_img)
    graph.add_node("commander", lambda s: s)
    graph.add_node("local_view", local_view)
    graph.add_node("fast_rule", fast_rule)
    graph.add_node("llm_decide", llm_decide)
    graph.add_node("execute", execute)
    graph.add_node("compose", compose)

    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "image_router")
    graph.add_conditional_edges(
        "image_router",
        _route_after_image_router,
        {"describe_img": "describe_img", "pre_intent": "pre_intent"},
    )
    graph.add_edge("pre_intent", "commander")
    graph.add_edge("describe_img", "fast_rule")
    graph.add_conditional_edges(
        "commander",
        _route_after_commander,
        {"local_view": "local_view", "fast_rule": "fast_rule"},
    )
    graph.add_edge("local_view", "compose")
    graph.add_conditional_edges(
        "fast_rule",
        _route_after_fast_rule,
        {"execute": "execute", "llm_decide": "llm_decide"},
    )
    graph.add_edge("llm_decide", "execute")
    graph.add_edge("execute", "compose")
    graph.add_edge("compose", END)

    return graph.compile()

