"""小模型意图分类 — 规则未命中时的兜底层。

设计要点：
- 复用现有 OpenCode ACP 进程，仅 create_session 多开一个 sessionId（不开新服务）
- session 与主待办采访会话隔离，不污染上下文
- 严格 JSON 输出契约：{intent, slots, confidence}
- 解析失败 / 超时 / 异常 → 返回 None，让上层走原 unified 兜底（绝不抛异常打断主流程）

输出 intent 枚举（与 utils/tool_names 对齐）：
    todo_add | todo_done | todo_next | todo_not_done | todo_skip | todo_abandon | todo_reorder
    remind_add | record_add | query_todo | query_remind | none
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from utils.flow_log import log_flow_event

from config import PACKAGE_ROOT

INTENT_LABELS = (
    "todo_add",
    "todo_done",
    "todo_next",
    "todo_not_done",
    "todo_skip",
    "todo_abandon",
    "todo_reorder",
    "remind_add",
    "record_add",
    "query_todo",
    "query_remind",
    "none",
)

# 一个会话只建一次 session_id，缓存到 acp 实例上避免每次重建
_INTENT_SESSION_ATTR = "_intent_classify_session_id"
_INTENT_SESSION_LOCK_ATTR = "_intent_classify_session_lock"
_INTENT_SESSION_PRIMED_ATTR = "_intent_classify_session_primed"

PROMPTS_DIR = PACKAGE_ROOT / "prompts"
_INTENT_CLASSIFY_FILE = "intent_classify.md"

_DEFAULT_INTENT_CLASSIFY = """你是中文意图分类器。请把用户消息分到下列意图之一，并抽取槽位。
只输出一个紧凑 JSON 对象，不要任何解释、不要 markdown、不要代码块。

intent ∈ {todo_add | todo_done | todo_next | todo_not_done | todo_skip | todo_abandon | todo_reorder | remind_add | record_add | query_todo | query_remind | none}
slots 字段：text / hhmm / template_name / category / event_date（按需填，缺省可省略）
confidence ∈ [0,1]，对自己判断的把握；倒装、纠错、模糊句把信心降低。

判定要点：
1) 设/加提醒、叫我、别忘了 + 具体时间 → remind_add，slots.hhmm 为 HH:MM
2) 已发生事件（体重、跑步、吃药、快递、读书） → record_add，slots.category ∈ 身体/运动/阅读/事务
3) 添加 X 待办 / 加入 X 模板 / 导入 X 流程模板 → todo_add；若 X 含"模板"二字则 slots.template_name=X，否则 slots.text=X
4) 当前待办相关：做完了/搞定 → todo_done；下一个/接下来做啥 → todo_next；做不动/等会再做 → todo_not_done；先跳过 → todo_skip；不做了/放弃 → todo_abandon；按这个顺序/重排 → todo_reorder
5) 看待办/今日待办 → query_todo；看提醒 → query_remind；查/找「最近几条记忆或记录」等只读浏览 → none（confidence 可 0.35～0.55：与写记录 record_add 区分，入口会读今日日记）
6) 不确定 → none，confidence ≤ 0.4
7) 续写/代指句（如「她也改签了」「同上」「跟刚才一样」）若无明确日期，intent 仍可为 record_add，但 confidence 应 ≤0.55，让上层 unified 结合上下文决定 event_date

重要约束：
- template_name 不允许是泛词（如"模板""待办模板""流程模板"），否则置空并降低 confidence
- 倒装/纠错句（"不是上山是下山流程模板"）应识别为 todo_add，slots.template_name=下山流程模板
- 含引用/转述触发词（如"回「记一下」即可""你回记一下"）且语义是澄清时，应判 none，不得判 todo_add

示例：
输入：添加上山模板待办 → {"intent":"todo_add","slots":{"template_name":"上山模板"},"confidence":0.92}
输入：不是上山是下山流程模板 → {"intent":"todo_add","slots":{"template_name":"下山流程模板"},"confidence":0.85}
输入：叫我九点半喝水 → {"intent":"remind_add","slots":{"hhmm":"21:30","text":"喝水"},"confidence":0.9}
输入：体重 78kg → {"intent":"record_add","slots":{"text":"体重 78kg","category":"身体"},"confidence":0.92}
输入：先跳过这个 → {"intent":"todo_skip","slots":{},"confidence":0.85}
输入：今天有什么待办 → {"intent":"query_todo","slots":{},"confidence":0.9}
输入：找一下最近的三条记忆 → {"intent":"none","slots":{},"confidence":0.45}
输入：我没看懂这条要怎么记。要我把它当作生活记录写进今日日记吗？回「记一下」即可。 → {"intent":"none","slots":{},"confidence":0.25}
输入：好的 → {"intent":"none","slots":{},"confidence":0.3}

---

按你已加载的分类规则执行本轮判断。
仅输出紧凑 JSON：{"intent":"...","slots":{},"confidence":0.0}

{context_block}用户消息: {user_text}
JSON:"""


def _load_intent_classify_prompts() -> tuple[str, str]:
    """从 intent_classify.md 读取，按 --- 拆分返回 (prime, turn)。热更新。"""
    path = PROMPTS_DIR / _INTENT_CLASSIFY_FILE
    try:
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            parts = content.split("\n---\n", 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    except OSError:
        pass
    parts = _DEFAULT_INTENT_CLASSIFY.split("\n---\n", 1)
    return parts[0].strip(), parts[1].strip()


def _build_prime_prompt() -> str:
    prime, _ = _load_intent_classify_prompts()
    return prime


def _build_turn_prompt(
    text: str,
    queue_state: dict | None,
    memory_context: str | None = None,
) -> str:
    """分类回合短 prompt：避免每轮重复注入长规则。"""
    queue = queue_state or {"active": False}
    mem = (memory_context or "").strip()
    mem_block = f"\n{mem}\n" if mem else ""
    context_block = f"队列状态: {json.dumps(queue, ensure_ascii=False)}\n{mem_block}"
    _, turn = _load_intent_classify_prompts()
    return turn.replace("{context_block}", context_block).replace("{user_text}", text)


def _build_prompt(
    text: str,
    queue_state: dict | None,
    memory_context: str | None = None,
) -> str:
    """兼容旧测试：返回 prime+turn 的完整提示。"""
    return f"{_build_prime_prompt()}\n\n{_build_turn_prompt(text, queue_state, memory_context)}"


def _extract_json(reply: str) -> dict | None:
    if not reply:
        return None
    s = reply.strip()
    # 直解
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 兜底：找首个 {...}
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _normalize(obj: dict) -> dict | None:
    intent = str(obj.get("intent") or "").strip().lower()
    if intent not in INTENT_LABELS:
        return None
    slots = obj.get("slots") if isinstance(obj.get("slots"), dict) else {}
    slots = {k: str(v).strip() for k, v in slots.items() if str(v).strip()}
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return {"intent": intent, "slots": slots, "confidence": conf}


async def _ensure_session(acp: Any, model: str) -> str:
    """惰性创建/复用专用 session（同 ACP 进程内多开一个 sessionId）。"""
    if not hasattr(acp, _INTENT_SESSION_LOCK_ATTR):
        setattr(acp, _INTENT_SESSION_LOCK_ATTR, asyncio.Lock())
    lock: asyncio.Lock = getattr(acp, _INTENT_SESSION_LOCK_ATTR)
    async with lock:
        sid = getattr(acp, _INTENT_SESSION_ATTR, "") or ""
        if not sid:
            sid = await acp.create_session(model)
            setattr(acp, _INTENT_SESSION_ATTR, sid)
            setattr(acp, _INTENT_SESSION_PRIMED_ATTR, False)

        primed = bool(getattr(acp, _INTENT_SESSION_PRIMED_ATTR, False))
        if not primed:
            await acp.prompt(sid, _build_prime_prompt(), trace_tag="intent_classify_prime")
            setattr(acp, _INTENT_SESSION_PRIMED_ATTR, True)
        return sid


async def classify_intent(
    acp: Any,
    *,
    model: str | None,
    text: str,
    queue_state: dict | None = None,
    memory_context: str | None = None,
    timeout: float = 5.0,
    from_user: str = "",
) -> dict | None:
    """对自然语言做意图分类。失败一律返回 None，调用方自行兜底。

    Returns:
        {"intent": "...", "slots": {...}, "confidence": 0.x}  或  None
    """
    if not text or not text.strip():
        return None
    if acp is None:
        return None
    use_model = model or getattr(acp, "model", "") or "deepseek/deepseek-v4-flash"

    try:
        sid = await _ensure_session(acp, use_model)
    except Exception as e:  # noqa: BLE001
        log_flow_event(
            stage="route",
            route="intent_llm_session_error",
            user_text=text,
            from_user=from_user,
            extra={"error": str(e)[:200]},
        )
        return None

    prompt = _build_turn_prompt(text, queue_state, memory_context)
    try:
        reply, _ = await asyncio.wait_for(
            acp.prompt(sid, prompt, trace_tag="intent_classify"),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log_flow_event(
            stage="route",
            route="intent_llm_timeout",
            user_text=text,
            from_user=from_user,
            session_id=sid,
            extra={"timeout_sec": timeout},
        )
        return None
    except Exception as e:  # noqa: BLE001
        log_flow_event(
            stage="route",
            route="intent_llm_error",
            user_text=text,
            from_user=from_user,
            session_id=sid,
            extra={"error": str(e)[:200]},
        )
        return None

    obj = _extract_json(reply or "")
    if not obj:
        return None
    return _normalize(obj)
