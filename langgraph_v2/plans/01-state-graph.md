# Phase 1：State 扩展 + Graph 拓扑定型

## 1.1 扩展 ClawBotState

**基线**（当前 `state.py`）：**11 个键**。本阶段 **新增 5 个键** → **全量共 16 个键**。

| 已有键 | 说明 |
|--------|------|
| text, from_user, context_token | 入参 / 会话 |
| msg_trace, queue_snapshot, command_kind | 追踪与队列 |
| decision, handled | 决策与是否已处理 |
| reply, wx_out, error | 输出与错误 |

| 新增键 | 用途 |
|--------|------|
| image_base64 | 微信图片原始 base64（纯 payload，不含 `data:` 前缀与否在实现层约定） |
| image_mime | 图片 MIME 类型 |
| intent_hint | 小模型预分类结果（`pre_intent` 写入；**不**替代 `decision`） |
| agent_mode | 白名单 agent 模式开关 |
| tool_result | `execute` 节点原始输出文本（与面向用户的 `reply` 分工见下） |

**`tool_result` vs `reply`**：`execute` 将工具/领域层返回的原文写入 `tool_result`；`compose` 依据 `decision.reply`、`tool_result` 与兜底文案生成最终 `reply` / `wx_out`。若某工具路径暂不拆分，可短期只写 `reply`，`tool_result` 留空。

### 目标 State（16 键）

```python
from typing import Any, TypedDict

class ClawBotState(TypedDict, total=False):
    # ── 入参 ──
    text: str
    image_base64: str
    image_mime: str
    from_user: str
    context_token: str
    agent_mode: bool

    # ── 中间产物 ──
    msg_trace: str
    command_kind: str
    intent_hint: dict[str, Any]
    queue_snapshot: list[str]
    decision: dict[str, Any]          # {tool, payload, reply}
    handled: bool
    tool_result: str
    error: str

    # ── 出参 ──
    reply: str
    wx_out: list[str]
```

## 1.2 图拓扑

10 节点 + 3 条件边。

```
START -> normalize -> image_router
                         /        \
                    text           image
                    /                \
              pre_intent        describe_img
                   |                  |
              commander           fast_rule
               /      \           /      \
            cmd     no cmd     hit       miss
             |         |        |          |
        local_view     |    execute    llm_decide
             |         |        |          |
        compose ------+        |      execute
                                |          |
                                +----+-----+
                                     |
                                  compose -> END
```

### 旧图 → 新图（节点名对照）

| 当前 `graph.py` | Phase 1 拓扑 |
|-----------------|--------------|
| `init_context` + `preprocess` | `normalize`（合并，见 `03-nodes.md` 3.1） |
| `local_command_or_reader` | `local_view`（职责不变时可再拆） |
| `fast_route` | `fast_rule` |
| `router_decide` | `llm_decide` |
| `execute_tool` | `execute` |
| `compose_reply` | `compose` |

## 1.3 条件路由表

| 分流点 | True 条件 | True -> | False -> |
|--------|-----------|---------|----------|
| image_router | `image_base64` 经规范化后非空（建议：`.strip()` 后判空；禁止仅空白串走图片支路） | describe_img | pre_intent |
| commander | `command_kind` 非空（与现 `preprocess` 一致：`/` 后为整段 strip） | local_view | fast_rule |
| fast_rule | `decision` 非空（**仅表示本节点刚命中规则**） | execute | llm_decide |

### 路由约定

- **`commander` → `local_view`**：`local_view` 若未识别具体子命令，应 **不** 擅自写入最终 `reply` 误导用户；可置 `handled=False` 并清空/忽略无效 `command_kind`，由 `compose` 统一兜底，或后续迭代为回到 `fast_rule`（若采用第二种，需在 Phase 3 节点实现里显式加边，本拓扑默认「local_view 总能落到 compose」）。
- **`fast_rule` 后的条件边**：进入该分流前，**只有 `fast_rule` 应写入 `decision`**。`pre_intent` 只写 `intent_hint`，避免与「规则命中」语义冲突。

## 1.4 GraphDeps 扩展

**目标形态**（与 Phase 2 `contracts` 补齐的 Protocol 对齐）：

```python
from dataclasses import dataclass

@dataclass
class GraphDeps:
    llm: LLMProvider
    image_llm: ImageLLMProvider
    classifier: IntentClassifier
    vault: VaultRepository
    todo: TodoRepository
    sessions: ACPSessionPool
    acl: ACLProvider
```

**Phase 1 落地**：若 `ImageLLMProvider` 等尚未定义，可二选一：**(a)** 仅在 `graph.py` 内用最小 `GraphDeps` + 占位 `None` 与运行时检查；**(b)** 在 `contracts.py` 先加 `Protocol` 空壳，Phase 2 再填方法。避免 Phase 1 为凑类型而硬依赖未实现的适配器。

## 1.5 文件改动

| 文件 | 动作 |
|------|------|
| state.py | 扩展至 **16 键**，注释分组与本文 1.1 一致 |
| graph.py | 注册 10 节点 + 3 条件边 + `compile()` |

## 错误与 END

异常时建议节点将说明写入 `error`，仍进入 `compose` 由后者生成用户可见 `reply`（或专用兜底文案），保证 **任意分支可达 END**，便于与验收「路径可达」一致。若个别节点需短路，须在 Phase 3 节点文档中单独列出。

## 验收

- [ ] `from langgraph_v2.state import ClawBotState` 无错误
- [ ] 所有 `conditional_edges` 分支映射完整、路径可达 **END**
- [ ] LangGraph 无环检查通过
- [ ] 存在图片 / 纯文本 / 斜杠命令 / 规则命中 / LLM 决策 至少各一条冒烟路径（可在 Phase 6 测试文档中落地自动化）
