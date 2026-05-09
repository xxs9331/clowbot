# Phase 6：测试 + 回退机制

## 6.1 单元测试

| 测试对象 | 测试内容 | 文件 |
|----------|----------|------|
| state.py | TypedDict 字段完整性、可选字段默认值 | tests/test_state.py |
| graph.py | 每条条件边路径可达性、无环校验 | tests/test_graph.py |
| fast_rule | 规则匹配正确性 + 边界 case | tests/test_fast_rule.py |
| compose | 兜底文案覆盖、"空指代"检测 | tests/test_compose.py |
| services.py | 每个 tool 正向/异常/空输入 | tests/test_services.py |
| llm_decide | mock LLM 返回各种 decision 形态 | tests/test_llm_decide.py |

## 6.2 集成测试

用 mock ACP + mock Vault + mock Todo 构造假对话流，验证 5 条主链路：

### 链路 1：生活记录
```
输入：体重 72.3kg
预期路径：normalize -> image_router -> pre_intent -> commander -> fast_rule -> llm_decide -> execute -> compose
预期 decision：{tool: "record.add", payload: {text: "体重 72.3kg", category: "身体"}}
预期写入：vault.append_record 被调用
```

### 链路 2：待办添加
```
输入：添加待办：买牛奶，写报告
预期路径：normalize -> ... -> fast_rule（命中 _split_tasks） -> execute -> compose
预期 decision：{tool: "todo.merge_new_items", payload: {tasks: ["买牛奶", "写报告"]}}
```

### 链路 3：提醒
```
输入：明天 8 点提醒我开会
预期路径：normalize -> ... -> llm_decide -> execute -> compose
预期 decision：{tool: "remind.add", payload: {text: "开会", hhmm: "08:00"}}
```

### 链路 4：快通道
```
输入：做完了
预期路径：normalize -> ... -> fast_rule（命中） -> execute -> compose
预期 decision：{tool: "todo.done_current"}
```

### 链路 5：闲聊
```
输入：今天好累
预期路径：normalize -> ... -> llm_decide -> execute -> compose
预期 decision：{tool: "none"}
预期：reply 不为空、无"空指代"
```

## 6.3 回退机制

### 秒级回退
```yaml
# config.yaml
graph:
  enabled: false  # 改这一行，重启即回旧链路
```

### 回退保证
- 旧 Handler 代码（handlers/base.py, handlers/dispatcher.py）保留至少 3 个月
- DualPathDispatcher 的 `enabled=false` 时完全绕过 LangGraph
- Shadow mode 对比日志保留 30 天

### 回退触发条件
- LangGraph 链路错误率 > 1%
- compare 日志显示新/旧输出不一致率 > 5%
- 用户反馈异常

## 6.4 监控指标

```python
# 在 DualPathDispatcher 里埋点
metrics = {
    "graph_calls": 0,          # 走 graph 的次数
    "legacy_calls": 0,         # 走旧链路的次数
    "graph_errors": 0,         # graph 异常次数
    "shadow_mismatches": 0,    # shadow 对比不一致次数
    "avg_graph_ms": 0,         # graph 平均耗时
    "avg_legacy_ms": 0,        # 旧链路平均耗时
}
```

## 文件改动

| 文件 | 动作 |
|------|------|
| tests/__init__.py | 新建（如不存在） |
| tests/test_state.py | 新建 |
| tests/test_graph.py | 新建 |
| tests/test_fast_rule.py | 新建 |
| tests/test_compose.py | 新建 |
| tests/test_services.py | 新建 |
| tests/test_llm_decide.py | 新建 |
| tests/test_integration.py | 新建（5 条主链路） |

## 验收

- [ ] `python -m pytest tests/` 全部通过
- [ ] 5 条集成测试链路输出与旧 Handler 一致
- [ ] graph.enabled=false 时零影响
