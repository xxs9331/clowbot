# Phase 3：节点实现

## 前置：可视化验证拓扑

**在写任何节点之前**，先确认 Graph 可编译、拓扑正确。

### visual.py（已就绪）

`langgraph_v2/visual.py` 三种用法：

```bash
# 1. 输出 Mermaid → 贴到 https://mermaid.live
python langgraph_v2/visual.py

# 2. 导出 PNG（需要 pip install pyppeteer pillow）
python langgraph_v2/visual.py --png

# 3. 显式 Mermaid
python langgraph_v2/visual.py --mermaid
```

验证清单：

- [x] `python langgraph_v2/visual.py` 无错误退出
- [ ] 在 mermaid.live 中确认 10 节点 + 3 条件边拓扑正确
- [ ] `--png` 导出 `docs/clawbot_topology.png`（可选）

---

## 当前状态

Phase 1/2 已执行完成。`graph.py` 中 10 个节点均在 **`build_chat_graph` 内联**（无独立 `nodes/*.py`）。下表 **行号仅作快照参考**，以函数名为准。

本 Phase 的职责从「首次实现」变为 **审查 + 对齐现有实现与计划差异**。

| 节点 | 状态 | 差异 / 说明 |
|------|------|-------------|
| normalize | ✅ | 合并初始化：含 `msg_trace`、`queue_snapshot`、`image_*`、`intent_hint`、`tool_result`、`error`、`agent_mode` 等 |
| image_router | ✅ pass-through | 条件边：`_route_after_image_router` |
| pre_intent | ✅ | `deps.classifier` 为 `None` 时跳过；写入 `intent_hint` |
| describe_img | ✅ | `image_llm is None` 时降级为占位 `text` |
| commander | ✅ pass-through | 条件边：`_route_after_commander` |
| local_view | ✅ | 识别 4 种 slash → `vault.read_view`；**未识别**时 `handled=False`，经 `compose` 在 `tool=="none"` 时走统一兜底文案（不静默空回） |
| fast_rule | ✅ | 规则：`todo.done_current` / `todo.next` / `todo.merge_new_items`（添加待办短语） |
| llm_decide | ✅ | 简版：单次 `deps.llm.structured_decide` |
| execute | ✅ | `DomainServices.execute`，**未**走 `_TOOL_HANDLERS` |
| compose | ✅ | 优先级：`local_reply` > `tool_result` > `decision_reply` > 兜底 |

### Tool 数量口径（避免「12 个」歧义）

- **旧栈**：`register_tool_handler` 共 **11 个可执行 tool**（todo 8 + timeline 1 + record 1 + remind 1）。
- **LLM schema**（如 `UnifiedDecideLLM._schema`）：enum 共 **12 项 = 11 个 tool + `none`**。
- **`services.execute` 当前**：仅覆盖其中 **7 个**（`record.add`、`remind.add`、`timeline.append`、`todo.merge_new_items`、`todo.done_current`、`todo.next`，外加 `none` 短路）；缺 `todo.not_done` / `todo.reorder` / `todo.reorder_confirm` / `todo.skip_current` / `todo.abandon_current`。

### 与计划的差异需确认

1. **execute 走 `services.py` 而非 `_TOOL_HANDLERS`**：缺 5 个 todo 类 tool 时 `execute` 返回 `handled=False`、空串，行为与旧 Dispatcher 不一致。补齐方式见下文 **P1 策略**。

2. **llm_decide 简版**：无 combined → decision_only → 全文本降级。`langgraph_v2/adapters/acp_llm.py` 中 **`UnifiedDecideLLM`** 已对齐旧链路——**将 `GraphDeps.llm` 注入为该实现**即可。

3. **compose 缺 `_sanitize_vague_none_reply`**：需从 `handlers/dispatcher.py` 迁入（或抽共享 util），与单测对齐。

4. **`error` / `agent_mode` / `acl`**（`01-state-graph`）：`normalize` 已带 `agent_mode`；节点侧尚未系统性写 `error`、也未接 `deps.acl`。可与 Phase 4/5 或 Phase 6 验收一并排期。

---

## 本 Phase 实际要做的

| 优先级 | 任务 | 复杂度 |
|--------|------|--------|
| **P0** | 验证 `visual.py` 输出拓扑与 `01-state-graph` 一致 | 低 |
| **P0** | `GraphDeps.llm` 注入 **`UnifiedDecideLLM`**（降级链路） | 低 |
| **P0** | `compose` 加 **`_sanitize_vague_none_reply`**（`tool=="none"` 等路径） | 中 |
| **P1** | **补齐 execute 与旧栈一致的 tool 覆盖**（见下策略） | 高～中 |
| **P2** | 节点拆到独立 `nodes/*.py`（当前内联可延后） | 低 |

### P1：execute 补齐策略（二选一，建议顺序）

| 路线 | 做法 | 优点 | 成本 |
|------|------|------|------|
| **A. 扩展 `services.py`** | 为缺 5 个 tool 各写分支，调用 `TodoRepository` / 与现 vault API 对齐 | 不绑 Coach `self`，LangGraph 边界清晰 | 可能与 post-write hooks、Coach 内存状态不完全一致，需逐 tool 对齐文案与副作用 |
| **B. 接入 `_TOOL_HANDLERS`** | 与 `DispatcherMixin._apply_unified_decision` 共用注册表 | 行为与线上一致 | 需解决 **handler 绑定的 mixin 实例**、生命周期与 `run_post_write_hooks`，工程量更大 |

**建议**：短期走 **A** 尽快覆盖 11 个 tool；中期再评估 **B** 统一执行内核。

---

## 验收

- [ ] `python langgraph_v2/visual.py` 输出与 mermaid.live 渲染一致
- [ ] `UnifiedDecideLLM` 降级链路验证（combined → decision_only → 全文本兜底）
- [ ] `compose` 空指代处理不差于旧版（对照 `test_dispatcher` / `test_opencode_client` 中与 `_sanitize_vague_none_reply` 相关的用例）
- [ ] `execute`：**11 个可执行 tool** 均有明确路径且与旧栈语义可比；`none` 仍短路（与 schema 12 项含 `none` 一致）
