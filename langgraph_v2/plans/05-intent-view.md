# Phase 5：pre_intent + local_view 对齐与增强

## 现状（与文档同步）

Phase 3 已落地 **`langgraph_v2/nodes/`** 模块，`graph.py` 已挂载：

- **`pre_intent`**：`deps.classifier` 非空时调用 `IntentClassifier.classify(user_id, text, queue_snapshot)`，写入 `intent_hint`（见 `nodes/pre_intent.py`）。
- **`local_view`**：`command_kind` ∈ {待办, 提醒, 记录, 时间轴} 时 `vault.read_view`，并设置 `reply` + `tool_result`（见 `nodes/local_view.py`）。
- **`describe_img`**：`image_llm is None` 时仅设置占位 `text`（如「收到图片」），**不**写入 `decision`，避免干扰 `fast_rule` 后的条件边（见 `nodes/describe_img.py`）。

本 Phase 的重点不是「从 stub 改成实现」，而是：**与旧栈意图/本地查看对齐**、**把 `intent_hint` 真正接入 unified 决策 prompt**、按需扩展 slash/关键词与适配器。

---

## 5.1 pre_intent：两套意图源（勿混用）

| 来源 | 模块 | 性质 | 输出形态 |
|------|------|------|----------|
| 规则 | `utils/intent.detect_intent` | 正则/关键词，**无 LLM prompt** | `(intent_type, data)` 元组，常量见 `utils/intent.py`（如 `INTENT_REMIND`、`INTENT_TODO`） |
| LLM | `utils/intent_llm.py` + `prompts/intent_classify.md` | 与线上一致的小模型分类 | JSON：`intent` / `slots` / `confidence`，`intent` ∈ `intent_llm.INTENT_LABELS`（如 `todo_add`、`record_add`、`query_todo`、`none`） |

**计划任选或组合**：

- **A**：`IntentClassifier` 实现内调用 **`intent_llm`** 现有加载与 ACP 会话逻辑（与 Handler 行为对齐，推荐）。
- **B**：仅用 **`detect_intent`** 规则，把结果规整为 `dict` 写入 `intent_hint`（成本低，覆盖窄）。
- **C**：规则先跑，未命中再 LLM（需约定优先级与超时）。

### `IntentClassifier` 签名（与 `contracts.py` 一致）

```python
async def classify(
    self, *, user_id: str, text: str, queue_snapshot: list[str]
) -> dict[str, Any]: ...
```

### 适配器位置与示例方向

新建 **`langgraph_v2/adapters/classifier_adapter.py`**（与 `adapters/acp_llm.py` 同目录；勿与仓库根 `adapters/` 混淆）。

实现类需按 **`intent_llm`** 的契约组 prompt（含可选 `queue_snapshot` / 用户上下文块），调用 `OpenCodeACP.prompt`，解析为 **dict**（失败返回 `{}`，不打断主流程）。具体可复用或抽取 `intent_llm` 内已有函数，避免复制 `intent_classify.md` 逻辑。

---

## 5.2 `intent_hint` → `llm_decide`（当前缺口）

`nodes/llm_decide.py` 仅调用：

`deps.llm.structured_decide(user_id=..., text=..., queue_snapshot=...)`，**未**传入 `state["intent_hint"]`。

`UnifiedDecideLLM` 中 `_build_combined_prompt` / `_build_decision_only_prompt` 里 **`hint_block` 目前恒为空字符串**（见 `adapters/acp_llm.py`），与 `DispatcherMixin` 可拼 hint 的行为不一致。

**本 Phase 必做（闭环）**：

1. 扩展 **`LLMProvider.structured_decide`** 可选参数，例如 `intent_hint: dict[str, Any] | None = None`（或并列 `**kwargs`），并在 **`UnifiedDecideLLM`** 内把 hint 格式化为 `hint_block` 填入 `unified_decide` 模板。
2. **`llm_decide` 节点**从 `state` 取出 `intent_hint` 传入 `structured_decide`。

完成后，验收项「hint 注入 llm_decide」才成立。详见交叉引用：`02-contracts-llm.md`（Protocol）、`03-nodes.md`（llm_decide 优先级）。

---

## 5.3 local_view

**职责**：本地读 Obsidian / 今日日志切片，**不**触发 LLM 决策；由 **`compose`** 按 `local_reply` 优先输出。

**当前触发条件**：仅 **`commander`** 在 `command_kind` 非空时进入 **`local_view`**（`normalize` 已把 `/xxx` 解析为 `command_kind`）。与 `handlers/base.py` 中更宽的 slash 别名（如「查看待办」→ 待办）**可能仍不一致**，若要对齐需在 **`normalize` 或 commander 前**增加同构映射，而不是只改 `local_view` 函数体。

**「查看类关键词」免 slash**：当前图 **没有**「纯自然语言 → local_view」的边；若产品需要，需在 **`commander`**（或新节点）中识别关键词并设置与 `local_view` 兼容的 `command_kind` / 内部路由，并更新 `01-state-graph` 拓扑说明。

**实现参考**：`handlers/local_view.py`（`LocalViewMixin`）中与只读展示相关的逻辑；**Vault 侧**已可通过 **`VaultRepository.read_view`**（Handler 上适配方法）复用读路径。

---

## 5.4 describe_img

**当前行为**（保持）：无多模态时只更新 **`text`**，不设 **`decision`**，避免 `fast_rule` 误判「规则已命中」直跳 **`execute`**。

多模态就绪后：为 **`GraphDeps.image_llm`** 注入 **`ImageLLMProvider`** 实现即可；**禁止**在 stub 里写死带 `decision` 的返回，除非明确希望跳过 `fast_rule`/`llm_decide`（一般不建议）。

---

## 文件改动（增量）

| 文件 | 动作 |
|------|------|
| `langgraph_v2/adapters/classifier_adapter.py` | 新建：`FlashIntentClassifier`（或等价），对齐 `IntentClassifier` + `intent_llm` |
| `langgraph_v2/contracts.py` | 为 `LLMProvider.structured_decide` 增加 `intent_hint`（可选） |
| `langgraph_v2/adapters/acp_llm.py` | `UnifiedDecideLLM`：把 `intent_hint` 填入 `hint_block` |
| `langgraph_v2/nodes/llm_decide.py` | 传入 `intent_hint` |
| `langgraph_v2/nodes/normalize.py`（或 commander） | 可选：slash 别名与旧 Handler 对齐；可选：关键词 → 查看 |
| `handlers/base.py` | `_init_dual_dispatcher`：`GraphDeps(classifier=...)` 接入新适配器（若启用） |

---

## 验收

- [ ] `IntentClassifier` 实现与 **`intent_llm` 标签/契约**一致（或明确文档说明仅用规则 `detect_intent`）
- [ ] `intent_hint` 非空时，**`UnifiedDecideLLM` 的 prompt 中可见格式化后的 hint**（与旧 unified 行为可比）
- [ ] `/待办`、`/提醒`、`/记录`、`/时间轴`（及与 normalize 对齐后的等价 slash）**本地读取**正确，`compose` 输出正确
- [ ] 图片：`image_llm is None` 时不崩溃、不产生错误 `decision` 副作用
- [ ] （可选）关键词触发 local_view：有对应单测或手动用例
