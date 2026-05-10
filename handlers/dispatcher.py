"""统一决策与分发层。

- _llm_unified_decide：优先单次 structured 产出 {tool, payload, reply}；失败再仅决策 + unified_reply
- _coalesce_unified_decision：把 LLM JSON 规整成 (tool, payload, reply) 三元组（含旧名映射、payload 野字段吸收）
- _apply_unified_decision：根据 tool 名查 _TOOL_HANDLERS 并执行；末尾统一调用 run_post_write_hooks

不持有业务状态；与 Coach Mixin 通过 method dispatcher 表协作（运行时 self 由 Handler 组合提供）。
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from typing import Any, Awaitable, Callable

from acp.opencode_client import OpenCodeACP
from config import PACKAGE_ROOT
from utils.flow_log import log_flow_event
from utils.llm_reply_unescape import unescape_llm_visible_newlines
from utils.refresh_hooks import run_post_write_hooks
from utils.tool_names import (
    LEGACY_TOOL_ALIASES,  # noqa: F401  导入便于重导出
    TOOL_DECISION_NONE,
    TOOL_RECORD_ADD,
    TOOL_REMIND_ADD,
    TOOL_TIMELINE_APPEND,
    TOOL_TODO_ABANDON_CURRENT,
    TOOL_TODO_DONE_CURRENT,
    TOOL_TODO_MERGE_NEW_ITEMS,
    TOOL_TODO_NEXT,
    TOOL_TODO_NOT_DONE,
    TOOL_TODO_REORDER,
    TOOL_TODO_REORDER_CONFIRM,
    TOOL_TODO_SKIP_CURRENT,
    normalize_tool_name,
)

# legacy 文本 JSON 解析仍失败时，若原文含「{」（多半在尝试输出 JSON），则将全文作为 reply 兜底，避免用户侧空白。
# 16K 字符量级：明显长于常见微信单条，又避免极端超长占用内存/日志。
UNIFIED_LEGACY_RAW_FALLBACK_MAX_CHARS = 16384

PROMPTS_DIR = PACKAGE_ROOT / "prompts"
_UNIFIED_DECIDE_FILE = "unified_decide.md"

_DEFAULT_UNIFIED_DECIDE = """{hint_block}你是微信个人助手。请在同一轮输出里同时完成：动作决策（tool+payload）与发给用户的中文 reply（微信里自然、简短即可）。
根对象只能包含 tool、payload、reply 三个键；禁止其它键。
reply：须至少一句可见中文；多行时在 JSON 的 reply 字符串内只使用标准 JSON 换行转义（一个 \\ 加字母 n）；禁止双重转义（不要写成两个 \\ 再跟 n，否则微信仍显示字面量 \\n）；禁止用「等着我去翻」「快了快了」等假装正在查日记的话术；若使用「刚才/前面/刚列过」等指代，必须附上关键原文片段，禁止空指代；tool 非 none 时可附带一句简短确认。

{tool_hint}{rules}{context_block}

---

{hint_block}你是微信个人助手。请只做动作决策，不生成给用户的话术。
根对象只能包含 tool、payload 两个键；禁止出现 reply、message、content 等任何其它键，即使用户在闲聊、角色扮演也不要在本轮输出里写回复正文。
你必须仅输出一个 JSON 对象，字段只有 tool 与 payload。

{tool_hint}{rules}{context_block}

---

你是微信个人助手。根据用户原话回复 1～3 句自然、简短中文。
硬约束：
- 禁止用「等着我去翻」「我去查查」「快了快了」「别催」等假装正在查询、拖延交付的话术。
- 若用户在要具体事实、清单、记忆/日记/记录内容，而你这里没有引用任何材料，应直接说明自己本轮拿不到日记正文，可请用户发「查看记录」或「找一下最近的三条记忆」这类话触发系统自动读取；不要承诺代查或演「正在翻」。
- 纯闲聊、问你在做什么、吐槽等，正常接话即可。
- 段落之间直接按回车分段，不要输出字面量「反斜杠 + 字母 n」两个字符。
不要 JSON，不要解释，不要 markdown。

用户原话：{user_text}
回复：

--

你是微信个人助手。动作已被系统接管执行，请输出一句简短确认文案。
不要 JSON，不要解释，不要 markdown。

用户原话：{user_text}
已执行动作：tool={tool}, payload={payload_json}
回复："""


def _load_unified_decide_prompts() -> tuple[str, str, str, str]:
    """从 unified_decide.md 读取联合模板，按 --- 拆分返回 4 段:
    (structured_combined, structured_decision_only, reply_none, reply_action)

    每次调用都重新读取，确保热更新。缺失/异常 → fallback 默认值。
    """
    path = PROMPTS_DIR / _UNIFIED_DECIDE_FILE
    try:
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            parts = content.split("\n---\n", 2)
            if len(parts) == 3:
                reply_parts = parts[2].strip().split("\n--\n", 1)
                reply_none = reply_parts[0].strip() if reply_parts else parts[2].strip()
                reply_action = reply_parts[1].strip() if len(reply_parts) > 1 else reply_none
                return parts[0].strip(), parts[1].strip(), reply_none, reply_action
    except OSError:
        pass
    parts = _DEFAULT_UNIFIED_DECIDE.split("\n---\n", 2)
    reply_parts = parts[2].strip().split("\n--\n", 1)
    reply_none = reply_parts[0].strip() if reply_parts else parts[2].strip()
    reply_action = reply_parts[1].strip() if len(reply_parts) > 1 else reply_none
    return parts[0].strip(), parts[1].strip(), reply_none, reply_action

# 跟进时间短句（如「中午一点吧」）：含时间词且整体较短则暴露 remind.add，无需跨轮状态。
_CANDIDATE_TIME_HINT_RE = re.compile(
    r"\d{1,2}[:：]\d{2}|[0-9零一两三四五六七八九十]+点|中午|下午|晚上|上午"
)

# tool=none 且 structured 多轮重试后：这些子串易与「已执行写入」混淆，需加免责提示。
_FALSE_EXEC_REPLY_MARKERS = (
    "已记下",
    "记下了",
    "设好了",
    "已经帮你",
    "帮你设好",
    "真记上",
    "补记",
)


def _coalesce_unified_decision(decision: dict) -> tuple[str, dict[str, Any], str]:
    """LLM JSON -> (tool, payload, reply)。

    - tool：按 tool_names.normalize_tool_name 处理（小写 + 旧名映射）
    - payload：保证是 dict；若 LLM 把 tasks/reorder 写在了顶层而非 payload 里，自动吸收
    - reply：strip 后字符串
    """
    raw_tool = decision.get("tool", TOOL_DECISION_NONE) or TOOL_DECISION_NONE
    tool = normalize_tool_name(str(raw_tool))
    payload = decision.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    payload = dict(payload)
    if "tasks" in decision and "tasks" not in payload:
        t = decision.get("tasks")
        if isinstance(t, list):
            payload["tasks"] = t
    if "reorder" in decision and "reorder" not in payload:
        r = decision.get("reorder")
        if isinstance(r, list):
            payload["reorder"] = r
    reply = unescape_llm_visible_newlines(
        str(decision.get("reply", "") or "").strip()
    )
    return tool, payload, reply


# 以 (handler, payload, reply, from_user, context_token, user_text) -> bool 为统一签名
_HandlerFn = Callable[[Any, dict, str, str, str, str], Awaitable[bool]]
_TOOL_HANDLERS: dict[str, _HandlerFn] = {}
_UNIFIED_TOOLS = (
    TOOL_TODO_MERGE_NEW_ITEMS,
    TOOL_TODO_DONE_CURRENT,
    TOOL_TODO_NOT_DONE,
    TOOL_TODO_NEXT,
    TOOL_TODO_REORDER,
    TOOL_TODO_REORDER_CONFIRM,
    TOOL_TODO_SKIP_CURRENT,
    TOOL_TODO_ABANDON_CURRENT,
    TOOL_RECORD_ADD,
    TOOL_REMIND_ADD,
    TOOL_TIMELINE_APPEND,
    TOOL_DECISION_NONE,
)


def register_tool_handler(tool: str, fn: _HandlerFn) -> None:
    """coach 模块在 import 时调用，把自家 method 装进 dispatcher 表。

    用注册式而不是 import 式，避免 dispatcher → coaches → dispatcher 循环依赖。
    """
    _TOOL_HANDLERS[tool] = fn


def get_registered_tools() -> list[str]:
    return sorted(_TOOL_HANDLERS.keys())


class DispatcherMixin:
    """Handler 组合后即获得三域统一决策与分发能力。"""

    @staticmethod
    def _unified_decision_schema(*, include_reply: bool) -> dict:
        props: dict[str, Any] = {
            "tool": {"type": "string", "enum": list(_UNIFIED_TOOLS)},
            "payload": {"type": "object"},
        }
        required = ["tool", "payload"]
        if include_reply:
            props["reply"] = {"type": "string"}
            required.append("reply")
        return {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }

    @staticmethod
    def _build_unified_context_block(
        *,
        current_task: str,
        remaining: list[str],
        pending_reorder: list[str],
        text: str,
    ) -> str:
        return (
            f"- current_task: {current_task or 'null'}\n"
            f"- remaining_tasks: {json.dumps(remaining, ensure_ascii=False)}\n"
            f"- pending_reorder: {json.dumps(pending_reorder, ensure_ascii=False)}\n"
            f"- user_message: {text}\n"
        )

    @staticmethod
    def _select_candidate_tools(text: str, current_task: str, remaining: list[str]) -> list[str]:
        """动态工具发现：本轮只高亮少量候选工具，降低工具认知过载。"""
        t = (text or "").strip()
        out = [TOOL_DECISION_NONE]
        if any(k in t for k in ("提醒", "叫我", "闹钟", "别忘")):
            out.append(TOOL_REMIND_ADD)
        if any(
            k in t
            for k in (
                "记录",
                "记一下",
                "改签",
                "体重",
                "跑步",
                "快递",
                "吃了",
                "喝过",
                "喝了",
                "用药",
                "吃药",
                "睡了",
                "午饭",
                "晚饭",
                "早饭",
                "早餐",
                "晚餐",
            )
        ):
            out.append(TOOL_RECORD_ADD)
        if t.startswith("追加") or any(
            k in t for k in ("追加到时间轴", "追加时间轴", "追加到时间线")
        ):
            out.append(TOOL_TIMELINE_APPEND)
        if any(k in t for k in ("待办", "下一个", "做完", "跳过", "放弃", "重排")):
            out.extend(
                [
                    TOOL_TODO_MERGE_NEW_ITEMS,
                    TOOL_TODO_DONE_CURRENT,
                    TOOL_TODO_NOT_DONE,
                    TOOL_TODO_NEXT,
                    TOOL_TODO_REORDER,
                    TOOL_TODO_REORDER_CONFIRM,
                    TOOL_TODO_SKIP_CURRENT,
                    TOOL_TODO_ABANDON_CURRENT,
                ]
            )
        if current_task or remaining:
            out.extend(
                [
                    TOOL_TODO_DONE_CURRENT,
                    TOOL_TODO_NOT_DONE,
                    TOOL_TODO_NEXT,
                    TOOL_TODO_SKIP_CURRENT,
                    TOOL_TODO_ABANDON_CURRENT,
                ]
            )
        if len(t) < 20 and _CANDIDATE_TIME_HINT_RE.search(t):
            out.append(TOOL_REMIND_ADD)
            # 短句+钟点常为「已发生事实」而非提醒，同时暴露生活记录以免只选 remind
            if any(
                k in t
                for k in (
                    "吃了",
                    "睡了",
                    "喝了",
                    "药",
                    "饭",
                    "面",
                    "跑完",
                    "走了",
                    "体重",
                )
            ):
                out.append(TOOL_RECORD_ADD)
        # 相对未来日期 + 钟点 + 备忘语义 → 优先提醒（与时间轴状态句区分）
        if any(x in t for x in ("三天后", "两天后", "明天", "后天", "下周", "过几天")):
            if any(x in t for x in ("点", ":", "：", "半")) and any(
                x in t for x in ("提醒", "叫我", "别忘", "备忘", "闹钟")
            ):
                out.append(TOOL_REMIND_ADD)
        # 保序去重
        seen = set()
        uniq: list[str] = []
        for x in out:
            if x in seen:
                continue
            seen.add(x)
            uniq.append(x)
        return uniq

    @staticmethod
    def _tool_discovery_hint(candidates: list[str]) -> str:
        return (
            "本轮候选工具（动态发现，仅缩小搜索范围；"
            "纯闲聊、只读查询、不确定时仍必须选 none，不要因为出现在列表里就强行选工具）：\n"
            + ", ".join(candidates)
            + "\n\n"
        )

    @staticmethod
    def _build_unified_legacy_prompt(hint_block: str, context_block: str) -> str:
        return (
            f"{hint_block}"
            "你是微信个人助手。待办话术与意图划分以 todo-coach 为准；生活记录以 record-coach 为准；"
            "提醒以 remind-coach 为准。你必须只输出一个合法 JSON 对象，不要其它内容。\n"
            "输出格式硬约束（必须全部满足）：\n"
            "1) 仅输出 1 行 JSON，对象根节点必须包含 tool、payload、reply 三个键\n"
            "2) 使用双引号，不要单引号，不要注释，不要 markdown，不要代码块\n"
            "3) reply 必须是 JSON 字符串；换行使用标准 JSON 转义（\\\\n 在 JSON 文本里表现为反斜杠+n 一对）；禁止双重反斜杠；禁止用未转义的真实换行打断整段 JSON\n"
            "4) 若不确定，输出 {\"tool\":\"none\",\"payload\":{},\"reply\":\"\"}\n"
            "5) 严禁在 JSON 前后输出任何说明文字\n"
            "请严格按这个骨架输出："
            "{\"tool\":\"<tool>\",\"payload\":{},\"reply\":\"<reply>\"}\n"
            "字段：\n"
            "- tool: 字符串，取值之一：\n"
            f'  待办：{TOOL_TODO_MERGE_NEW_ITEMS} | {TOOL_TODO_DONE_CURRENT} | {TOOL_TODO_NOT_DONE} | '
            f'{TOOL_TODO_NEXT} | {TOOL_TODO_REORDER} | {TOOL_TODO_REORDER_CONFIRM} | '
            f'{TOOL_TODO_SKIP_CURRENT} | {TOOL_TODO_ABANDON_CURRENT}\n'
            f'  生活记录：{TOOL_RECORD_ADD}\n'
            f'  设/加提醒：{TOOL_REMIND_ADD}\n'
            f'  时间轴追加：{TOOL_TIMELINE_APPEND}\n'
            f'  不处理：{TOOL_DECISION_NONE}\n'
            "- payload: 对象（见下）\n"
            "- reply: 可选，给用户的微信短句\n\n"
            "payload 约定：\n"
            f'- "{TOOL_TODO_MERGE_NEW_ITEMS}": {{"tasks": ["项1", ...]}}\n'
            f'- "{TOOL_TODO_REORDER}": {{"reorder": [...]}}，元素集合须与 remaining_tasks 相同，仅顺序可变\n'
            f'- 其它 todo.*：通常为 {{}}\n'
            f'- "{TOOL_RECORD_ADD}": {{"text": "...", "category": "身体|运动|阅读|事务", "event_date": "YYYY-MM-DD?", '
            f'"event_hhmm": "HH:MM?", "timeline_line": "可选，已为时间轴准备好的短句"}}\n'
            f'  （event_hhmm：用户已发生事件所指钟点，如「15:00 吃了弥宁」→ "15:00"；下午三点→"15:00"；无则省略，系统用当前时刻）\n'
            f'- "{TOOL_REMIND_ADD}": {{"text": "...", "hhmm": "HH:MM", "event_date": "YYYY-MM-DD?"}}\n'
            f'- "{TOOL_TIMELINE_APPEND}": {{"text": "...", "slot": "HH:MM?"}}（slot 省略则用当前半格）\n\n'
            "分流：\n"
            f'- 已发生的生活事件（体重/饮食/睡眠/快递/出行已落实/已用药等）→ {TOOL_RECORD_ADD}；时间轴由记录写盘后再回写，禁止用 {TOOL_TIMELINE_APPEND} 绕过记录\n'
            f'- 句中含用户所指事件钟点（如 15:00、下午3点）的已发生事实 → {TOOL_RECORD_ADD}，且须在 payload 填 event_hhmm（规范 HH:MM）\n'
            f'- {TOOL_TIMELINE_APPEND} 仅用于：用户明确要求「追加到时间轴/追加时间轴」或句首「追加…」且意图是只改时间轴展示、不是记生活日志；不得用于用药/吃饭等已发生记录\n'
            f'- 用户明确要求写时间轴追加（上一行所述窄口径）且要写盘 → {TOOL_TIMELINE_APPEND}\n'
            f'- 设提醒/叫我/别忘了+具体时间 → {TOOL_REMIND_ADD}\n'
            f'- 含「明天/后天/三天后/下周…」等相对未来日期且含钟点（如两点、14:00）且为提醒/叫我/别忘了 → {TOOL_REMIND_ADD}（优先于生活记录）\n'
            f'- 待办推进/完成/重排/跳过/放弃 → todo.*\n'
            f'- 纯闲聊/问助手状态 → {TOOL_DECISION_NONE}，reply 禁止假称正在查日记\n'
            f'- 查日记/记忆等只读需求由入口层处理，仍输出 {TOOL_DECISION_NONE}，reply 可提示用户用「查看记录」等，勿编造列表\n'
            f'- 其它不确定 → {TOOL_DECISION_NONE}\n\n'
            "何时不用（反例）：\n"
            f'- 不要把「查看记录/查看待办/查看提醒」判成 {TOOL_RECORD_ADD} 或 {TOOL_TODO_MERGE_NEW_ITEMS}\n'
            f'- 不要把无时间的“记得提醒我”直接判成 {TOOL_REMIND_ADD}（应先要时间或给 none）\n'
            f'- 不要把纯情绪/寒暄句判成 todo.*，应给 {TOOL_DECISION_NONE}\n\n'
            f"{context_block}"
        )

    def _unified_decide_hint_and_context(
        self,
        user_id: str,
        text: str,
        *,
        intent_hint: dict | None,
    ) -> tuple[str, str, str]:
        current_task = self._get_current_queue_task(user_id)
        remaining = self._get_remaining_queue_tasks(user_id)
        pending_reorder = self._pending_reorders.get(user_id, [])
        candidates = self._select_candidate_tools(text, current_task, remaining)
        context_block = self._build_unified_context_block(
            current_task=current_task,
            remaining=remaining,
            pending_reorder=pending_reorder,
            text=text,
        )
        context_block = self._augment_context_block_with_memory(user_id, context_block)
        tool_hint_block = self._tool_discovery_hint(candidates)
        hint_block = ""
        if isinstance(intent_hint, dict) and intent_hint:
            try:
                hint_block = (
                    "参考意图（来自小模型预分类，仅作参考，可推翻）：\n"
                    f"{json.dumps(intent_hint, ensure_ascii=False)}\n\n"
                )
            except Exception:
                hint_block = ""
        return hint_block, context_block, tool_hint_block

    def _augment_context_block_with_memory(self, user_id: str, context_block: str) -> str:
        """把 Handler 提供的结构化记忆拼进 unified 上下文。

        分层注入：
          - 顶部：structured_state_block（现在在做什么）
          - 中部：recent_decisions_json（刚才做了什么，现有）
          - 底部：context_block（本轮上下文）
        """
        state_block = ""
        gs = getattr(self, "_structured_state_context_block", None)
        if callable(gs):
            try:
                state_block = (gs(user_id) or "").strip()
            except Exception:
                state_block = ""
        extra_ctx = ""
        ge = getattr(self, "_extra_unified_context", None)
        if callable(ge):
            try:
                extra_ctx = (ge(user_id) or "").strip()
            except Exception:
                extra_ctx = ""
        parts = []
        if state_block:
            parts.append(state_block)
        if extra_ctx:
            parts.append(extra_ctx)
        parts.append(context_block)
        return "\n".join(parts) + "\n"

    def _unified_decide_tool_payload_reply_rules_block(self) -> str:
        """tool/payload/reply 字段说明 + 分流（合并路径与仅决策路径共用）。"""
        return (
            "字段：\n"
            "- tool: 字符串，取值之一：\n"
            f'  待办：{TOOL_TODO_MERGE_NEW_ITEMS} | {TOOL_TODO_DONE_CURRENT} | {TOOL_TODO_NOT_DONE} | '
            f'{TOOL_TODO_NEXT} | {TOOL_TODO_REORDER} | {TOOL_TODO_REORDER_CONFIRM} | '
            f'{TOOL_TODO_SKIP_CURRENT} | {TOOL_TODO_ABANDON_CURRENT}\n'
            f'  生活记录：{TOOL_RECORD_ADD}\n'
            f'  设/加提醒：{TOOL_REMIND_ADD}\n'
            f'  时间轴追加：{TOOL_TIMELINE_APPEND}\n'
            f'  不处理：{TOOL_DECISION_NONE}\n'
            "- payload: 对象（见下）\n\n"
            "payload 约定：\n"
            f'- "{TOOL_TODO_MERGE_NEW_ITEMS}": {{"tasks": ["项1", ...]}}\n'
            f'- "{TOOL_TODO_REORDER}": {{"reorder": [...]}}，元素集合须与 remaining_tasks 相同，仅顺序可变\n'
            f'- 其它 todo.*：通常为 {{}}\n'
            f'- "{TOOL_RECORD_ADD}": {{"text": "...", "category": "身体|运动|阅读|事务", "event_date": "YYYY-MM-DD?", '
            f'"event_hhmm": "HH:MM?", "timeline_line": "可选"}}\n'
            f'  （event_hhmm：已发生事件在用户句中的钟点，如「15:00 吃了弥宁」→ "15:00"；无则省略）\n'
            '- **重要：reply 与 payload.text 语气分离**\n'
            '  - reply：微信聊天语气，可有温度/语气词/emoji，承接上文对话\n'
            '  - payload.text：日志书面语，去情绪词、去口语、客观陈述，如 "体重 72.3kg"\n'
            '  - 示例：用户说 "今天喝了奶茶撑死了" → reply: "记下了！偶尔一杯没事~" → payload.text: "下午 奶茶"\n'
            f'- "{TOOL_REMIND_ADD}": {{"text": "...", "hhmm": "HH:MM", "event_date": "YYYY-MM-DD?"}}\n'
            f'- "{TOOL_TIMELINE_APPEND}": {{"text": "...", "slot": "HH:MM?"}}\n\n'
            "分流规则：\n"
            f'- 已发生的生活事件 → {TOOL_RECORD_ADD}；时间轴仅由记录流程回写，禁止用 {TOOL_TIMELINE_APPEND} 写用药/饮食/睡眠等已发生事实\n'
            f'- 句中含事件钟点的已发生事实 → {TOOL_RECORD_ADD} 且须填 event_hhmm（HH:MM）\n'
            f'- {TOOL_TIMELINE_APPEND} 仅用于：句首「追加…」或「追加到时间轴/追加时间轴」且用户意图是只改时间轴、不写生活记录节；窄口径，与 {TOOL_RECORD_ADD} 区分\n'
            f'- 明确要求时间轴追加（上一行窄口径）且须带可落盘内容 → {TOOL_TIMELINE_APPEND}\n'
            f'- 设提醒/叫我/别忘了+具体时间 → {TOOL_REMIND_ADD}\n'
            f'- 含「明天/后天/三天后/下周…」等相对未来日期且含钟点且为提醒/叫我/别忘了 → {TOOL_REMIND_ADD}（优先于生活记录；与「过去半小时在做什么」类状态句区分）\n'
            f'- 待办推进/完成/重排/跳过/放弃 → todo.*\n'
            f'- 纯闲聊、问助手在干嘛、评价/吐槽 → {TOOL_DECISION_NONE}（不得假称正在帮用户查日记或记忆）\n'
            f'- 读今日日记/查记忆/列最近几条记录等只读查询由消息层本地处理，若用户句子里已有「查看记录」「最近几条记忆」等触发词，仍选 {TOOL_DECISION_NONE}，不要编造查询结果\n'
            f'- 其它不确定 → {TOOL_DECISION_NONE}\n\n'
            "何时不用（反例）：\n"
            f'- 不要把「查看记录/查看待办/查看提醒」判成 {TOOL_RECORD_ADD} 或 {TOOL_TODO_MERGE_NEW_ITEMS}\n'
            f'- 不要把无时间的“记得提醒我”直接判成 {TOOL_REMIND_ADD}（应先要时间或给 none）\n'
            f'- 不要把纯情绪/寒暄句判成 todo.*，应给 {TOOL_DECISION_NONE}\n\n'
            "可追问性（reply 硬约束）：\n"
            "- 若 reply 中出现「刚才/前面/刚给你列过/已经说过」等指代，必须同时附上 1～3 条关键原文"
            "（例如提交号一行、列表项一行），禁止空指代。\n"
            "- 若你本轮没有可粘贴的原文，必须明确写「本轮无法附上原文」，不要假装已经展示过。\n\n"
        )

    @staticmethod
    def _sanitize_vague_none_reply(
        reply: str,
        tool: str,
        *,
        structured_trace: dict | None = None,
    ) -> str:
        """tool=none 时：承接语但无列表/提交号等证据则补一句可追问提示。"""
        if tool != TOOL_DECISION_NONE:
            return reply
        t = (reply or "").strip()
        if not t:
            return reply
        attempts = 1
        if isinstance(structured_trace, dict):
            with suppress(Exception):
                attempts = int(structured_trace.get("structured_attempts") or 1)
        if attempts > 1 and any(m in t for m in _FALSE_EXEC_REPLY_MARKERS):
            guard_phrase = "系统侧未确认写入"
            if guard_phrase not in t:
                return (
                    t
                    + "\n（本轮模型输出经过重试；若你本来要「加提醒」但系统侧未确认写入，"
                    "请再发一条完整提醒句，例如「明天 13:00 提醒我拿快递」。）"
                )
        if not any(
            x in t
            for x in OpenCodeACP._STRUCTURED_VAGUE_REFERENCE_SUBSTRINGS
        ):
            return reply
        if "`" in t or re.search(r"\b[0-9a-f]{7,40}\b", t, re.I):
            return reply
        if "\n-" in t or re.search(r"\n\s*[-*]\s", t):
            return reply
        return (
            t
            + "\n（若上面没有你要的列表/提交号原文，回我「重发完整列表」我按原文贴。）"
        )

    async def _llm_unified_decide_structured_combined(
        self,
        user_id: str,
        text: str,
        *,
        intent_hint: dict | None = None,
    ) -> dict | None:
        """单次结构化：tool + payload + reply（正常路径，省第二轮 unified_reply）。"""
        hint_block, context_block, tool_hint = self._unified_decide_hint_and_context(
            user_id, text, intent_hint=intent_hint
        )
        rules = self._unified_decide_tool_payload_reply_rules_block()
        tmpl, _, _, _ = _load_unified_decide_prompts()
        prompt = tmpl.format(
            hint_block=hint_block,
            tool_hint=tool_hint,
            rules=rules,
            context_block=context_block,
        )
        op_cfg = self.cfg.get("opencode", {}) or {}
        retry_count = int(op_cfg.get("structured_retry_count", 3) or 3)
        schema = self._unified_decision_schema(include_reply=True)
        return await self.acp.prompt_structured(
            self.unified_session_id,
            prompt,
            json_schema=schema,
            retry_count=retry_count,
            trace_tag="unified_decide_combined",
        )

    async def _llm_unified_decide_structured_decision_only(
        self,
        user_id: str,
        text: str,
        *,
        intent_hint: dict | None = None,
    ) -> dict | None:
        """仅决策（无 reply）；供合并路径失败后的兜底，reply 由 unified_reply 或 spill 补上。"""
        hint_block, context_block, tool_hint = self._unified_decide_hint_and_context(
            user_id, text, intent_hint=intent_hint
        )
        rules = self._unified_decide_tool_payload_reply_rules_block()
        _, tmpl, _, _ = _load_unified_decide_prompts()
        prompt = tmpl.format(
            hint_block=hint_block,
            tool_hint=tool_hint,
            rules=rules,
            context_block=context_block,
        )
        op_cfg = self.cfg.get("opencode", {}) or {}
        retry_count = int(op_cfg.get("structured_retry_count", 3) or 3)
        schema = self._unified_decision_schema(include_reply=False)
        return await self.acp.prompt_structured(
            self.unified_session_id,
            prompt,
            json_schema=schema,
            retry_count=retry_count,
            trace_tag="unified_decide_structured",
        )

    async def _llm_generate_unified_reply(
        self,
        *,
        user_text: str,
        tool: str,
        payload: dict[str, Any],
    ) -> str:
        _, _, reply_none_tmpl, reply_action_tmpl = _load_unified_decide_prompts()
        if tool == TOOL_DECISION_NONE:
            prompt = reply_none_tmpl.format(user_text=user_text)
        else:
            prompt = reply_action_tmpl.format(
                user_text=user_text,
                tool=tool,
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
        reply, _ = await self.acp.prompt(
            self.unified_session_id, prompt, trace_tag="unified_reply"
        )
        return unescape_llm_visible_newlines((reply or "").strip())

    async def _llm_unified_decide(
        self,
        user_id: str,
        text: str,
        *,
        intent_hint: dict | None = None,
    ) -> dict:
        """优先单次 structured 产出 tool+payload+reply；仅当该路径失败时才拆成仅决策 + unified_reply。"""
        max_rl = int((self.cfg.get("bot") or {}).get("max_reply_length", 2000) or 2000)

        decision = await self._llm_unified_decide_structured_combined(
            user_id=user_id, text=text, intent_hint=intent_hint
        )
        if isinstance(decision, dict):
            trace = decision.pop(OpenCodeACP.STRUCTURED_TRACE_META_KEY, None)
            tool, payload, reply = _coalesce_unified_decision(decision)
            reply = self._sanitize_vague_none_reply(reply, tool, structured_trace=trace)
            if reply and max_rl > 0 and len(reply) > max_rl:
                reply = reply[:max_rl]
            log_flow_event(
                stage="route",
                route="unified_structured_ok",
                user_text=text,
                session_id=self.unified_session_id,
                extra={
                    "decision": decision,
                    "unified_struct_path": "combined",
                    **({"structured_trace": trace} if trace else {}),
                },
            )
            return {"tool": tool, "payload": payload, "reply": reply}

        decision = await self._llm_unified_decide_structured_decision_only(
            user_id=user_id, text=text, intent_hint=intent_hint
        )
        if isinstance(decision, dict):
            trace = decision.pop(OpenCodeACP.STRUCTURED_TRACE_META_KEY, None)
            spill_key = OpenCodeACP.STRUCTURED_DECISION_SPILL_REPLY_KEY
            spill = str(decision.pop(spill_key, "") or "").strip()
            if spill and max_rl > 0 and len(spill) > max_rl:
                spill = spill[:max_rl]
            tool, payload, _ = _coalesce_unified_decision(decision)
            log_flow_event(
                stage="route",
                route="unified_structured_ok",
                user_text=text,
                session_id=self.unified_session_id,
                extra={
                    "decision": decision,
                    "unified_struct_path": "decision_only",
                    "structured_spill_reused": bool(spill and tool == TOOL_DECISION_NONE),
                    **({"structured_trace": trace} if trace else {}),
                },
            )
            reply = ""
            if spill and tool == TOOL_DECISION_NONE:
                reply = self._sanitize_vague_none_reply(spill, tool, structured_trace=trace)
            else:
                try:
                    reply = await self._llm_generate_unified_reply(
                        user_text=text,
                        tool=tool,
                        payload=payload,
                    )
                except Exception as e:  # noqa: BLE001
                    log_flow_event(
                        stage="route",
                        route="reply_generate_fail",
                        user_text=text,
                        session_id=self.unified_session_id,
                        extra={"error": str(e)[:200], "tool": tool},
                    )
                    reply = ""
            reply = self._sanitize_vague_none_reply(reply, tool, structured_trace=trace)
            if reply and max_rl > 0 and len(reply) > max_rl:
                reply = reply[:max_rl]
            return {"tool": tool, "payload": payload, "reply": reply}

        log_flow_event(
            stage="route",
            route="unified_structured_fail",
            user_text=text,
            session_id=self.unified_session_id,
            extra={"intent_hint": intent_hint},
        )
        return {"tool": TOOL_DECISION_NONE, "payload": {}, "reply": ""}

    async def _apply_unified_decision(
        self,
        decision: dict,
        from_user: str,
        context_token: str,
        user_text: str = "",
    ) -> bool:
        """执行 dispatcher 表里 tool 名对应的 coach；末尾统一调用 post-write hooks。"""
        tool, payload, reply = _coalesce_unified_decision(decision)
        if getattr(self, "_eval_mode", False):
            tr = getattr(self, "_eval_pipeline_trace", None)
            if isinstance(tr, list):
                snap = dict(payload) if isinstance(payload, dict) else {}
                raw = json.dumps(snap, ensure_ascii=False)
                if len(raw) > 800:
                    snap = {"_truncated": True, "keys": list(snap.keys())}
                tr.append(
                    {
                        "tool": tool,
                        "payload": snap,
                        "reply_preview": (reply or "")[:240],
                    }
                )

        if tool == TOOL_DECISION_NONE:
            log_flow_event(
                stage="timeline",
                route="write_blocked",
                user_text=user_text,
                from_user=from_user,
                session_id=self.unified_session_id,
                extra={"reason": "tool_none", "reply_present": bool(reply)},
            )
            if reply:
                await self.wx.send_text(reply, from_user, context_token)
                return True
            return False

        handler = _TOOL_HANDLERS.get(tool)
        if handler is not None:
            emit = getattr(self, "_emit_tool_status", None)
            bot_cfg = self.cfg.get("bot") or {}
            slow_sec = float(bot_cfg.get("tool_slow_hint_seconds", 3) or 0)
            slow_msg = str(bot_cfg.get("tool_slow_hint_text") or "").strip() or "还在处理中，稍等我一下…"
            if callable(emit):
                try:
                    await emit(
                        from_user=from_user,
                        context_token=context_token,
                        tool=tool,
                        phase="executing",
                    )
                except Exception:
                    pass
            actionable = (
                "请补充更具体的待办/提醒内容，或先发「查看记录」「查看待办」确认当前状态后再试。"
            )
            try:
                if slow_sec > 0:
                    exec_task = asyncio.create_task(
                        handler(self, payload, reply, from_user, context_token, user_text)
                    )
                    wait_task = asyncio.create_task(asyncio.sleep(slow_sec))
                    done, _pending = await asyncio.wait(
                        {exec_task, wait_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if exec_task in done:
                        wait_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await wait_task
                        handled = await exec_task
                    else:
                        await self.wx.send_text(slow_msg, from_user, context_token)
                        log_flow_event(
                            stage="route",
                            route="tool_slow_hint",
                            user_text=user_text,
                            from_user=from_user,
                            session_id=self.unified_session_id,
                            extra={"tool": tool, "after_sec": slow_sec},
                        )
                        handled = await exec_task
                else:
                    handled = await handler(
                        self, payload, reply, from_user, context_token, user_text
                    )
            except Exception as e:  # noqa: BLE001
                if callable(emit):
                    try:
                        await emit(
                            from_user=from_user,
                            context_token=context_token,
                            tool=tool,
                            phase="failed",
                        )
                    except Exception:
                        pass
                structured = {
                    "error": str(e)[:500],
                    "error_type": type(e).__name__,
                    "actionable_hint": actionable,
                    "tool": tool,
                }
                log_flow_event(
                    stage="route",
                    route="tool_handler_error",
                    user_text=user_text,
                    from_user=from_user,
                    session_id=self.unified_session_id,
                    extra=structured,
                )
                err_line = str(e).strip().replace("\n", " ")[:160]
                await self.wx.send_text(
                    f"处理失败：{err_line}\n{actionable}",
                    from_user,
                    context_token,
                )
                return True
            if handled:
                if callable(emit):
                    try:
                        await emit(
                            from_user=from_user,
                            context_token=context_token,
                            tool=tool,
                            phase="result_ready",
                        )
                    except Exception:
                        pass
                run_post_write_hooks(self, tool, payload)
                remember = getattr(self, "_remember_unified_decision", None)
                if callable(remember):
                    try:
                        remember(
                            from_user,
                            {"tool": tool, "payload": dict(payload), "reply": reply},
                        )
                    except Exception:
                        pass
                if callable(emit):
                    try:
                        await emit(
                            from_user=from_user,
                            context_token=context_token,
                            tool=tool,
                            phase="done",
                        )
                    except Exception:
                        pass
            return handled

        # 未知 tool：尽量不让用户被静默丢弃
        if reply:
            await self.wx.send_text(reply, from_user, context_token)
            return True
        await self.wx.send_text(
            "这个动作我暂时执行不了。建议：\n"
            "1) 直接说“记一条记录：…（可带日期）”\n"
            "2) 或说“提醒我 HH:MM …”\n"
            "3) 或先发“查看记录/查看待办”确认当前状态",
            from_user,
            context_token,
        )
        return False
