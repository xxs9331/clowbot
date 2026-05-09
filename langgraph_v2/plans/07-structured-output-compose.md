# Phase 7：结构化输出增强 + compose 兜底修复

## 背景

- `prompt_structured` 依赖 **纯文本 + 本地 `_extract_first_json_object`（含 regex/截断）**，解析失败时整条结构化链路降级，可靠性有限。
- `compose` 在 **`tool == "none"`** 且 **`decision_reply` 为空**、且 **`local_reply` / `tool_result` 皆空** 时，使用硬编码兜底：「我没看懂这条要怎么记…」。用户在不具备「误解」语义的场景下也会看到该句（含 **合法 `none` + 空 reply**）。
- **根因假设**（须用日志证实）：有人怀疑 LangGraph state 合并导致 `decision` 丢失，使 `compose` 读到空 `decision` → `tool` 退化为 `"none"`。当前 `execute` 仅返回 `handled` / `tool_result`，**未覆盖 `decision`**；默认合并语义下 **`decision` 通常应保留**。实施前应用 **`msg_trace` 串联的 flow 日志** 确认是 **真丢字段** 还是 **`none`+空 `reply` 的合法路径**。

## 目标

| 目标 | 说明 |
|------|------|
| 止血 | 异常/空回复路径下避免误导性「我没看懂」全文（或改为中性短句，产品定稿） |
| 可观测 | `compose` / `llm_decide` 打 **符合 `log_flow_event` 契约** 的诊断日志 |
| 解析 | `_extract_first_json_object` 在 `json.loads` 失败后 **`json_repair` 兜底**，并保留严格类型约束（只收 `dict`） |
| 源头 | 结构化请求优先走 **`response_format/json schema`**（若网关支持），不支持时退化为严格 JSON 模板 |
| 稳定性 | 增加「解析校验失败 -> 反馈错误 -> 有限重试」闭环，避免一次失败直接降级 |

## 改动清单

| 文件 | 改动 | 风险 |
|------|------|------|
| `langgraph_v2/nodes/compose.py` | 兜底文案替换；**窄条件**诊断日志 | 低（文案属产品决策） |
| `langgraph_v2/nodes/llm_decide.py` | `return` 前 `log_flow_event` | 低 |
| `acp/opencode_client.py` | `_build_prompt_params` 支持 `extra`；**`prompt()`** 传入结构化专用 extra；`_extract_first_json_object` 内 `json_repair` | 低～中（JSON Mode 依赖上游） |
| `requirements.txt` | `json-repair>=0.41.0`（以 PyPI 为准） | 极低 |

---

## Part 1 — compose 兜底与诊断

### 1.1 兜底文案（产品确认后落地）

**文件**：`langgraph_v2/nodes/compose.py`（`tool == "none"` 分支）

- **现状**：`decision_reply or "我没看懂这条要怎么记。要我把它当作生活记录写进今日日志吗？"`，再经 `_sanitize_vague_none_reply`。
- **可选替换**：例如 `"收到～"` 等**中性短句**。注意：这会作用于 **所有**「none + 空 `decision_reply` + 无 `local_reply`/`tool_result`」而不仅是「state 异常」。

### 1.2 `log_flow_event` 正确用法

**签名**（`utils/flow_log.py`）：

```python
log_flow_event(
    *,
    stage: str,
    route: str,
    user_text: str = "",
    from_user: str = "",
    session_id: str = "",
    extra: dict | None = None,
)
```

- **禁止**位置参数第一参当 stage/route 混用。
- **`ClawBotState`** 无 `session_id` / `msg_id`；用 **`msg_trace`**、`from_user`、`context_token` 等放入 **`extra`**。若需 ACP session，由调用方从 `GraphDeps.sessions` 等处取，一般不强行塞进 state。

### 1.3 compose 诊断条件（避免刷屏）

- **不推荐**：`if not decision or not decision.get("reply")` —— 合法 **`none` + 空 reply** 也会频繁命中。
- **推荐**：仅在 **`decision` 缺失关键结构** 时记录，例如：`not decision`（空 mapping）或 **`"tool" not in decision`**；或与 **`msg_trace`** 关联的采样。具体阈值可再调。

**示例**：

```python
if not decision:
    log_flow_event(
        stage="graph",
        route="compose_decision_missing",
        user_text=str(state.get("text") or "")[:300],
        from_user=str(state.get("from_user") or ""),
        extra={
            "msg_trace": state.get("msg_trace"),
            "tool_result_preview": (state.get("tool_result") or "")[:200],
            "reply_preview": (state.get("reply") or "")[:200],
            "full_state_keys": sorted(list(state.keys())),
            "raw_decision_repr": repr(state.get("decision")),
        },
    )
```

### 1.4 llm_decide 对照日志

在 **`return {"decision": ...}` 之前**，记录 **`d.tool` / `d.reply` 长度** 等（变量名为 **`d`**，不是 `decision`）：

```python
log_flow_event(
    stage="graph",
    route="llm_decide_output",
    from_user=str(state.get("from_user") or ""),
    extra={
        "msg_trace": state.get("msg_trace"),
        "tool": d.tool,
        "reply_len": len(str(d.reply or "")),
    },
)
```

---

## Phase 7b：`structured_decide` 二次调用诊断 + VS Code Debug

### 背景

- Phase 7 已加 compose 兜底与 `llm_decide_output` 等日志，可观测性提升。
- 新现象：`v2_unified_reply`（`_generate_reply`）在 **`tool=none`** 且 **模型 JSON 里 `reply` 本应非空** 时仍被触发。
- 疑点：`prompt_structured` → `d` 里 `reply` 仍有内容，但 **`_coalesce_unified_decision` 之后** `reply` 变空 → 命中「空则补 `_generate_reply`」分支。须在 **coalesce 前后** 对照定位丢失环节（`_project_obj_to_schema_properties` / `_validate_schema_obj` / coalesce 自身）。

### 改动清单（已实现）

| 文件 | 改动 | 风险 |
|------|------|------|
| `langgraph_v2/adapters/acp_llm.py` | `structured_decide` **combined 路径**：`_coalesce_unified_decision(d)` 之后，若 **`tool=="none"`** 且 **`d` 中 `reply` 非空** 但 **coalesce 后 `reply` 为空**，则 `log_flow_event(route="structured_reply_stripped", ...)`（`extra`：`msg_trace` 前 200 字、`raw_reply_len`、`coalesced_reply_len`、`d_keys`）。**不刷屏**：仅上述窄条件。 | 极低 |
| `.vscode/launch.json` | **Attach**：`127.0.0.1:5678`，`pathMappings` 以 **`${workspaceFolder}`** 为根（当前仓库根即 `.clawbot` 时勿再套一层 `.clawbot`）。**Launch**：`tests/test_langgraph_v2_acp_llm.py`（仓库内无 `test_compose_decision.py` 时用现有用例；若日后新增 compose 专测可改 `program`）。 | 极低 |
| `langgraph_v2/nodes/compose.py` | `compose_decision_missing` 的 `extra` 增加 **`full_state_keys`**、`raw_decision_repr`（区分 `{}` 与 `None`）。 | 极低 |

### 验收（7b）

- [ ] 复现「`none` + 模型侧长 reply」一轮后，若 coalesce 吃掉了 reply，flow 中出现 **`route=structured_reply_stripped`**，且 `d_keys` / `raw_reply_len` 与 ACP trace 可对照。
- [ ] 若 **不出现** 该 route：说明 `d.get("reply")` 在进 coalesce 前已空 → 继续查 `prompt_structured` 内 `_finalize_out` / `_merge_structured_final_reply` 等（见 `opencode_client.py`）。
- [ ] VS Code：进程带 debugpy 后可 **Attach**；或直接 **Launch** 上述测试文件通过断点跟 `acp_llm` / `opencode_client`。

**建议断点**（手工 Debug）：`acp_llm.py` coalesce 前后；`opencode_client.py` 中 `_project_obj_to_schema_properties` / `_validate_schema_obj` 返回处。

---

## Part 2 — 结构化解析与 JSON Mode

### 2.1 `json_repair`

**文件**：`requirements.txt`  
新增：`json-repair>=0.41.0`（版本以项目锁定策略为准）。

**文件**：`acp/opencode_client.py` — **`_extract_first_json_object`**

- 在 **`json.loads` 失败** 之后、**`JSONDecoder().raw_decode` 循环**之前，尝试：

```python
try:
    import json_repair
    repaired = json_repair.repair_json(s, return_objects=True)
    if isinstance(repaired, dict):
        return repaired
except Exception:
    pass
```

- `import` 放在函数内，避免未安装时模块 import 失败。
- 若 `repair_json` 返回 **list**，结构化路径需 **dict** 时应丢弃或取首元素，避免误当 decision。
- 修复后仍要做字段校验：至少包含预期键（如 `tool`），否则视为失败并进入重试。

### 2.2 JSON Mode — 必须走 `prompt()` / `prompt_structured` 链路

**事实**：`prompt_structured` 通过 **`await self.prompt(session_id, structured_prompt, trace_tag=...)`** 发请求，**不是**在 `prompt_with_image` 的 `_send` 处改参。

**步骤**：

1. **`_build_prompt_params(self, session_id, prompt_parts, extra: dict | None = None)`**  
   - 在 `maxTokens` 之后 `params.update(extra or {})`。

2. **`prompt(..., prompt_extra: dict | None = None)`**（参数名可自定）  
   - `_build_prompt_params(session_id, parts, extra=prompt_extra)`。

3. **`prompt_structured`** 内循环调用 `prompt` 时：若希望启用 JSON Mode，传  
   `prompt_extra={"response_format": {"type": "json_object"}}`  
   （或项目与 OpenCode 约定字段名；需 **实机验证** 是否透传）。
4. 若网关支持 schema，优先改成 schema 约束（比 `json_object` 更稳）：

```python
prompt_extra={
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "decision_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "reply": {"type": ["string", "null"]},
                },
                "required": ["tool", "reply"],
                "additionalProperties": True,
            },
        },
    }
}
```

5. **勿**把 JSON Mode 绑到 **`prompt_with_image`** 的 `_send`，除非也显式给多模态结构化留口子。

**门控**：仅当 **`trace_tag` 含 `structured`**（或仅 `prompt_structured` 调用链）时注入，避免普通闲聊 `prompt` 被强制 JSON。

**风险**：网关/模型不支持时可能报错或忽略；建议 **先日志确认参数到达**，必要时 **配置开关** 回滚。

### 2.3 严格模板与失败重试

- `structured_prompt` 必须包含硬约束：**只输出 JSON、禁止 markdown/解释文字、字段缺失填 `null` 不猜测**。
- 输出格式与任务描述分段书写，避免指令混淆。
- 新增有限重试策略（建议 1~2 次）：
  1. 首次失败：返回校验错误摘要（缺字段/类型错误/非 JSON）。
  2. 要求“仅修复格式，不改语义”后重试。
  3. 再失败则走当前降级路径并记录 `prompt_structured_fail`。

---

## 验收

- [ ] `pip install -r requirements.txt` 后 `import json_repair` 可用，结构化失败率下降（对比 `prompt_structured_fail` 日志或抽样）。
- [ ] `compose` / `llm_decide` 日志 **`stage`/`route`/`extra` 合法**，无异常栈。
- [ ] 诊断日志 **无刷屏**（或仅采样/仅异常结构）。
- [ ] JSON Mode：至少一条真实 `prompt_structured` 请求验证 **无 4xx**、解析成功率变化可接受。
- [ ] 若启用 schema：字段完整率（如 `tool`、`reply`）高于 `json_object` 或纯文本模式。
- [ ] 失败重试链路生效：解析失败后可观测到重试且无死循环。
- [ ] 兜底文案与 **`_sanitize_vague_none_reply`** 行为符合产品预期（单测或手工）。

## 参考

- `utils/flow_log.py` — `log_flow_event`
- `langgraph_v2/state.py` — `msg_trace` 等字段
- `acp/opencode_client.py` — `prompt`、`prompt_structured`、`_extract_first_json_object`
