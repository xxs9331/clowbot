from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph

from .contracts import (
    ACLProvider,
    ACPSessionPool,
    ImageLLMProvider,
    IntentClassifier,
    LLMProvider,
    TodoRepository,
    VaultRepository,
)
from .nodes import (
    compose as compose_node,
    describe_img as describe_img_node,
    execute as execute_node,
    fast_rule as fast_rule_node,
    llm_decide as llm_decide_node,
    local_view as local_view_node,
    normalize as normalize_node,
    pre_intent as pre_intent_node,
    route_after_commander,
    route_after_fast_rule,
    route_after_image_router,
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

def build_chat_graph(deps: GraphDeps):
    services = DomainServices(deps.vault, deps.todo)
    graph = StateGraph(ClawBotState)

    async def _pre_intent(state: ClawBotState) -> ClawBotState:
        return await pre_intent_node(state, deps)

    async def _describe_img(state: ClawBotState) -> ClawBotState:
        return await describe_img_node(state, deps)

    async def _local_view(state: ClawBotState) -> ClawBotState:
        return await local_view_node(state, deps)

    async def _llm_decide(state: ClawBotState) -> ClawBotState:
        return await llm_decide_node(state, deps)

    async def _execute(state: ClawBotState) -> ClawBotState:
        return await execute_node(state, services)

    graph.add_node("normalize", normalize_node)
    graph.add_node("image_router", lambda s: s)
    graph.add_node("pre_intent", _pre_intent)
    graph.add_node("describe_img", _describe_img)
    graph.add_node("commander", lambda s: s)
    graph.add_node("local_view", _local_view)
    graph.add_node("fast_rule", fast_rule_node)
    graph.add_node("llm_decide", _llm_decide)
    graph.add_node("execute", _execute)
    graph.add_node("compose", compose_node)

    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "image_router")
    graph.add_conditional_edges(
        "image_router",
        route_after_image_router,
        {"describe_img": "describe_img", "pre_intent": "commander"},
    )
    graph.add_edge("describe_img", "fast_rule")
    graph.add_conditional_edges(
        "commander",
        route_after_commander,
        {"local_view": "local_view", "fast_rule": "fast_rule"},
    )
    graph.add_edge("local_view", "compose")
    graph.add_conditional_edges(
        "fast_rule",
        route_after_fast_rule,
        {"execute": "execute", "llm_decide": "pre_intent"},
    )
    graph.add_edge("pre_intent", "llm_decide")
    graph.add_edge("llm_decide", "execute")
    graph.add_edge("execute", "compose")
    graph.add_edge("compose", END)

    return graph.compile()
