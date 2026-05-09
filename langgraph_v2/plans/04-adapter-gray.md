# Phase 4：适配层 + 灰度开关

## 4.1 config.yaml 新增

```yaml
graph:
  enabled: false            # 总开关
  rollout_rate: 0.0         # 0.0 ~ 1.0
  rollout_users: []         # 白名单用户优先
  shadow_mode: false        # 同时跑 graph 但不发微信
  shadow_log_dir: ""        # 对比日志目录
```

## 4.2 DualPathDispatcher

bot.py 消息入口加一层，按策略路由到旧 Handler 或新 LangGraph。

```python
# langgraph_v2/dispatcher.py
class DualPathDispatcher:
    """旧 Handler + 新 LangGraph 双通道"""

    def __init__(self, handler: Handler, graph, graph_deps: GraphDeps, config: dict):
        self._legacy = handler
        self._graph = graph
        self._deps = graph_deps
        self._cfg = config.get("graph", {})

    def _should_use_graph(self, user_id: str) -> bool:
        cfg = self._cfg
        if not cfg.get("enabled"):
            return False
        if user_id in (cfg.get("rollout_users") or []):
            return True
        return random.random() < float(cfg.get("rollout_rate", 0))

    async def dispatch(
        self,
        text: str = "",
        image_base64: str = "",
        image_mime: str = "",
        from_user: str = "",
        context_token: str = "",
    ) -> tuple[str, bool]:
        """返回 (reply, handled)。"""
        if not self._should_use_graph(from_user):
            return await self._legacy_path(text, from_user, context_token)

        state = ClawBotState(
            text=text,
            image_base64=image_base64,
            image_mime=image_mime,
            from_user=from_user,
            context_token=context_token,
        )
        result = await self._graph.ainvoke(
            state,
            config={"configurable": {"deps": self._deps}},
        )
        reply = result.get("reply", "")
        wx_out = result.get("wx_out", [])
        return reply, bool(wx_out)

    async def _legacy_path(self, text, from_user, context_token):
        """走旧 Handler 路径"""
        ...
```

## 4.3 Shadow Mode

`shadow_mode=true` 时：旧链路正常返回用户，同时异步跑 graph 对比。对比日志写到 shadow_log_dir。

```
如果 enabled=true 且 shadow_mode=true：
  legacy -> 发微信（主路径）
  graph  -> 异步跑 -> 写对比日志
```

## 4.4 三步上线

| 步骤 | 配置 | 描述 |
|------|------|------|
| 1 | shadow_mode=true, rollout_rate=0 | 只打日志，不影响用户 |
| 2 | shadow_mode=false, rollout_rate=0.1 | 10% 走新链路 |
| 3 | shadow_mode=false, rollout_rate=1.0 | 全量。7 天后删旧代码 |

## 4.5 文件改动

| 文件 | 动作 |
|------|------|
| langgraph_v2/dispatcher.py | 新建，DualPathDispatcher |
| bot.py | 入口处加 DualPathDispatcher 调用 |
| config.py | 新增 graph 配置项校验 |
| config.yaml | 新增 graph 配置段 |

## 验收

- [ ] rollout_rate=0 时走旧链路（零影响）
- [ ] rollout_rate=1.0 且白名单用户走新链路
- [ ] shadow_mode 对比日志正确
