from __future__ import annotations

from typing import Any, Mapping

from .flow_log import log_flow_event


def state_snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    text = str(state.get("text") or "")
    decision = state.get("decision")
    decision_tool = ""
    if isinstance(decision, dict):
        decision_tool = str(decision.get("tool") or "")
    intent_hint = state.get("intent_hint")
    if not isinstance(intent_hint, dict):
        intent_hint = {}
    queue_snapshot = state.get("queue_snapshot")
    if isinstance(queue_snapshot, list):
        queue_len = len(queue_snapshot)
    else:
        queue_len = 0
    return {
        "text_len": len(text),
        "has_image": bool(str(state.get("image_base64") or "").strip()),
        "command_kind": str(state.get("command_kind") or ""),
        "intent": intent_hint,
        "decision_tool": decision_tool,
        "handled": bool(state.get("handled", False)),
        "queue_len": queue_len,
        "error": str(state.get("error") or "")[:200],
    }


def log_node_entry(state: Mapping[str, Any], node_name: str) -> None:
    log_flow_event(
        stage="graph",
        route=f"node_{node_name}_enter",
        from_user=str(state.get("from_user") or ""),
        request_id=str(state.get("request_id") or ""),
        extra={
            "msg_trace": str(state.get("msg_trace") or ""),
            **state_snapshot(state),
        },
    )
