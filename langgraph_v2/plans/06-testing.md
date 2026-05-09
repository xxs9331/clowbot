# Phase 6：测试 + 回退机制

## 与仓库对齐（必读）

LangGraph 相关用例已集中在下列文件中，**不必**再按本计划逐文件新建一套平行 `test_state.py` / `test_graph.py`（除非从 `test_langgraph_v2.py` **刻意拆分**）：

| 文件 | 覆盖范围 |
|------|----------|
| `tests/test_langgraph_v2.py` | `build_chat_graph`、`fast_rule`、`llm_decide`+vault、`local_view`（slash）、`none` 兜底等 |
| `tests/test_langgraph_v2_dispatcher.py` | `DualPathDispatcher` / 灰度相关 |
| `tests/test_langgraph_v2_classifier_adapter.py` | `IntentClassifier` 适配（若有） |
| `tests/test_langgraph_v2_acp_llm.py` | `UnifiedDecideLLM` / `ACPStructuredLLM` |

本 Phase 的增量：**在以上文件中补 case**，或拆模块时 **迁移并更新本表**。

**拓扑以 `langgraph_v2/graph.py` 与 `05-pre-intent-local.md` 为准**（`fast_rule` 在 **`pre_intent` 节点之前** 于文本主路径上执行）。

---

## 6.1 单元测试（能力矩阵 → 落地文件）

| 测试对象 | 测试内容 | 优先落地 |
|----------|----------|----------|
| `state.py` | `TypedDict` 键、可选字段语义 | 可在 `test_langgraph_v2.py` 或专拆 `tests/test_langgraph_v2_state.py` |
| `graph.py` | 条件边分支、`compile()` 无异常 | `test_langgraph_v2.py` + `langgraph_v2/visual.py` 人工/脚本 |
| `fast_rule` | 做完了 / 下一个 / `todo.merge_new_items` 短语 | `test_langgraph_v2.py`（已有） |
| `compose` | 兜底、`local_reply`/`tool_result` 优先级、`_sanitize_vague_none_reply`（接入后） | 扩展 `test_langgraph_v2.py` 或 `tests/test_langgraph_v2_compose.py` |
| `services.py` | 每 tool 正向/异常/空输入 | 新建 `tests/test_langgraph_v2_services.py` 或 `tests/test_services.py`（与 v2 命名统一即可） |
| `llm_decide` | mock `Decision` 形态；**回归** `intent_hint` → `structured_decide` / `hint_block` | `test_langgraph_v2.py` + `test_langgraph_v2_acp_llm.py` |

**说明**：`pytest.importorskip("langgraph")` 已在 `test_langgraph_v2.py` 使用；无 langgraph 环境时这些用例跳过。

**实现注**：`route_after_image_router` 在无图时返回字符串 **`"pre_intent"`**，但在 `graph.add_conditional_edges` 中映射到节点 **`commander`**（历史命名）；文档与断言按 **「无图 → commander」** 理解即可。

---

## 6.2 集成测试（5 条主链路）

用 **mock LLM + mock Vault + mock Todo**（必要时 mock classifier）构造 `ainvoke`，避免绑定真实 ACP/日期。

### 当前拓扑摘要（与 `graph.py` 一致）

**无图**：`normalize → image_router →` **commander**  
→（有 slash）**`local_view → compose`**  
→（无 slash）**`fast_rule`**  
→（命中 `decision`）**`execute → compose`**  
→（未命中）**`pre_intent → llm_decide → execute → compose`**

**有图**：`normalize → image_router → describe_img → fast_rule →` 同上（**不经 `commander`**）。

**`pre_intent` 节点**：仅在 **`fast_rule` 未命中** 时进入；`classifier is None` 时该节点仍执行但快速返回 `{}`。**快通道**（链路 2、4）不应增加 classifier / 意图 LLM 调用。

### 链路 1：生活记录

- **输入**（示例）：`今早体重 72kg`（与现单测类似即可）。
- **预期路径**：`normalize → image_router → commander → fast_rule`（未命中）`→ pre_intent → llm_decide → execute → compose`。
- **断言方式**：**mock** `LLMProvider` 固定返回 `Decision(tool="record.add", payload={...})`，再断言 **`vault.append_record`** 被调用、**`wx_out`** 含预期文案。  
  不宜把「自然语言 → 固定 `category: 身体`」写死为 golden，除非跑真实模型 E2E。
- **可选**：挂载 `_FakeClassifier` 时断言 **`fast_rule` 未命中后** `classifier` 才被调用。

### 链路 2：待办添加（fast_rule）

- **输入**：`添加待办：买牛奶，写报告`。
- **预期路径**：`normalize → image_router → commander → fast_rule → execute → compose`（**无 `pre_intent`、`无 llm_decide`**）。
- **预期**：`decision.tool == "todo.merge_new_items"`，`payload.tasks` 正确；**`llm.calls == 0`**（若挂了 mock LLM）。

### 链路 3：提醒

- **输入**（示例）：`明天 8 点提醒我开会`。
- **预期路径**：`… → commander → fast_rule`（通常未命中）`→ pre_intent → llm_decide → execute → compose`。
- **断言**：优先 **mock** `remind.add` 的 `Decision`；若测真实解析，需独立 E2E 并处理**日历/相对日期**，勿在默认集成里写死 `hhmm: "08:00"`。

### 链路 4：快通道

- **输入**：`做完了`。
- **预期路径**：`normalize → image_router → commander → fast_rule → execute → compose`（**无 `pre_intent`、`无 llm_decide`**）。
- **预期**：`todo.done_current` 行为与现 `test_fast_route_done_current` 一致；**`llm.calls == 0`**。

### 链路 5：闲聊 / none

- **输入**（示例）：`今天好累`。
- **预期路径**：`… → commander → fast_rule`（未命中）`→ pre_intent → llm_decide → execute → compose`（`execute` 对 `none` 仍执行但短路）。
- **预期**：若验收「reply 不为空、无空指代」，需 **mock LLM 在 `tool=="none"` 时仍返回非空 `reply`**，或单独测 **`_sanitize_vague_none_reply`**；否则当前 **`compose`** 在 `reply` 全空时可能走固定兜底文案，**不等于**「闲聊口吻」。

### 有图（可选第 6 条冒烟）

- **路径**：`describe_img → fast_rule → …`（未命中则 `pre_intent → llm_decide`）。
- **断言**：stub 文本下 **`fast_rule` 常未命中**，允许 **1 次** unified LLM；与 `05-pre-intent-local` 节奏表一致。

---

## 6.3 回退机制

### 配置回退

```yaml
# config.yaml
graph:
  enabled: false   # 关闭 LangGraph 主路径与 shadow 跑图（与 04-adapter-gray 一致）
```

**生效方式**：以当前实现为准——多为 **改配置后重启 bot**；若将来支持热读配置，再更新本文。

### 回退保证

- 旧 Handler（`handlers/base.py`、`handlers/dispatcher.py`）在并行期内保留。
- `graph.enabled=false` 时 **`DualPathDispatcher` 不调用 `ainvoke`**（零图副作用）。
- Shadow 对比日志保留周期（如 30 天）属运维策略，与代码验收分开。

### 回退触发条件（需 metrics/日志支撑）

- LangGraph 错误率、shadow 不一致率等阈值（如 1% / 5%）**仅在有埋点与看板时**可操作；否则作人工准则即可。

---

## 6.4 监控指标（可选实现）

```python
# DualPathDispatcher 或统一 metrics 模块（实现后才有意义）
metrics = {
    "graph_calls": 0,
    "legacy_calls": 0,
    "graph_errors": 0,
    "shadow_mismatches": 0,
    "avg_graph_ms": 0.0,
    "avg_legacy_ms": 0.0,
}
```

落地后再将计数/耗时写入日志或 Prometheus，本文不强制具体后端。

---

## 文件改动（增量）

| 动作 | 说明 |
|------|------|
| 扩展 `tests/test_langgraph_v2.py` | 补 5 条链路中尚未覆盖的断言；拓扑变更后校对 `pre_intent` / `llm.calls` |
| 可选 `tests/test_langgraph_v2_services.py` | 专测 `langgraph_v2/services.py` |
| 可选 `pytest` marker | 例如 `@pytest.mark.langgraph`，便于 CI 只跑 `pytest -m langgraph` |
| **不**要求 `tests/__init__.py` | 无特殊需要可不建 |

---

## 验收

- [ ] `pytest tests/test_langgraph_v2.py tests/test_langgraph_v2_dispatcher.py tests/test_langgraph_v2_acp_llm.py tests/test_langgraph_v2_classifier_adapter.py`（及已存在的 v2 相关文件）通过；或项目约定 `pytest -m langgraph` 通过
- [ ] **不**将「整仓 `pytest tests/` 必绿」作为 LangGraph Phase 6 门槛（除非 CI 已隔离 e2e/真实 ACP）
- [ ] 链路 2、4：**`llm.calls == 0`**，且 **`pre_intent` / classifier 在快通道上不被需要**（或 `classifier.calls == 0` 若可观测）
- [ ] 链路 1、3、5：**`fast_rule` 未命中后**出现 `pre_intent → llm_decide`（与 `graph.py` 一致）
- [ ] `graph.enabled=false` 时无 `ainvoke`、行为与仅旧 Handler 一致（见 dispatcher 单测或手工）
