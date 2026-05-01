"""今日日志本地读取、简报、防抖与 LLM 兜底"""

import re
import time
from pathlib import Path

from acp.opencode_client import build_system_prompt
from config import _log_reasoning
from utils.log_sync import get_log_path


class LocalViewMixin:
    def _get_today_log_path(self) -> Path:
        vault = self.cfg["vault"]
        return get_log_path(vault["root"], vault["daily_log_dir"])

    @staticmethod
    def _extract_section(content: str, emoji: str, title: str) -> str:
        # 只在「二级标题」处结束：行首为 ## + 空格。若用 (?=##|\Z)，会在 ### 处误匹配（### 以 ## 开头）。
        pattern = rf"(##\s*(?:\d+(?:\.\d+)?\s+)?{re.escape(emoji)}\s*{re.escape(title)}.*?)(?=^## |\Z)"
        match = re.search(pattern, content, re.DOTALL | re.MULTILINE)
        return match.group(1).strip() if match else ""

    def _log_local_view_obs(
        self,
        kind: str,
        status: str,
        detail: str,
        t0: float,
        cache_hit: bool,
    ) -> None:
        ms = (time.perf_counter() - t0) * 1000
        print(
            f"[Bot] local_view kind={kind} status={status} detail={detail} "
            f"ms={ms:.1f} cache_hit={cache_hit}"
        )

    @staticmethod
    def _count_section_bullet_lines(section: str) -> int:
        if not section:
            return 0
        return sum(1 for line in section.splitlines() if line.strip().startswith("- "))

    @staticmethod
    def _count_pending_checkboxes(section: str) -> int:
        if not section:
            return 0
        return len(re.findall(r"^- \[ \]", section, re.MULTILINE))

    def _build_local_daily_brief(self, content: str) -> str:
        rec_sec = self._extract_section(content, "📝", "记录")
        rem_sec = self._extract_section(content, "⏰", "提醒")
        todo_sec = self._extract_section(content, "📋", "待办")
        n_record = self._count_section_bullet_lines(rec_sec)
        n_remind_pending = self._count_pending_checkboxes(rem_sec)
        n_todo_pending = self._count_pending_checkboxes(todo_sec)
        return (
            "今日简报\n"
            f"记录条目数：{n_record}\n"
            f"未完成提醒：{n_remind_pending}\n"
            f"未完成待办：{n_todo_pending}"
        )

    def _compose_local_view_body(self, kind: str, content: str) -> str:
        if kind == "log":
            return content.strip() if content.strip() else "今日日志为空"
        if kind == "brief":
            return self._build_local_daily_brief(content)
        if kind == "record":
            section = self._extract_section(content, "📝", "记录")
            return section if section else "今日暂无记录内容"
        if kind == "remind":
            section = self._extract_section(content, "⏰", "提醒")
            return section if section else "今日暂无提醒内容"
        if kind == "todo":
            section = self._extract_section(content, "📋", "待办")
            return section if section else "今日暂无待办内容"
        return ""

    def _detect_local_view_kind(self, text: str) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        # 简报优先（避免与「日志」子串误触）
        brief_kws = [
            "查看简报", "今日简报", "今天简报", "看下简报", "看看简报",
            "日志简报", "今日概况", "今天概况",
        ]
        if any(k in raw for k in brief_kws):
            return "brief"
        log_kws = [
            "查看今日日志", "查看今天日志", "今日日志", "今天日志",
            "查看日志", "看日志", "看下日志", "看看日志", "打开日志",
            "给我日志", "日志全文", "今日日志内容", "今天日志内容",
            "今日日记", "今天日记", "看下日记",
        ]
        if any(k in raw for k in log_kws):
            return "log"
        record_kws = [
            "查看记录", "看记录", "看下记录", "看看记录",
            "今日记录", "今天记录", "记录列表",
        ]
        if any(k in raw for k in record_kws):
            return "record"
        remind_kws = [
            "查看提醒", "看提醒", "看下提醒", "看看提醒",
            "提醒列表", "今日提醒", "今天提醒", "有什么提醒",
        ]
        if any(k in raw for k in remind_kws):
            return "remind"
        todo_kws = [
            "查看待办", "看待办", "看下待办", "看看待办",
            "待办列表", "待办清单", "今日待办", "今天待办",
            "有什么待办", "待办呢",
        ]
        if any(k in raw for k in todo_kws):
            return "todo"
        return ""

    async def _send_local_today_view(self, kind: str, to_user: str, context_token: str = "") -> str:
        """本地读取今日日记。返回 ok（含正常空内容）或 error（文件不存在/读失败）。"""
        t0 = time.perf_counter()
        log_path = self._get_today_log_path()
        cache_key = (to_user, kind)

        if kind not in ("log", "record", "remind", "todo", "brief"):
            self._log_local_view_obs(kind, "error", "bad_kind", t0, False)
            return "error"

        try:
            if not log_path.exists():
                self._log_local_view_obs(kind, "error", "missing_file", t0, False)
                return "error"
            content = log_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[Bot] local_view read error: {e}")
            self._log_local_view_obs(kind, "error", "read_exception", t0, False)
            return "error"

        msg = self._compose_local_view_body(kind, content)
        if not msg:
            self._log_local_view_obs(kind, "error", "empty_compose", t0, False)
            return "error"

        now_m = time.monotonic()
        prev = self._local_view_last.get(cache_key)
        if prev is not None:
            ts, prev_msg = prev
            if (
                now_m - ts < self._local_view_debounce_sec
                and prev_msg == msg
            ):
                await self.wx.send_text(prev_msg, to_user, context_token)
                self._log_local_view_obs(kind, "ok", "debounce_resend", t0, True)
                return "ok"

        await self.wx.send_text(msg, to_user, context_token)
        self._local_view_last[cache_key] = (now_m, msg)
        self._log_local_view_obs(kind, "ok", "sent", t0, False)
        return "ok"

    async def _local_view_llm_fallback(
        self,
        kind: str,
        from_user: str,
        context_token: str,
        user_text: str,
    ) -> None:
        """仅当本地读取异常时调用大模型尝试读文件或说明原因。"""
        await self.wx.set_typing(to_user=from_user, status=1, context_token=context_token)
        try:
            vault = self.cfg["vault"]
            log_path = self._get_today_log_path()
            system_prefix = build_system_prompt(
                vault_root=vault["root"],
                daily_log_dir=vault["daily_log_dir"],
                project_dir=vault.get("project_dir", ""),
                task_dir=vault.get("task_dir", ""),
            )
            kind_hint = {
                "log": "全文日志",
                "record": "「记录」节",
                "remind": "「提醒」节",
                "todo": "「待办」节",
                "brief": "今日简报（记录条数、未完成提醒/待办统计）",
            }.get(kind, kind)
            prompt = (
                f"{system_prefix}\n"
                f"用户想查看今日日记的本地内容，但程序读取文件失败（类型：{kind_hint}）。\n"
                f"文件路径：{log_path}\n"
                f"用户原话：「{user_text}」\n"
                "请用工具读取该文件（若存在）并给出用户需要的内容；若无法读取则说明原因。"
            )
            reply, reasoning = await self.acp.prompt(
                self.session_id, prompt, trace_tag="local_view_llm_fallback"
            )
            if reasoning:
                print(f"[Bot] 🧠 {reasoning[:200]}")
                _log_reasoning(f"[local_view_fallback:{kind}] {user_text[:80]}", reasoning)
            reply = (reply or "").strip()[: self.cfg["bot"].get("max_reply_length", 2000)]
            await self.wx.send_text(reply or "本地读取失败，请稍后重试。", from_user, context_token)
        finally:
            await self.wx.set_typing(to_user=from_user, status=2, context_token=context_token)

    async def _local_view_with_optional_llm_fallback(
        self,
        kind: str,
        to_user: str,
        context_token: str,
        user_text: str,
    ) -> None:
        status = await self._send_local_today_view(kind, to_user, context_token)
        if status == "error":
            await self._local_view_llm_fallback(kind, to_user, context_token, user_text)
