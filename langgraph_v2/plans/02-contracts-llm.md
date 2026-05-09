# Phase 2：Contracts 扩展 + ACP LLM 适配

## 2.1 contracts.py 基线与目标

**当前**（以仓库为准）：

| 类型 | 名称 | 说明 |
|------|------|------|
| dataclass | `Decision` | `tool` / `payload` / `reply`，非 Protocol |
| Protocol | `LLMProvider` | `structured_decide(*, user_id, text, queue_snapshot) -> Decision` |
| Protocol | `VaultRepository` / `TodoRepository` | 不变 |
| Protocol | `ImageLLMProvider` | 已有，方法名为 **`describe_image`**（不是 `describe`） |
| Protocol | `IntentClassifier` | 已有，签名为 **`user_id` + `text` + `queue_snapshot`**，与 `LLMProvider` 对齐 |
| Protocol | `ACLProvider` | 已有，**`async`** `is_agent_user(*, user_id: str) -> bool` |
| Protocol | `ACPSessionPool` | 已有占位（`pass`），本阶段需 **补全可注入形态**（见 2.2） |

**本 Phase 要完成的事**：把 `ACPSessionPool` 从空壳补成可用类型；新增/落地 `langgraph_v2/adapters/`；**不**再重复发明与上表冲突的签名。

### 与实现对齐的 Protocol 摘录

```python
class ImageLLMProvider(Protocol):
    """多模态 LLM（describe_img 节点）。当前主模型若无多模态可先 stub，后续换支持 vision 的模型。"""

    async def describe_image(self, *, image_base64: str, image_mime: str) -> str: ...


class IntentClassifier(Protocol):
    """轻量意图分类；参数与 LLMProvider 一致，便于 pre_intent 节点传入队列上下文。"""

    async def classify(
        self, *, user_id: str, text: str, queue_snapshot: list[str]
    ) -> dict[str, Any]: ...


class ACLProvider(Protocol):
    """agent 模式白名单；异步便于将来接远端/缓存。"""

    async def is_agent_user(self, *, user_id: str) -> bool: ...
```

## 2.2 ACP Session 池

旧 Handler 在 `handlers/base.py::init_session` 中创建 **6** 个 session id（与域一一对应）：

| 字段 | 对应 Handler 属性 | 说明 |
|------|-------------------|------|
| unified | `unified_session_id` | 统一决策、`session_id` 默认别名 |
| todo | `todo_session_id` | 待办域 |
| record | `record_session_id` | 记录域 |
| remind | `remind_session_id` | 提醒域 |
| agent | `agent_session_id` | agent 域（白名单内才可能非空） |
| debug | `debug_session_id` | 调试模型专用（配置开启时才有） |

LangGraph 版通过 `GraphDeps.sessions` 注入，**须包含 debug**，避免与旧栈行为不一致。

**推荐**：使用 **frozen dataclass** 作为运行时载体，`GraphDeps` 中类型为该 dataclass；`contracts.py` 里可将 `ACPSessionPool` 改为该 dataclass 的 **类型别名**，或改为带 6 个 `str` 字段的 Protocol（只读属性）。避免长期保留无成员的 `Protocol`。

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ACPSessionPool:
    unified: str
    todo: str
    record: str
    remind: str
    agent: str
    debug: str  # 未启用调试时可置 ""
```

## 2.3 llm_decide：分层适配

**约束**：`llm_decide` 节点需要 ACP structured output，但 **graph 节点内不直接依赖** `OpenCodeACP`（与 Phase 1 依赖注入一致）。

### 两层职责

| 层 | 职责 | 典型类型 |
|----|------|----------|
| **传输层** | 给定 `session_id` + 已拼好的 `prompt` + `json_schema`，调用 `prompt_structured`，返回 `dict \| None` | `ACPStructuredLLM` |
| **LLMProvider 实现** | 拼装 unified 上下文（等价于 `DispatcherMixin._unified_decide_hint_and_context`、rules 块、schema 选择），按降级策略多次调用传输层，最终返回 `Decision` | 如 `UnifiedDecideLLM`（新建，实现 `LLMProvider`） |

`graph.py` 只依赖 **`LLMProvider`**；`UnifiedDecideLLM` 内部组合 `ACPStructuredLLM` + `sessions.unified`。

### 传输层示例（`langgraph_v2/adapters/acp_llm.py`）

```python
# langgraph_v2/adapters/acp_llm.py
class ACPStructuredLLM:
    """仅封装 ACP prompt_structured，不做业务 prompt 拼装。"""

    def __init__(self, acp: OpenCodeACP, session_id: str):
        self._acp = acp
        self._session = session_id

    async def prompt_structured(
        self, *, prompt: str, schema: dict[str, Any], retry: int = 3
    ) -> dict[str, Any] | None:
        return await self._acp.prompt_structured(
            self._session, prompt, json_schema=schema, retry_count=retry
        )
```

（方法名用 `prompt_structured` 可避免与 `LLMProvider.structured_decide` 混淆。）

### 降级策略

与 `handlers/dispatcher.py` 中 `_llm_unified_decide` 一致（实现时按函数逐段对齐，勿只记三句口号）：

1. **structured_combined**（`include_reply=True` 的 schema）→ 成功则 `_coalesce_unified_decision` 后返回 `Decision`
2. 失败 → **structured_decision_only** + **`_llm_generate_unified_reply`** 补 `reply`
3. 再失败 → **全文本**兜底（与现有 trace / 模板段一致）

参考锚点：`_llm_unified_decide_structured_combined`、`_llm_unified_decide_structured_decision_only`、`_llm_generate_unified_reply`、`_llm_unified_decide`。

## 2.4 Prompt 加载

复用 `prompts/unified_decide.md`，保持热更新。适配层在需要时调用与 `DispatcherMixin._load_unified_decide_prompts()` **相同语义**的加载方式（每次调用重新读文件，与现网一致）。

可考虑从 `handlers/dispatcher.py` **复用** `_load_unified_decide_prompts`（若担心循环导入，再抽 `utils/unified_prompts.py` —— 属实现细节，本 Phase 不强制）。

## 2.5 文件与路径

**建议目录**：`langgraph_v2/adapters/`（与 `graph.py` 同顶层包，相对导入清晰）。与 `plans/05-intent-view.md` 等文中「`adapters/...`」统指该目录。

| 文件 | 动作 |
|------|------|
| `langgraph_v2/contracts.py` | 将 `ACPSessionPool` 补全为 dataclass 或等价 Protocol；其余签名已与本文一致则仅小修 |
| `langgraph_v2/adapters/__init__.py` | 新建 |
| `langgraph_v2/adapters/acp_llm.py` | 新建：`ACPStructuredLLM` +（可选同文件）`UnifiedDecideLLM` |

## 验收

- [ ] `ImageLLMProvider` / `IntentClassifier` / `ACLProvider` / `ACPSessionPool` 与本文及 `contracts.py` 一致，可通过 **typing.Protocol** 的 structurally-typed mock 满足
- [ ] `UnifiedDecideLLM`（或等价实现）**`isinstance` 语义上**实现 `LLMProvider`（或由静态类型检查确认）
- [ ] `langgraph_v2/adapters/acp_llm.py` 中 `ACPStructuredLLM` 可单测（mock `OpenCodeACP.prompt_structured`）
- [ ] 降级路径与 `dispatcher._llm_unified_decide` 行为可对齐抽查（至少 combined → decision_only+reply 两条）
