# LangGraph v2 计划索引

在 Prometheus 写完 -> /start-work 切 Sisyphus 执行。

| 文件 | Phase | 内容 | 状态 |
|------|-------|------|------|
| [01-state-graph.md](01-state-graph.md) | 1 | State 扩展 + Graph 拓扑定型 | ⏳ |
| [02-contracts-llm.md](02-contracts-llm.md) | 2 | Contracts 扩展 + ACP LLM 适配 | ⏳ |
| [03-nodes.md](03-nodes.md) | 3 | 节点实现（normalize ~ llm_decide） | ⏳ |
| [04-adapter-gray.md](04-adapter-gray.md) | 4 | DualPathDispatcher + 灰度开关 | ⏳ |
| [05-pre-intent-local.md](05-pre-intent-local.md) | 5 | pre_intent + local_view + 节奏/拓扑（**主文档**） | ⏳ |
| [05-intent-view.md](05-intent-view.md) | 5 | 早期稿，实施以 05-pre-intent-local 为准 | ⏳ |
| [06-testing.md](06-testing.md) | 6 | 测试 + 回退机制 | ⏳ |

## 不变更清单

| 模块 | 原因 |
|------|------|
| wechat/client.py | 微信接入层独立 |
| scheduler/ | 异步调度器独立进程 |
| prompts/*.md | 模板复用，热更新不变 |
| CoachMixin.* | 写入逻辑原封不动 |
| bot.py 消息循环 | 只加 DualPathDispatcher |
