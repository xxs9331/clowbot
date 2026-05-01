"""每日日志轮转：把超过保留期的 flow-*/reasoning-*.log 移入 logs/archive/。

策略：
- 每天 03:00 触发（避开 02:00 自动归档协程的窗口，避免抢锁竞写）
- 保留窗口 RETAIN_DAYS（默认 30 天）
- 仅处理 logs/ 根下符合命名 `flow-YYYY-MM-DD.log` / `reasoning-YYYY-MM-DD.log` 的文件
- 移到 logs/archive/，同名覆盖（重复运行幂等）
- 异常吞掉（不让轮转崩了主循环）
"""

from __future__ import annotations

import asyncio
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from config import LOG_DIR

RETAIN_DAYS = 30
ARCHIVE_DIR = LOG_DIR / "archive"

_LOG_NAME_RE = re.compile(r"^(flow|reasoning)-(\d{4})-(\d{2})-(\d{2})\.log$")


def rotate_once(now: datetime | None = None, retain_days: int = RETAIN_DAYS) -> int:
    """扫一遍 logs/，把过期文件搬到 logs/archive/。返回搬运数量。"""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=retain_days)).date()
    if not LOG_DIR.exists():
        return 0
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for entry in LOG_DIR.iterdir():
        if not entry.is_file():
            continue
        m = _LOG_NAME_RE.match(entry.name)
        if not m:
            continue
        try:
            file_date = datetime(int(m.group(2)), int(m.group(3)), int(m.group(4))).date()
        except ValueError:
            continue
        if file_date >= cutoff:
            continue
        target = ARCHIVE_DIR / entry.name
        try:
            if target.exists():
                target.unlink()
            shutil.move(str(entry), str(target))
            moved += 1
        except Exception as e:  # noqa: BLE001
            print(f"[log-rotate] move failed {entry.name}: {e}")
    if moved:
        print(f"[log-rotate] archived {moved} file(s) older than {retain_days}d")
    return moved


async def log_rotation_loop(retain_days: int = RETAIN_DAYS) -> None:
    """每天 03:00 跑一次 rotate_once；启动时也立刻跑一次。"""
    rotate_once(retain_days=retain_days)
    while True:
        now = datetime.now()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        try:
            await asyncio.sleep(wait_sec)
            rotate_once(retain_days=retain_days)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[log-rotate] loop error: {e}")
            await asyncio.sleep(60)
