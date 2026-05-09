# Phase 5：pre_intent + local_view 对齐与增强（主文档）

> **索引**：`05-intent-view.md` 为早期稿；**节奏、拓扑与验收以本文为准**。

## 0. 节奏约束

**问题**：拓扑若为 `normalize → image_router → pre_intent → commander → fast_rule`，且 `pre_intent` 使用 **`classify_intent`（LLM）**，则用户发「做完了」也会先多一次意图 LLM，再进 `fast_rule`，比旧栈慢。

**旧栈**（概念上）：先能规则短路则短路，再 unified（1 次 LLM），意图细分类多在 unified / 少数 fallback 里完成，**不默认前置独立意图 LLM**。

**本 Phase 目标**：

- **拓扑**：`fast_rule` 尽量在 **`pre_intent` 之前**（见 §5.3），使「做完了 / 下一个 / 添加待办:」等 **0 次 LLM** 即可 `execute`。
- **pre_intent（目标形态）**：仅 **`utils/intent.detect_intent`**，**0 次 LLM**；hint 仅作 `llm_decide` 辅助信号。
- **`llm_decide`**：统一决策；已有 **`intent_hint` → `hint_block`**（见 §5.2）。

**与代码现状**：`graph.py` **可能仍为** `pre_intent → commander → fast_rule`；**`FlashIntentClassifier` 仍可能调用 `intent_llm`**。若与上表冲突，以 **§5.1 二选一** 为准推进重构。

---

## 现状摘要

- **`nodes/pre_intent` / `local_view` / `describe_img`** 已存在（非 stub）。
- **`intent_hint` 注入 unified** 已在 `contracts.LLMProvider`、`nodes/llm_decide`、`UnifiedDecideLLM._build_hint_block` 落地（§5.2）。

---

## 5.1 pre_intent：规则优先 vs 现网适配器

### 目标：`RuleIntentClassifier`（零 LLM）

| 来源 | 模块 | LLM |
|------|------|-----|
| 规则 | `utils/intent.detect_intent` | **0** |

**策略**：`detect_intent(text) → (intent_type, data)`；非 `INTENT_NONE` 时规整为 `intent_hint`（如 `{"intent": "<与 unified schema 对齐的标签>", "slots": {...}, "confidence": 1.0}`）；否则不写，`llm_decide` 自行判断。

**标签映射**须与 `detect_intent` **全部返回值**一致，并映射到与 `intent_llm` / unified 可读的形式（示例，**不完整处补全请对照 `utils/intent.py` 常量**）：

| `utils/intent` 常量 | 建议 `intent_hint["intent"]` |
|---------------------|------------------------------|
| `INTENT_REMIND` | `remind_add`（slots 含 `hhmm` / `text`，由 `data` 解析） |
| `INTENT_TODO` | `todo_add` |
| `INTENT_TODO_DONE` | `todo_done` |
| `INTENT_TODO_NEXT` | `todo_next` |
| `INTENT_TODO_NOT_DONE` | `todo_not_done` |
| `INTENT_NONE` | 不写 `intent_hint` |

### 现网：`FlashIntentClassifier`（`classifier_adapter.py`）

当前实现 **先 `await classify_intent`（LLM）**，失败再 **`_map_rule_intent`（detect_intent）**。这与 **「pre_intent 零 LLM」** 冲突。

**二选一**（团队定案后改代码或改配置）：

1. **替换为 `RuleIntentClassifier`**（仅 `detect_intent`），`GraphDeps.classifier` 指向该类；或  
2. **保留 Flash**，则接受「多数消息多 1 次意图 LLM」，**仍建议做 §5.3 拓扑前移 `fast_rule`**，至少保住「做完了」不经 `pre_intent`。

### `IntentClassifier` 签名

与 `contracts.py` 一致；实现可为 **`async def classify`** 且 **内部无 `await`（除未来可能的缓存等）**，禁止写「同步函数」破坏 Protocol。

```python
async def classify(
    self, *, user_id: str, text: str, queue_snapshot: list[str]
) -> dict[str, Any]: ...
```

---

## 5.2 `intent_hint` → `llm_decide`（已实现，做回归）

下列已在仓库落地，本文仅作 **回归 / Code Review** 检查点：

- `LLMProvider.structured_decide(..., intent_hint: dict | None = None)`
- `llm_decide` 传入 `state["intent_hint"]`
- `UnifiedDecideLLM`：`hint_block=self._build_hint_block(intent_hint)` 填入 `unified_decide` 模板

若行为与 `DispatcherMixin` 仍有差距，在 **`hint_block` 格式**或 **模板占位符** 上迭代，而非重复「加参数」类改造。

---

## 5.3 拓扑优化：`fast_rule` 前置

### 当前（常见实现）

纯文本：`normalize → image_router → pre_intent → commander → fast_rule → (execute | llm_decide) → …`  
有图：`… → describe_img → fast_rule → …`（**不经过** `commander` / `pre_intent`）。

### 目标（无图：先 `commander`；有图：先 `describe_img`，再在 `fast_rule` 汇合）

```
无图：image_router → commander ──有 slash──→ local_view → compose
                    commander ──无 slash──→ fast_rule ──命中──→ execute → compose
                                              │
                                            未命中 → pre_intent → llm_decide → execute → compose

有图：image_router → describe_img → fast_rule ──命中──→ execute → compose
                                        │
                                      未命中 → pre_intent → llm_decide → execute → compose
```

有图时 **不经 `commander`**：纯图片场景的 slash/caption 策略若有产品要求，需在 **`normalize`/`describe_img`** 单独立项，不在此默认拓扑里展开。

**含义**：

- **`image_router` 无图**：下一节点为 **`commander`**（不再先进 `pre_intent`）。
- **`image_router` 有图**：**`describe_img` → `fast_rule`**；与文本支路在 **`fast_rule` 汇合**（命中则 `execute`，否则 **`pre_intent` → `llm_decide`**）。纯图片无 slash 时通常 **无** `local_view`；若需带 `/待办` 等，依赖 **normalize 对 caption 的 `command_kind`** 与产品约定（边条件复杂时单开设计说明）。
- **`fast_rule` 未命中** → **`pre_intent`**（规则 hint）→ **`llm_decide`**。

### `graph.py` 改动要点

- 无图：`image_router` **false** → **`commander`**（替代原 → `pre_intent`）。
- `commander`：**true** → `local_view`；**false** → **`fast_rule`**（保持）。
- **`fast_rule`**：**true**（已有 `decision`）→ **`execute`**；**false** → **`pre_intent`** → **`llm_decide`**（替代原 false → 直接 `llm_decide`）。
- **`pre_intent`** → **`llm_decide`**（仅未命中 fast_rule 时进入）。
- **`describe_img`** → **`fast_rule`**（保持汇合点；若改为先进 `commander` 需单独论证 slash+图场景）。

### 节奏表（修正后）

| 场景 | LLM 次数（目标拓扑 + `RuleIntentClassifier`） |
|------|-----------------------------------------------|
| 「做完了」「下一个」 | **0**（`fast_rule` 命中） |
| `添加待办:` / `待办:` / `todo:` 可解析 | **0**（`fast_rule` 命中） |
| 「提醒我 …」含时间等（**未**扩写进 `fast_rule`） | 通常 **1**（`llm_decide`；`pre_intent` 仅规则 hint，0 LLM） |
| 「今早体重 72kg」等 | **1**（`llm_decide`） |
| 图片、`image_llm is None`、占位 `text` | 通常 **1**（`fast_rule` 多不命中 → `pre_intent` 规则 → `llm_decide`）；**不是** 0 |

若扩展 **`fast_rule`** 覆盖部分 remind 句式，可把对应 case 从「1」降为「0」，需在 `fast_rule.py` 与表中同步维护。

---

## 5.4 local_view

**职责**：本地读 Markdown / 今日日志切片；**不**触发 LLM；**`compose`** 优先 `local_reply`。

**触发**：**`commander`** 在 **`command_kind` 非空** 时进 **`local_view`**。与 **`handlers/base.py` slash 别名** 不一致时，在 **`normalize` / commander** 侧做映射，而非只改 `local_view`。

**免 slash 关键词**：需新路由或 commander 内关键词 → `command_kind`，并更新 **`01-state-graph.md`**。

**参考**：`handlers/local_view.py`、`VaultRepository.read_view`。

---

## 5.5 describe_img

无多模态时只写 **`text`**，**不写 `decision`**，避免误判 `fast_rule` 已命中。多模态就绪后注入 **`ImageLLMProvider`**。

---

## 文件改动（增量）

| 文件 | 动作 |
|------|------|
| `langgraph_v2/graph.py` | **拓扑重排**（§5.3） |
| `langgraph_v2/adapters/classifier_adapter.py` | **新增或改为** `RuleIntentClassifier`，或与 `FlashIntentClassifier` 二选一；与 §5.1 定案一致 |
| `handlers/base.py` | `_init_dual_dispatcher`：`GraphDeps.classifier=` 与定案一致 |
| `langgraph_v2/visual.py` / `plans/01-state-graph.md` | 与上新边一致后更新图示与说明 |
| ~~`contracts.py` / `acp_llm.py` / `llm_decide.py`~~ | **`intent_hint` 已具备**，除非协议再扩展 |

---

## 验收

- [ ] 定案 **Rule-only** 时：`classifier` 路径 **无** `classify_intent` / 意图专用 `acp.prompt`
- [ ] 「做完了」「下一个」在 **目标拓扑** 下 **0 次** unified / 意图 LLM（可用 mock 计数）
- [ ] `intent_hint` 非空时 **prompt 内可见** `hint_block`（回归）
- [ ] slash **本地读取**、`compose` 正确
- [ ] 图片 stub：**无错误 `decision`**；接受 **通常 1 次** `llm_decide` 或与扩展后的 `fast_rule` 一致
- [ ] **Mermaid / `visual.py`** 与 §5.3 一致
