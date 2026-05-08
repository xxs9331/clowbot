"""今日日志本地读取、简报、防抖与 LLM 兜底"""

import re
import time
from pathlib import Path

from acp.opencode_client import build_system_prompt
from config import _log_reasoning
from utils.log_sync import get_log_path
from utils.section_reader import extract_section_text

# 中文数字 → int（仅处理常见「一～十、两」；解析失败交给正则 \d+ 或默认 3）
_CN_COUNT_ONE_DIGIT = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


class LocalViewMixin:
    def _get_today_log_path(self) -> Path:
        vault = self.cfg["vault"]
        return get_log_path(vault["root"], vault["daily_log_dir"])

    @staticmethod
    def _extract_section(content: str, emoji: str, title: str) -> str:
        """薄包装：保持旧接口（含 H2 标题的整段字符串），实现交由 section_reader。"""
        return extract_section_text(content, emoji, title, include_heading=True)

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

    @staticmethod
    def _parse_recent_bullet_count(text: str) -> int:
        """从用户话里抽「几条」数量，默认 3，上限 20。"""
        raw = (text or "").strip()
        m = re.search(r"(\d{1,2})\s*条", raw)
        if m:
            return max(1, min(20, int(m.group(1))))
        m2 = re.search(r"([一二两三四五六七八九十]+)\s*条", raw)
        if m2:
            s = m2.group(1)
            if len(s) == 1 and s in _CN_COUNT_ONE_DIGIT:
                return max(1, min(20, _CN_COUNT_ONE_DIGIT[s]))
        return 3

    @staticmethod
    def _extract_search_keywords(text: str) -> list[str]:
        """从查询文本中提取搜索关键词，去掉包装词和标点"""
        raw = (text or "").strip()
        for kw in [
            "查一下", "查一查", "查查", "查找", "搜索", "搜一下",
            "找一下", "找一找", "找找", "回忆一下", "回想一下",
            "相关的记忆", "相关的记录", "的记忆", "的记录",
            "相关", "一下", "最近", "看看", "看下", "说说",
            "讲讲", "有没有", "哪些", "几条",
        ]:
            raw = raw.replace(kw, " ")
        words = re.split(r"[\s,，。！？、]+", raw)
        return [w for w in words if len(w) >= 2]

    def _compose_local_view_body(
        self, kind: str, content: str, user_text: str = ""
    ) -> str:
        if kind == "log":
            return content.strip() if content.strip() else "今日日志为空"
        if kind == "brief":
            return self._build_local_daily_brief(content)
        if kind == "record_recent":
            n = self._parse_recent_bullet_count(user_text)
            keywords = self._extract_search_keywords(user_text)
            body = extract_section_text(content, "📝", "记录", include_heading=False)
            bullets: list[str] = []
            for line in (body or "").splitlines():
                s = line.strip()
                if s.startswith("- "):
                    bullets.append(s)
            if not bullets:
                return "今日「📝 记录」里还没有条目。"
            # 有关键词 → 过滤；无关键词 → 返回最近 N 条
            if keywords:
                matched = [b for b in bullets if any(kw in b for kw in keywords)]
                if matched:
                    head = f"找到 {len(matched)} 条相关记录："
                    return head + "\n" + "\n".join(matched[-n:])
                else:
                    return f"今日记录中没有找到「{' '.join(keywords)}」相关的内容。"
            tail = bullets[-n:]
            head = f"今日记录（最近 {len(tail)} 条）："
            return head + "\n" + "\n".join(tail)
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

    async def _send_local_today_view(
        self,
        kind: str,
        to_user: str,
        context_token: str = "",
        *,
        user_text: str = "",
    ) -> str:
        """本地读取今日日记。返回 ok（含正常空内容）或 error（文件不存在/读失败）。"""
        t0 = time.perf_counter()
        log_path = self._get_today_log_path()
        cache_key = (to_user, kind)

        if kind not in ("log", "record", "record_recent", "remind", "todo", "brief"):
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

        msg = self._compose_local_view_body(kind, content, user_text=user_text)
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
                "record_recent": "「记录」节末尾若干条",
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
        status = await self._send_local_today_view(
            kind, to_user, context_token, user_text=user_text
        )
        if status == "error":
            await self._local_view_llm_fallback(kind, to_user, context_token, user_text)
