# Phase 5：pre_intent + local_view 补齐

Phase 3 中这两个节点做了 pass-through。Phase 5 补齐。

---

## 5.1 pre_intent

**职责**：flash 模型快速意图分类，产出 intent_hint，注入 llm_decide 的 prompt context。

**实现**：
- 复用 `utils.intent.detect_intent` 的 prompt 模板和分类逻辑
- 通过 `IntentClassifier` Protocol 调用（适配层封 flash LLM）

```python
# adapters/classifier_adapter.py
class FlashIntentClassifier:
    """用 flash 模型做轻量意图分类"""

    def __init__(self, acp: OpenCodeACP, session_id: str):
        self._acp = acp
        self._session = session_id

    async def classify(self, *, text: str) -> dict[str, Any]:
        # 复用 utils/intent.py 的 prompt
        prompt = build_intent_prompt(text)
        result = await self._acp.prompt(self._session, prompt)
        return parse_intent_result(result)
```

```python
# nodes/pre_intent.py
async def pre_intent(state: ClawBotState, deps: GraphDeps) -> dict:
    text = str(state.get("text") or "")
    if not text:
        return {}
    try:
        hint = await deps.classifier.classify(text=text)
        return {"intent_hint": hint}
    except Exception:
        return {"intent_hint": {}}
```

**分类标签**（与 `utils/intent.py` 一致）：
- INTENT_RECORD — 生活记录
- INTENT_TODO — 待办操作
- INTENT_REMIND — 提醒设置
- INTENT_CHAT — 闲聊/询问

---

## 5.2 local_view

**职责**：本地 Obsidian markdown 读取，直接返回不触发 LLM 决策。

**触发条件**：command_kind 非空（slash 命令）或查看类关键词匹配。

**实现**：复用 `LocalViewMixin` 的文件读取逻辑。

```python
# nodes/local_view.py
async def local_view(state: ClawBotState, deps: GraphDeps) -> dict:
    cmd = str(state.get("command_kind") or "")
    view_map = {
        "待办": "todo",
        "提醒": "remind",
        "记录": "record",
        "时间轴": "timeline",
    }
    if cmd in view_map:
        body = await deps.vault.read_view(kind=view_map[cmd])
        return {"handled": True, "reply": body}
    return {}
```

---

## 5.3 describe_img

当前 deepseek 不支持多模态。先保留 stub，后续恢复多模态能力时替换。

```python
# nodes/describe_img.py（stub）
async def describe_img(state: ClawBotState, deps: GraphDeps) -> dict:
    """stub：当前无多模态模型"""
    return {
        "text": "[图片]（暂不支持多模态分析）",
        "decision": {"tool": "none", "payload": {}, "reply": "收到图片，但当前暂不支持识别图片内容。"},
    }
```

---

## 文件改动

| 文件 | 动作 |
|------|------|
| adapters/classifier_adapter.py | 新建，FlashIntentClassifier |
| nodes/pre_intent.py | 从 stub 改为真正实现 |
| nodes/local_view.py | 从 stub 改为真正实现 |
| nodes/describe_img.py | interim stub（待多模态模型就绪） |

## 验收

- [ ] pre_intent 正确产出 intent_hint 并注入 llm_decide prompt
- [ ] /待办 /提醒 /记录 /时间轴 本地读取正确
- [ ] 图片消息不崩溃（stub reply）
