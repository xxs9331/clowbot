# ClawBot v1（Legacy 对话管线）归档

此目录保存 **LangGraph v2 上线前** 的主对话路由实现快照，仅供对照与回溯，**不参与运行时代码加载**。

## 当时架构要点

- 入口：`Handler._run_chat_routing_pipeline`（规则守卫 → `build_fast_unified_decision` 快通道 → 可选 wx agent → `_llm_unified_decide` → `_apply_unified_decision` → 意图兜底）。
- 与 v2 并行期：通过 `graph.enabled` / `DualPathDispatcher` 做灰度与 shadow。

## 当前产品路径

生产环境仅保留 **`langgraph_v2`** 编译图 + `ChatGraphDispatcher`；配置中的 `graph.*` 灰度字段已废弃。

详见仓库内 `langgraph_v2/` 与 `handlers/base.py` 中的 `_init_chat_graph` / `_invoke_chat_graph` 调用链。
