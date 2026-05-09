# Phase 3：节点实现

## 节点优先级

| 优先级 | 节点 | 复杂度 | 说明 |
|--------|------|--------|------|
| P0 | normalize | 低 | 直接迁移，无外部依赖 |
| P0 | fast_rule | 低 | 规则匹配，纯 Python |
| P0 | compose | 中 | reply 兜底需精细保留原语义 |
| P0 | execute | 高 | 接入所有 CoachMixin — 最大工作量 |
| P1 | commander | 低 | slash 命令路由 |
| P1 | image_router | 低 | 简单条件判断 |
| P2 | describe_img | 中 | 需多模态 LLM（当前 deepseek 不支持） |
| P2 | pre_intent | 中 | 小模型分类 — P3 先 pass-through |
| P2 | local_view | 中 | 本地 markdown 读取 — P3 先 pass-through |
| P2 | llm_decide | 高 | structured output + 降级策略 |

---

## 3.1 normalize

```python
async def normalize(state: ClawBotState) -> dict:
    text = str(state.get("text") or "").strip()
    cmd = ""
    if text.startswith("/"):
        cmd = text[1:].strip()
    return {
        "text": text,
        "command_kind": cmd,
        "msg_trace": uuid.uuid4().hex[:12],
        "handled": False,
        "queue_snapshot": [],
        "wx_out": [],
        "error": "",
    }
```

**源对应**：graph.py 现有 init_context + preprocess 合并为一个节点。

---

## 3.2 image_router

```python
def _route_after_image_router(state: ClawBotState) -> str:
    if state.get("image_base64"):
        return "describe_img"
    return "pre_intent"
```

---

## 3.3 fast_rule

无 LLM 快速规则。源对应 `utils.route_fast.build_fast_unified_decision` + graph.py 现有 fast_route。

```python
async def fast_rule(state: ClawBotState) -> dict:
    text = str(state.get("text") or "").lower()
    # 简单确认
    if text in ("做完了", "好了", "完成了", "done"):
        return {"decision": {"tool": "todo.done_current", "payload": {}, "reply": ""}}
    if text in ("下一个", "next"):
        return {"decision": {"tool": "todo.next", "payload": {}, "reply": ""}}
    # 添加待办
    tasks = _split_tasks(text)
    if tasks:
        return {"decision": {"tool": "todo.merge_new_items", "payload": {"tasks": tasks}, "reply": ""}}
    return {}
```

---

## 3.4 llm_decide

最复杂的节点之一。调用 adapters/acp_llm.py，处理降级。

```python
async def llm_decide(state: ClawBotState, deps: GraphDeps) -> dict:
    prompt = build_unified_decide_prompt(
        text=str(state.get("text") or ""),
        intent_hint=state.get("intent_hint") or {},
        queue_snapshot=list(state.get("queue_snapshot") or []),
    )
    decision = await deps.llm.structured_decide(
        user_id=str(state.get("from_user") or ""),
        text=str(state.get("text") or ""),
        queue_snapshot=list(state.get("queue_snapshot") or []),
    )
    return {
        "decision": {
            "tool": decision.tool,
            "payload": decision.payload,
            "reply": decision.reply,
        }
    }
```

**降级流程**（与 dispatcher.py 一致）：
1. 优先 structured_combined（tool+payload+reply）
2. 失败 -> structured_decision_only + reply 另调
3. 再失败 -> tool=none 兜底

---

## 3.5 execute

全图最重的节点。对应 `DispatcherMixin._apply_unified_decision`（160 行），拆三步：

```python
async def execute(state: ClawBotState, deps: GraphDeps) -> dict:
    decision = state.get("decision") or {}
    tool = str(decision.get("tool") or "none")
    payload = decision.get("payload") or {}

    # tool=none 直接跳过，不调 coach
    if tool == "none":
        return {"handled": False, "tool_result": ""}

    # 1. tool dispatch — 复用现有 TOOL_HANDLERS 注册表
    from handlers.dispatcher import _TOOL_HANDLERS
    handler = _TOOL_HANDLERS.get(tool)
    if handler is None:
        return {"handled": False, "tool_result": ""}

    # 2. 执行 + slow hint（3s 竞速，与旧版一致）
    try:
        handled = await handler_with_slow_hint(...)
    except Exception as e:
        return {"handled": True, "tool_result": str(e)[:500], "error": str(e)}

    # 3. post hooks
    if handled:
        run_post_write_hooks(handler_ref, tool, payload)

    return {"handled": handled, "tool_result": decision.get("reply", "")}
```

**关键原则**：不重写 CoachMixin。所有 tool dispatch 走现有 `_TOOL_HANDLERS` 注册表。

---

## 3.6 compose

reply 拼装 + 兜底。保留旧版 `_sanitize_vague_none_reply` 语义（空指代检测、假执行标记检测）。

```python
async def compose(state: ClawBotState) -> dict:
    reply = str(state.get("tool_result") or state.get("decision", {}).get("reply", "") or "").strip()
    if not reply:
        reply = "我没看懂这条要怎么记。要我把它当作生活记录写进今日日志吗？"
    # 空指代检测
    reply = sanitize_vague_none_reply(reply)
    return {"reply": reply, "wx_out": [reply]}
```

---

## 3.7 各节点数据流

```
normalize ->        text, command_kind, msg_trace
image_router ->     (text 流) | (image 流)
describe_img ->     +decision={tool:"record.add", payload:{text:描述}}
pre_intent ->       +intent_hint
commander ->        (command_kind 非空路由) | (空路由)
fast_rule ->        +decision（命中） | {}（未命中）
llm_decide ->       +decision
execute ->          +tool_result, +handled
compose ->          reply, wx_out
```

---

## 文件改动

| 文件 | 动作 |
|------|------|
| nodes/__init__.py | 新建 |
| nodes/normalize.py | 新建 |
| nodes/image_router.py | 新建 |
| nodes/describe_img.py | 新建（stub） |
| nodes/pre_intent.py | 新建（pass-through stub） |
| nodes/commander.py | 新建 |
| nodes/fast_rule.py | 新建 |
| nodes/local_view.py | 新建（pass-through stub） |
| nodes/llm_decide.py | 新建 |
| nodes/execute.py | 新建 |
| nodes/compose.py | 新建 |
| graph.py | 扩展节点注册 + edges |

## 验收

- [ ] 5 条主链路：记录/待办/提醒/快通道/闲聊
- [ ] execute 正确路由到 _TOOL_HANDLERS
- [ ] compose 兜底文案不低于旧版质量
