"""每日凌晨自动归档昨日生活日志"""

import asyncio
from datetime import datetime, timedelta

from acp.opencode_client import OpenCodeACP


async def auto_archive_loop(acp: OpenCodeACP, config: dict, handler):
    """每天凌晨 2 点自动归档昨日生活日志到生生项目"""
    while True:
        now = datetime.now()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        print(f"[Bot] 下次自动归档: {target.strftime('%m-%d %H:%M')} ({wait/3600:.1f}h后)")

        await asyncio.sleep(wait)

        vault = config["vault"]
        yesterday = datetime.now() - timedelta(days=1)
        log_path = f"{vault['root']}/{vault['daily_log_dir']}/{yesterday.year}/{yesterday.month:02d}/{yesterday.strftime('%Y-%m-%d')}.md"

        try:
            reply, reasoning = await acp.prompt(
                handler.session_id,
                f"读取 {log_path}，按「生活日志」skill 的归档流程将记录分发到生生项目各分类文件并更新任务监控。如果文件不存在或为空，回复「无记录」。只回复一行确认。",
                trace_tag="scheduler_auto_archive",
            )
            print(f"[Bot] 自动归档: {reply or 'OK'}")
            if reasoning:
                print(f"[Bot] 🧠 {reasoning[:200]}")
        except Exception as e:
            print(f"[Bot] 自动归档失败: {e}")
