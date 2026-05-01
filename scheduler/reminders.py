"""今日日志提醒：索引与轻量调度"""

import asyncio
import heapq
from datetime import datetime

from utils.log_sync import get_log_path, mark_reminder_done, parse_reminders_from_log


def build_today_reminder_heap(handler):
    """从今日日志提取未完成提醒，构建最小堆（按触发时间）"""
    vault = handler.cfg["vault"]
    log_path = get_log_path(vault["root"], vault["daily_log_dir"])
    today = datetime.now().date()
    heap = []
    for r in parse_reminders_from_log(log_path):
        if r["done"]:
            continue
        try:
            hh, mm = r["time"].split(":")
            due_dt = datetime.combine(today, datetime.min.time()).replace(
                hour=int(hh), minute=int(mm)
            )
        except Exception:
            continue
        rid = f"{today.isoformat()}:{r['line']}:{r['time']}"
        heapq.heappush(heap, (due_dt, rid, r))
    return log_path, heap


async def remind_check_loop(handler):
    """轻量调度器：只关注今日日志，睡眠到最近到期提醒"""
    last_day = None
    reminder_heap = []
    log_path = None

    while True:
        try:
            now_dt = datetime.now()
            today = now_dt.date()

            if last_day != today or handler._reminder_refresh.is_set() or not reminder_heap:
                log_path, reminder_heap = build_today_reminder_heap(handler)
                handler._reminder_refresh.clear()
                last_day = today
                print(f"[Bot] 提醒索引已加载: {len(reminder_heap)} 条，日期={today.isoformat()}")

            if not reminder_heap:
                await handler._reminder_refresh.wait()
                continue

            next_due_dt = reminder_heap[0][0]
            sleep_seconds = max((next_due_dt - now_dt).total_seconds(), 0.0)

            try:
                await asyncio.wait_for(handler._reminder_refresh.wait(), timeout=sleep_seconds)
                continue
            except asyncio.TimeoutError:
                pass

            now_dt = datetime.now()
            while reminder_heap and reminder_heap[0][0] <= now_dt:
                _, rid, r = heapq.heappop(reminder_heap)
                if rid in handler._reminded_ids:
                    continue

                msg = f"⏰ 提醒（{r['time']}）：{r['text']}"
                if handler.wx._context_tokens:
                    last_user, last_token = list(handler.wx._context_tokens.items())[-1]
                    await handler.wx.send_text(msg, last_user, last_token)
                else:
                    await handler.wx.send_text(msg, handler.wx.user_id, "")
                print(f"[Bot] ⏰ 提醒触发: {r['text']}")

                ok = mark_reminder_done(log_path, r["line"])
                if ok:
                    handler._reminded_ids.add(rid)
                    print(f"[Bot] ⏰ 提醒已标记完成: {r['text']}")
                else:
                    print(f"[Bot] ⏰ 提醒标记失败: {r['text']}")

        except Exception as e:
            print(f"[Bot] 提醒检查错误: {e}")
            await asyncio.sleep(3)
