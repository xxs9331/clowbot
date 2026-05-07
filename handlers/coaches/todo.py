"""待办催办 Coach：内存队列 + 8 个 _coach_* 处理器。

- 写盘统一通过 utils/coach_tools.build_coach_write_prompt(DOMAIN_TODO, ...)
- 读盘已 Python 化：utils/section_reader.read_todo_section
- tool 名常量从 utils/tool_names 单一来源 import
"""

from __future__ import annotations

import re
from pathlib import Path

from handlers.dispatcher import register_tool_handler
from utils.coach_tools import OUTPUT_WRITE_CONFIRM, build_coach_write_prompt
from utils.flow_log import log_flow_event
from utils.log_sync import get_log_path, mark_todo_group_done, remove_todo_item_from_group
from utils.section_reader import read_todo_section
from utils.time_utils import time_str
from utils.tool_names import (
    DOMAIN_TODO,
    TOOL_TODO_ABANDON_CURRENT,
    TOOL_TODO_DONE_CURRENT,
    TOOL_TODO_MERGE_NEW_ITEMS,
    TOOL_TODO_NEXT,
    TOOL_TODO_NOT_DONE,
    TOOL_TODO_REORDER,
    TOOL_TODO_REORDER_CONFIRM,
    TOOL_TODO_SKIP_CURRENT,
)


class TodoCoachMixin:
    _TEMPLATE_REGISTRY_CANDIDATES = (
        "待办模板总表.md",
        "模板总表.md",
    )
    # ─── 内存催办队列 ───

    def _set_todo_queue(self, user_id: str, tasks: list[str]):
        self._todo_queues[user_id] = {"tasks": tasks, "idx": 0}

    def _get_current_queue_task(self, user_id: str) -> str:
        state = self._todo_queues.get(user_id)
        if not state:
            return ""
        idx = state.get("idx", 0)
        tasks = state.get("tasks", [])
        if idx >= len(tasks):
            return ""
        return tasks[idx]

    def _advance_queue_task(self, user_id: str) -> str:
        state = self._todo_queues.get(user_id)
        if not state:
            return ""
        state["idx"] = state.get("idx", 0) + 1
        next_task = self._get_current_queue_task(user_id)
        if not next_task:
            self._todo_queues.pop(user_id, None)
        return next_task

    def _get_remaining_queue_tasks(self, user_id: str) -> list[str]:
        state = self._todo_queues.get(user_id)
        if not state:
            return []
        idx = state.get("idx", 0)
        tasks = state.get("tasks", [])
        return tasks[idx:] if idx < len(tasks) else []

    @staticmethod
    def _has_next_prompt(reply: str) -> bool:
        """回复里是否仍包含待办追问（去重/节流用）。"""
        r = (reply or "").strip()
        if not r:
            return False
        if "做完了吗" in r:
            return True
        if "下一组：" in r or "下一个：" in r:
            return True
        return False

    # ─── Vault 写入 prompt 便捷 ───

    def _write_todo_prompt(self, payload: dict, output_contract: str = OUTPUT_WRITE_CONFIRM) -> str:
        v = self.cfg["vault"]
        return build_coach_write_prompt(
            DOMAIN_TODO,
            vault_root=v["root"],
            daily_log_dir=v["daily_log_dir"],
            payload=payload,
            output_contract=output_contract,
        )

    async def _sync_todo_queue_from_vault(self, user_id: str, fallback_tasks: list[str]) -> None:
        """Python 解析当日 `## 📋 待办` 未完成子项扁平序，作催办队列；解析失败回落 fallback。"""
        v = self.cfg["vault"]
        log_path = get_log_path(v["root"], v["daily_log_dir"])
        try:
            md = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        except Exception:
            md = ""
        flat: list[str] = []
        groups: list[dict] = []
        if md:
            try:
                parsed = read_todo_section(md)
                flat = parsed.get("queue_flat", []) or []
                groups = parsed.get("groups", []) or []
            except Exception:
                flat = []
                groups = []
        # 缓存 item -> group 映射，供 done_current 判断组边界。
        self._todo_item_group = {}
        self._todo_groups = groups
        for g_idx, g in enumerate(groups):
            for item in (g.get("items") or []):
                label = str(item).strip()
                if label:
                    self._todo_item_group[label] = g_idx
        tasks = [str(x).strip() for x in flat if str(x).strip()]
        if tasks:
            self._set_todo_queue(user_id, tasks)
            return
        self._set_todo_queue(user_id, fallback_tasks)

    async def _vault_tool_rewrite_flat(self, ordered_tasks: list[str]) -> None:
        if not ordered_tasks:
            return
        msg = self._write_todo_prompt(
            {"op": "rewrite_section_flat", "flat_order": ordered_tasks}
        )
        await self.acp.prompt(self.todo_session_id, msg, trace_tag="vault_reorder_todo")

    async def _vault_tool_remove_subitem(self, label: str) -> None:
        msg = self._write_todo_prompt(
            {"op": "remove_subitem", "remove_label": label}
        )
        await self.acp.prompt(self.todo_session_id, msg, trace_tag="vault_abandon_item")

    # ─── 模板待办展开（Python 读模板，支持模板内容随时改）───

    @staticmethod
    def _looks_like_template_ref(text: str) -> bool:
        s = text.strip()
        return ("模板" in s) or s.endswith(".md") or ("/" in s) or ("\\" in s)

    @staticmethod
    def _normalize_template_name(raw: str) -> str:
        s = (raw or "").strip().strip("。.!！")
        if not s:
            return ""
        s = re.sub(r"^(添加|加入|导入|用|使用)\s*", "", s)
        s = re.sub(r"(的?待办|待办)$", "", s).strip()
        s = s.strip("：:，, ")
        return s

    @staticmethod
    def _canonical_template_key(raw: str) -> str:
        """模板名归一化键：去掉高频噪音词，提升“下山模板/下山流程模板”匹配率。"""
        s = (raw or "").strip()
        if not s:
            return ""
        s = re.sub(r"\s+", "", s)
        s = re.sub(r"(流程|待办|代办|模板|模版)", "", s)
        return s.strip()

    def _resolve_template_registry_file(self) -> Path | None:
        root = Path(self.cfg["vault"]["root"])
        template_dir = root / "3-Resources" / "模板库"
        for name in self._TEMPLATE_REGISTRY_CANDIDATES:
            p = template_dir / name
            if p.exists() and p.is_file():
                return p
        return None

    @staticmethod
    def _parse_template_tasks(md_text: str) -> list[str]:
        """尽量宽容地从模板 Markdown 提取待办项，允许模板格式变动。"""
        out: list[str] = []
        seen: set[str] = set()
        in_code = False
        for raw in md_text.splitlines():
            line = raw.rstrip()
            s = line.strip()
            if not s:
                continue
            if s.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            if s.startswith("#") or s.startswith(">"):
                continue

            # 优先：checkbox 列表
            m = re.match(r"^\s*[-*+]\s*\[(?: |x|X)\]\s*(.+?)\s*$", line)
            if not m:
                # 其次：有序列表 / 无序列表
                m = re.match(r"^\s*(?:\d+[\.、)]|[-*+])\s+(.+?)\s*$", line)
            if not m:
                continue
            item = re.sub(r"\s+", " ", m.group(1)).strip(" -\t")
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    def _parse_template_registry_map(self, md_text: str) -> dict[str, list[str]]:
        """从模板总表解析 {模板名: [任务...]}。

        支持两种写法：
        1) H2/H3 分节 + 列表（推荐）
           ## 上山流程模板
           - [ ] 任务A
           - [ ] 任务B
        2) Markdown 表格
           | 模板 | 待办 |
           | 上山流程模板 | 任务A；任务B |
        """
        mapping: dict[str, list[str]] = {}

        # A. 分节列表
        current_name = ""
        current_items: list[str] = []
        for raw in md_text.splitlines():
            s = raw.strip()
            if not s:
                continue
            h = re.match(r"^#{2,3}\s+(.+?)\s*$", s)
            if h:
                if current_name and current_items:
                    mapping[current_name] = list(dict.fromkeys(current_items))
                current_name = h.group(1).strip()
                current_items = []
                continue
            if not current_name:
                continue
            m = re.match(r"^\s*(?:[-*+]\s*\[(?: |x|X)\]|[-*+]|\d+[\.、)])\s+(.+?)\s*$", raw)
            if not m:
                continue
            item = re.sub(r"\s+", " ", m.group(1)).strip(" -\t")
            if item:
                current_items.append(item)
        if current_name and current_items:
            mapping[current_name] = list(dict.fromkeys(current_items))

        # B. 表格补充（仅补充不存在模板名）
        for raw in md_text.splitlines():
            s = raw.strip()
            if not s.startswith("|") or s.count("|") < 3:
                continue
            cols = [c.strip() for c in s.strip("|").split("|")]
            if len(cols) < 2:
                continue
            left, right = cols[0], cols[1]
            if not left or not right:
                continue
            if set(left) <= {"-", ":"}:
                continue
            if left in ("模板", "template", "name"):
                continue
            if left in mapping:
                continue
            items = [
                re.sub(r"\s+", " ", x).strip()
                for x in re.split(r"[；;，,\n]+", right)
                if re.sub(r"\s+", " ", x).strip()
            ]
            if items:
                mapping[left] = list(dict.fromkeys(items))

        return mapping

    def _load_template_registry(self) -> dict[str, list[str]]:
        registry_path = self._resolve_template_registry_file()
        if not registry_path:
            return {}
        try:
            return self._parse_template_registry_map(registry_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _match_template_from_text(
        self, user_text: str, registry: dict[str, list[str]]
    ) -> tuple[str, list[str]] | None:
        """按用户原话显式检索模板名：只要出现“模板”就尝试匹配。"""
        if not user_text or "模板" not in user_text:
            return None
        text_key = self._canonical_template_key(self._normalize_template_name(user_text))
        if not text_key:
            return None
        candidates: list[tuple[int, str, list[str]]] = []
        for name, vals in registry.items():
            k = self._canonical_template_key(name)
            if not k:
                continue
            if (k in text_key) or (text_key in k):
                # 优先最长匹配，避免“上山”误吸附更短键
                candidates.append((len(k), name, vals))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, best_name, best_vals = candidates[0]
        return best_name, best_vals

    def _expand_template_tasks(self, tasks: list[str]) -> tuple[list[str], list[str]]:
        """把 tasks 中模板名展开为具体待办项（仅支持模板总表）。"""
        expanded: list[str] = []
        hit_templates: list[str] = []
        generic_keys = {"模板", "待办模板", "流程模板"}
        registry = self._load_template_registry()

        canonical_registry: dict[str, tuple[str, list[str]]] = {}
        for name, vals in registry.items():
            ck = self._canonical_template_key(name)
            if ck and ck not in canonical_registry:
                canonical_registry[ck] = (name, vals)

        for t in tasks:
            if not self._looks_like_template_ref(t):
                expanded.append(t)
                continue
            key = self._normalize_template_name(t)
            if not key:
                expanded.append(t)
                continue
            # exact -> canonical -> fuzzy
            items = registry.get(key)
            if items is None:
                ckey = self._canonical_template_key(key)
                if ckey and ckey in canonical_registry:
                    matched_name, matched_items = canonical_registry[ckey]
                    key = matched_name
                    items = matched_items
            if items is None and key not in generic_keys:
                for name, vals in registry.items():
                    if key in name:
                        items = vals
                        key = name
                        break
            if not items:
                expanded.append(t)
                continue
            expanded.extend(items)
            hit_templates.append(key)
        return expanded, hit_templates

    # ─── 8 个 coach handlers ───

    async def _coach_merge_new_items(
        self, payload: dict, reply: str, from_user: str, context_token: str, user_text: str = ""
    ) -> bool:
        raw_tasks = [str(t).strip() for t in (payload.get("tasks") or []) if str(t).strip()]
        tasks = list(raw_tasks)
        if not tasks:
            return False
        tasks, hit_templates = self._expand_template_tasks(tasks)
        # 兜底：仅当“当前任务整体看起来是模板引用”且常规展开未命中时，才用原句检索模板名。
        if (not hit_templates) and all(self._looks_like_template_ref(t) for t in raw_tasks):
            registry = self._load_template_registry()
            explicit = self._match_template_from_text(user_text, registry)
            if explicit:
                name, vals = explicit
                tasks = list(vals)
                hit_templates = [name]
        if hit_templates:
            log_flow_event(
                stage="route",
                route="todo_template_expand",
                user_text=user_text or "；".join(raw_tasks),
                from_user=from_user,
                session_id=self.session_id,
                extra={
                    "template_hits": hit_templates,
                    "raw_tasks": raw_tasks,
                    "expanded_count": len(tasks),
                },
            )
        if not tasks:
            await self._todo_emit_reply(
                "模板里没解析到可添加的待办项，请检查模板列表格式。", from_user, context_token
            )
            return True
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        vault_reply = ""
        try:
            prompt = self._write_todo_prompt(
                {"op": "merge_new_items", "new_items_ordered": tasks}
            )
            vault_reply, _ = await self.acp.prompt(
                self.todo_session_id, prompt, trace_tag="vault_append_todo"
            )
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
        self._pending_reorders.pop(from_user, None)
        await self._sync_todo_queue_from_vault(from_user, tasks)
        first_task = self._get_current_queue_task(from_user)
        base_ack = (
            reply or (vault_reply or "").strip() or f"记下了，这{len(tasks)}个我按顺序陪你做。"
        )
        if hit_templates and ("模板" not in base_ack):
            base_ack = f"已按模板总表导入（{', '.join(hit_templates)}），共 {len(tasks)} 项。"
        ack = base_ack
        if first_task:
            ack = f"{ack}\n先做：{first_task}，做完了吗？"
        await self._todo_emit_reply(ack, from_user, context_token)
        return True

    async def _coach_done_current(
        self, payload: dict, reply: str, from_user: str, context_token: str, user_text: str = ""
    ) -> bool:
        current_task = self._get_current_queue_task(from_user)
        if not current_task:
            await self._todo_emit_reply(reply or "你现在没有进行中的待办。", from_user, context_token)
            return True
        state = self._todo_queues.get(from_user, {})
        total_count = len(state.get("tasks", []))
        idx = state.get("idx", 0)
        completed_count = min(idx + 1, total_count) if total_count > 0 else 0
        self._pending_reorders.pop(from_user, None)
        now_hm = time_str()
        groups = list(getattr(self, "_todo_groups", []) or [])
        group_idx = getattr(self, "_todo_item_group", {}).get(current_task)
        is_last_in_group = False
        if group_idx is not None and 0 <= group_idx < len(groups):
            g_items = groups[group_idx].get("items") or []
            if g_items:
                is_last_in_group = g_items[-1] == current_task
        if is_last_in_group:
            v = self.cfg["vault"]
            log_path = get_log_path(v["root"], v["daily_log_dir"])
            try:
                mark_todo_group_done(log_path, current_task, now_hm)
            except Exception:
                pass
        self._advance_queue_task(from_user)
        progress_text = f"({completed_count}/{total_count})" if total_count > 0 else ""
        nudge_text = (
            f"✅ 本组完成 {progress_text}。"
            if is_last_in_group
            else (f"✅ {current_task}完成 {progress_text}。" if progress_text else f"✅ {current_task}完成。")
        )
        next_task = self._get_current_queue_task(from_user)
        if next_task:
            prompt = f"{nudge_text} 下一个是「{next_task}」。请用一句自然的话鼓励用户继续。"
            nudged, _ = await self.acp.prompt(
                self.unified_session_id, prompt, trace_tag="todo_nudge"
            )
            await self._todo_emit_reply((nudged or reply or nudge_text).strip(), from_user, context_token)
            return True
        await self._todo_emit_reply(f"{nudge_text} 全部清空 🎉", from_user, context_token)
        return True

    async def _coach_not_done(
        self, payload: dict, reply: str, from_user: str, context_token: str, user_text: str = ""
    ) -> bool:
        current_task = self._get_current_queue_task(from_user)
        fallback = (
            f"先做1分钟版本：{current_task}，做好再回我“好了”。"
            if current_task
            else "没问题，你先发几个待办我来排。"
        )
        await self._todo_emit_reply(reply or fallback, from_user, context_token)
        return True

    async def _coach_next(
        self, payload: dict, reply: str, from_user: str, context_token: str, user_text: str = ""
    ) -> bool:
        current_task = self._get_current_queue_task(from_user)
        fallback = f"你现在先做：{current_task}" if current_task else "当前没有进行中的短待办。"
        await self._todo_emit_reply(reply or fallback, from_user, context_token)
        return True

    async def _coach_reorder(
        self, payload: dict, reply: str, from_user: str, context_token: str, user_text: str = ""
    ) -> bool:
        order = [str(t).strip() for t in (payload.get("reorder") or []) if str(t).strip()]
        remaining = self._get_remaining_queue_tasks(from_user)
        if not remaining or sorted(order) != sorted(remaining):
            await self._todo_emit_reply(
                "顺序建议我收到了，但还不能安全改队列，请你再确认一次。", from_user, context_token
            )
            return True
        self._pending_reorders[from_user] = order
        ask = reply or f"我建议顺序：{' → '.join(order)}。按这个顺序更新吗？"
        await self._todo_emit_reply(ask, from_user, context_token)
        return True

    async def _coach_reorder_confirm(
        self, payload: dict, reply: str, from_user: str, context_token: str, user_text: str = ""
    ) -> bool:
        order = self._pending_reorders.get(from_user, [])
        if not order:
            await self._todo_emit_reply(reply or "当前没有待确认的重排建议。", from_user, context_token)
            return True
        self._set_todo_queue(from_user, order)
        self._pending_reorders.pop(from_user, None)
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            await self._vault_tool_rewrite_flat(order)
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)
        current_task = self._get_current_queue_task(from_user)
        confirm_reply = reply or "已按确认顺序更新。"
        if current_task:
            confirm_reply = f"{confirm_reply}\n先做：{current_task}，做完了吗？"
        await self._todo_emit_reply(confirm_reply, from_user, context_token)
        return True

    async def _coach_skip_current(
        self, payload: dict, reply: str, from_user: str, context_token: str, user_text: str = ""
    ) -> bool:
        state = self._todo_queues.get(from_user)
        if not state:
            await self._todo_emit_reply(reply or "当前没有可跳过的待办。", from_user, context_token)
            return True
        tasks = state.get("tasks", [])
        idx = state.get("idx", 0)
        if idx >= len(tasks):
            await self._todo_emit_reply(reply or "当前没有可跳过的待办。", from_user, context_token)
            return True
        self._pending_reorders.pop(from_user, None)
        skipped_task = tasks.pop(idx)
        tasks.append(skipped_task)
        next_task = self._get_current_queue_task(from_user)
        skip_reply = reply or f"先跳过：{skipped_task}。"
        if next_task and next_task != skipped_task:
            skip_reply = f"{skip_reply}\n现在先做：{next_task}，做完了吗？"
        await self._todo_emit_reply(skip_reply, from_user, context_token)
        return True

    async def _coach_abandon_current(
        self, payload: dict, reply: str, from_user: str, context_token: str, user_text: str = ""
    ) -> bool:
        state = self._todo_queues.get(from_user)
        if not state:
            await self._todo_emit_reply(reply or "当前没有可放弃的待办。", from_user, context_token)
            return True
        tasks = state.get("tasks", [])
        idx = state.get("idx", 0)
        if idx >= len(tasks):
            await self._todo_emit_reply(reply or "当前没有可放弃的待办。", from_user, context_token)
            return True
        self._pending_reorders.pop(from_user, None)
        abandoned_task = tasks.pop(idx)
        if tasks:
            state["idx"] = min(idx, len(tasks) - 1)
        v = self.cfg["vault"]
        log_path = get_log_path(v["root"], v["daily_log_dir"])
        try:
            remove_todo_item_from_group(log_path, abandoned_task)
        except Exception:
            pass
        if not tasks:
            self._todo_queues.pop(from_user, None)
            final_text = f"已放弃：{abandoned_task}。当前没有进行中的待办。"
            nudged, _ = await self.acp.prompt(
                self.unified_session_id,
                f"{final_text} 请用一句自然中文回复用户。",
                trace_tag="todo_nudge",
            )
            await self._todo_emit_reply((reply or nudged or final_text).strip(), from_user, context_token)
            return True
        next_task = self._get_current_queue_task(from_user)
        abandon_reply = f"已放弃：{abandoned_task}。"
        if next_task:
            abandon_reply = f"{abandon_reply} 下一个是「{next_task}」。请继续。"
        nudged, _ = await self.acp.prompt(
            self.unified_session_id,
            f"{abandon_reply} 请用一句自然中文回复用户。",
            trace_tag="todo_nudge",
        )
        await self._todo_emit_reply((reply or nudged or abandon_reply).strip(), from_user, context_token)
        return True


# 注册到 dispatcher 表（dispatcher 通过 _TOOL_HANDLERS 查表执行）
register_tool_handler(TOOL_TODO_MERGE_NEW_ITEMS, TodoCoachMixin._coach_merge_new_items)
register_tool_handler(TOOL_TODO_DONE_CURRENT, TodoCoachMixin._coach_done_current)
register_tool_handler(TOOL_TODO_NOT_DONE, TodoCoachMixin._coach_not_done)
register_tool_handler(TOOL_TODO_NEXT, TodoCoachMixin._coach_next)
register_tool_handler(TOOL_TODO_REORDER, TodoCoachMixin._coach_reorder)
register_tool_handler(TOOL_TODO_REORDER_CONFIRM, TodoCoachMixin._coach_reorder_confirm)
register_tool_handler(TOOL_TODO_SKIP_CURRENT, TodoCoachMixin._coach_skip_current)
register_tool_handler(TOOL_TODO_ABANDON_CURRENT, TodoCoachMixin._coach_abandon_current)
