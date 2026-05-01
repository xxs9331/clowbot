"""斜杠命令路由"""

from contextlib import suppress

from acp.opencode_client import build_system_prompt
from config import _log_reasoning


class CommandsMixin:
    async def _cmd(self, text: str, to: str, context_token: str = ""):
        cmd = text.lower().split()[0]
        if cmd in ["/help", "/帮助"]:
            await self.wx.send_text(
                "🤖 生生生活日志助手\n\n"
                "发送消息自动记录：\n"
                "  体重 68.5kg\n"
                "  跑步 5km\n"
                "  看了1小时书\n\n"
                "命令：\n"
                "/remind 7:30 上班        → 设置提醒\n"
                "/remind list             → 查看提醒\n"
                "/todo 买菜、吃药、换衣服   → 批量添加待办\n"
                "/todo done 吃药          → 完成待办\n"
                "/todo next               → 现在先做哪个\n"
                "/todo list               → 查看待办\n"
                "/today  - 查看今日日志（本地优先）\n"
                "/record - 查看今日记录节\n"
                "/brief - 今日简报（本地统计）\n"
                "/remind list - 查看提醒节\n"
                "/todo list - 查看待办节\n"
                "/stat <分类> - 7日趋势\n"
                "/status - 系统状态\n"
                "/help   - 帮助",
                to,
                context_token,
            )
        elif cmd in ["/remind", "/提醒"]:
            arg = text.strip()
            for prefix in ["/remind", "/提醒"]:
                if arg.lower().startswith(prefix):
                    arg = arg[len(prefix):].strip()
                    break
            if arg.lower() in ["list", "列表", "ls", ""]:
                await self._local_view_with_optional_llm_fallback(
                    "remind", to, context_token, text
                )
            else:
                await self.wx.set_typing(to_user=to, status=1, context_token=context_token)
                try:
                    vault = self.cfg["vault"]
                    system_prefix = build_system_prompt(
                        vault_root=vault["root"],
                        daily_log_dir=vault["daily_log_dir"],
                    )
                    prompt = (
                        f"{system_prefix}\n"
                        f"用户要设置提醒：「{arg}」\n"
                        f"请在日志的「## ⏰ 提醒」节追加。只回复确认信息。"
                    )
                    reply, _ = await self.acp.prompt(self.session_id, prompt)
                    await self.wx.send_text((reply or "").strip() or f"⏰ 已记录提醒：{arg}", to, context_token)
                    self.notify_reminder_refresh()
                except Exception as e:
                    await self.wx.send_text(f"错误: {e}", to, context_token)
                finally:
                    await self.wx.set_typing(to_user=to, status=2, context_token=context_token)

        elif cmd in ["/todo", "/待办"]:
            try:
                arg = text.strip()
                for prefix in ["/todo", "/待办"]:
                    if arg.lower().startswith(prefix):
                        arg = arg[len(prefix):].strip()
                        break
                if arg.lower() in ["list", "列表", "ls", ""]:
                    await self._local_view_with_optional_llm_fallback(
                        "todo", to, context_token, text
                    )
                elif arg.lower().startswith(("done ", "完成 ")):
                    done_text = arg.split(maxsplit=1)[1] if " " in arg else ""
                    decision_text = f"我做完了：{done_text}" if done_text else "我做完了"
                    decision = await self._llm_unified_decide(to, decision_text)
                    handled = await self._apply_unified_decision(
                        decision, to, context_token, user_text=decision_text
                    )
                    if not handled:
                        await self.wx.send_text("收到，你是想标记完成。你说下具体是哪个任务。", to, context_token)
                else:
                    decision = await self._llm_unified_decide(to, arg)
                    handled = await self._apply_unified_decision(
                        decision, to, context_token, user_text=arg
                    )
                    if not handled:
                        await self.wx.send_text("你可以直接说要做的几个小事，或问我“现在先做哪个”。", to, context_token)
            except Exception as e:
                await self.wx.send_text(f"错误: {e}", to, context_token)
            finally:
                with suppress(Exception):
                    await self.wx.set_typing(to_user=to, status=2, context_token=context_token)
        elif cmd in ["/status", "/状态"]:
            status = "🟢 运行中" if self.acp.is_running else "🔴 已停止"
            model = self.cfg["opencode"].get("model", "?")
            await self.wx.send_text(
                f"状态: {status}\n模型: {model}\n会话: {self.session_id[:12]}...",
                to,
                context_token,
            )
        elif cmd in ["/today", "/今天", "/日志"]:
            await self._local_view_with_optional_llm_fallback("log", to, context_token, text)
        elif cmd in ["/record", "/记录"]:
            await self._local_view_with_optional_llm_fallback("record", to, context_token, text)
        elif cmd in ["/brief", "/简报"]:
            await self._local_view_with_optional_llm_fallback("brief", to, context_token, text)
        elif cmd.startswith("/stat"):
            cat = text.split()[1] if len(text.split()) > 1 else ""
            if cat:
                await self.wx.set_typing(
                    to_user=to, status=1, context_token=context_token
                )
                try:
                    vault = self.cfg["vault"]
                    reply, reasoning = await self.acp.prompt(
                        self.session_id,
                        f"只回复数据，不要输出分析过程。读取 {vault['root']}/{vault['daily_log_dir']} 下最近7天的文件，提取'{cat}'分类的记录，总结趋势。",
                    )
                    if reasoning:
                        print(f"[Bot] 🧠 {reasoning[:200]}")
                        _log_reasoning(f"/stat {cat}", reasoning)
                    await self.wx.send_text(
                        reply or f"暂无{cat}数据", to, context_token
                    )
                except Exception as e:
                    await self.wx.send_text(f"错误: {e}", to, context_token)
                finally:
                    await self.wx.set_typing(
                        to_user=to, status=2, context_token=context_token
                    )
        elif cmd in ["/new", "/新会话"]:
            await self.init_session()
            await self.wx.send_text("已创建新会话", to, context_token)
