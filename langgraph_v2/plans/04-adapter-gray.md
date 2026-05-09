# Phase 4：适配层 + 灰度开关

## 4.1 config.yaml 新增

```yaml
graph:
  enabled: false            # 总开关；false 时不跑 LangGraph（含 shadow）
  rollout_rate: 0.0         # 0.0 ~ 1.0，见 4.4 与 shadow 的语义
  rollout_users: []         # 白名单：在 enabled=true 时强制参与「新链路抽样」（见 4.4）
  shadow_mode: false        # true：用户可见回复永远走旧链路，另异步跑图写对比日志
  shadow_log_dir: ""        # shadow 对比日志目录；shadow_mode=true 时建议非空
  # 可选：按 user_id 稳定分桶（实现时二选一，不必都配）
  # rollout_stable_bucket: false   # true 时用 hash(user_id) 分桶替代 random
```

**`config.py` / `collect_config_errors` 建议校验**：

- `graph` 段若存在：`enabled` / `shadow_mode` 为 bool；`rollout_rate` 在 \[0, 1\]；`rollout_users` 为列表。
- `shadow_mode=true` 且 `enabled=true` 时：`shadow_log_dir` 非空（或明确允许只打 stdout）。
- `enabled=false` 时：`shadow_mode` 应为 false（否则无意义），或文档约定为「静默忽略 shadow」。

---

## 4.2 与 `Handler.handle` 的集成范围（必读）

现网入口是 `handlers/base.py::handle(self, msg)`：内含图片分支、agent 权限、timeline/checkin、slash 别名与多子命令、再进统一决策等。**不能**简单理解为在 `bot.py` 里用 `(reply, handled)` 包一层就等价替换。

**建议 MVP 边界**：

1. **仍走旧 Handler 前置逻辑**：`msg_type == "image"`（`_handle_image`）、`_maybe_handle_agent_permission_reply`、`_timeline_preprocess`、以及与你尚未迁到图内的 slash 行为（若与 `graph.local_view` 不一致）。
2. **在满足「与 LangGraph 文本主路径可比」的分支上**再调用双路径：例如已进入与现 `DispatcherMixin` 决策等价的文本处理，或单独抽一层 `handle_text_core(...)` 由 DualPath 调度。
3. **图片**：首版可约定 **仅文本** 进 `ainvoke`；带图消息继续旧链路，或在图中已有 `describe_img` 且与微信解码对齐后再放开。

否则灰度对比不公平，或行为静默分叉。

---

## 4.3 DualPathDispatcher（示例与现仓库对齐）

模块路径：`langgraph_v2/dispatcher.py`（与 `handlers/dispatcher.py` 不同包，import 时注意勿混）。

**依赖注入**：当前 `build_chat_graph(deps)` 在 **闭包**中捕获 `GraphDeps`（见 `graph.py`），`tests/test_langgraph_v2.py` 的 `ainvoke` **只传入 state 的 dict**。Phase 4 **默认不引入** `RunnableConfig["configurable"]["deps"]`；若将来要每请求换 deps，再单开重构。

**State**：`ClawBotState` 为 `TypedDict`，**无** `ClawBotState(...)` 构造函数，应使用普通 `dict`。

```python
# langgraph_v2/dispatcher.py（示意）
class DualPathDispatcher:
    """旧 Handler + 新 LangGraph 双通道（在 handle 内合适锚点调用）。"""

    def __init__(self, handler: Handler, graph, config: dict):
        self._legacy = handler
        self._graph = graph  # 已由 build_chat_graph(deps) 编译，deps 已闭包捕获
        self._cfg = config.get("graph") or {}

    def _in_rollout_users(self, user_id: str) -> bool:
        users = self._cfg.get("rollout_users") or []
        return user_id in users

    def _should_run_graph_for_user_reply(self, user_id: str) -> bool:
        """是否用图结果作为对用户回复（shadow 下恒为 False，见 4.4）。"""
        cfg = self._cfg
        if not cfg.get("enabled"):
            return False
        if cfg.get("shadow_mode"):
            return False
        if self._in_rollout_users(user_id):
            return True
        rate = float(cfg.get("rollout_rate", 0) or 0)
        # 可选：stable bucket — hash(user_id) % 10000 < rate * 10000
        import random
        return random.random() < rate

    def _should_run_graph_shadow(self, user_id: str) -> bool:
        """shadow 下是否异步跑图写日志（enabled 且 shadow，且命中抽样/白名单策略）。"""
        cfg = self._cfg
        if not (cfg.get("enabled") and cfg.get("shadow_mode")):
            return False
        if self._in_rollout_users(user_id):
            return True
        rate = float(cfg.get("rollout_rate", 0) or 0)
        import random
        return random.random() < rate

    async def ainvoke_graph(
        self,
        *,
        text: str = "",
        image_base64: str = "",
        image_mime: str = "",
        from_user: str = "",
        context_token: str = "",
    ) -> dict:
        payload: ClawBotState = {
            "text": text,
            "image_base64": image_base64,
            "image_mime": image_mime,
            "from_user": from_user,
            "context_token": context_token,
        }
        return await self._graph.ainvoke(payload)

    async def _legacy_path(self, msg: dict):
        """委托现有 handle 或未抽核前的等价调用；签名与 legacy 一致。"""
        await self._legacy.handle(msg)
```

**返回值**：现网没有统一的 `(reply, handled)`；双路径更适合在 **Handler 内部**发完微信或与 `wx` 回调对齐。若需返回值，仅用于单测或 shadow 对比，勿强行与全链路 `handled` 语义绑定。

---

## 4.4 Shadow Mode 与 rollout 语义

| 条件 | 用户可见回复 | LangGraph |
|------|----------------|-----------|
| `enabled=false` | 旧链路 | 不跑 |
| `enabled=true`, `shadow_mode=true` | **始终旧链路** | 若 `_should_run_graph_shadow` 为 true：**异步** `ainvoke`，结果写入 `shadow_log_dir` |
| `enabled=true`, `shadow_mode=false` | `_should_run_graph_for_user_reply` 为 true 时用图结果发消息 | 同步跑图 |

**白名单 `rollout_users`**：

- **非 shadow**：在该列表中的用户 **强制** 走新链路（用户可见）。
- **shadow**：在该列表中的用户 **强制** 参与异步跑图写日志；用户可见仍为旧链路。

**`rollout_rate=0`（非 shadow）**：非白名单用户 **不走** 新链路（用户可见），即「零流量」。

---

## 4.5 Shadow 运维注意

- 异步任务 **`asyncio.create_task`**：内层异常捕获并记日志，**不**影响主链路 `handle`。
- **并发与成本**：shadow 会双倍调用 LLM，需限制并发或采样（可通过 `rollout_rate` 控制 shadow 样本比例）。
- **日志与隐私**：`shadow_log_dir` 中是否落全文 `text` / `wx_out` 需符合你的留存策略；可只存 `msg_trace` + 摘要。

---

## 4.6 三步上线（示例）

| 步骤 | 配置 | 描述 |
|------|------|------|
| 1 | `enabled=true`, `shadow_mode=true`, `rollout_rate=0.05` | 用户全走旧链路；约 5%（+ 白名单）异步对比日志 |
| 2 | `shadow_mode=false`, `rollout_rate=0.1` | 约 10% 用户可见走新链路 |
| 3 | `rollout_rate=1.0`, `shadow_mode=false` | 全量新链路（白名单在步骤 1～2 已可提前验证） |

---

## 4.7 文件改动

| 文件 | 动作 |
|------|------|
| `langgraph_v2/dispatcher.py` | 新建，`DualPathDispatcher`（或等价命名） |
| `handlers/base.py`（或抽出的核心函数） | 在约定锚点调用双路径；**非**仅在 `bot.py` 包一层 |
| `bot.py` | 若 Handler 构造需传入已编译的 `graph`，可在此 `build_chat_graph(GraphDeps(...))` 后注入 |
| `config.py` | `collect_config_errors` 增加 `graph` 段校验 |
| `config.example.yaml` / `config.yaml` | 增加 `graph` 示例段 |

---

## 验收

- [ ] `enabled=false`：不调用 `ainvoke`（或无任何图副作用）
- [ ] `enabled=true`, `shadow_mode=true`：用户侧行为与仅旧链路一致；命中样本写入 `shadow_log_dir` 且异步失败不影响发消息
- [ ] `rollout_rate=0` 且非白名单：`shadow_mode=false` 时用户可见 **不** 走图
- [ ] `rollout_users` 在非 shadow 下 **强制** 走图；在 shadow 下 **强制** 参与异步对比
- [ ] `ainvoke` 入参为 **dict**，与 `test_langgraph_v2.py` 一致；图由 `build_chat_graph(deps)` 预先编译
