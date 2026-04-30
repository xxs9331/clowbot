"""提醒功能模块 — 持久化存储 + 后台定时检查

支持：
  /remind 7:30 上班               → 一次性提醒（今天 7:30）
  /remind 23:30 抖音买裤子         → 一次性提醒（今天/明天 23:30）
  /remind 明天 8:00 微信读书       → 明天提醒
  /remind 每天 7:00 起床           → 每日重复
  /remind 周一 9:00 晨会           → 每周重复
  /remind list                     → 查看所有提醒
  /remind del <id>                 → 删除提醒

存储: .clawbot/reminders.json（重启不丢失）
"""

import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

REMINDERS_FILE = Path(__file__).parent / "reminders.json"

# ─── 中文星期映射 ───
WEEKDAY_CN = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6,
}


class Reminder:
    def __init__(self, text: str, trigger_at: datetime, repeat: str = "",
                 rid: str = ""):
        self.id = rid or uuid.uuid4().hex[:8]
        self.text = text
        self.trigger_at = trigger_at
        self.repeat = repeat  # "", "daily", "weekly", "weekdays"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "trigger_at": self.trigger_at.isoformat(),
            "repeat": self.repeat,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Reminder":
        return cls(
            text=d["text"],
            trigger_at=datetime.fromisoformat(d["trigger_at"]),
            repeat=d.get("repeat", ""),
            rid=d.get("id", ""),
        )

    def next_trigger(self) -> Optional["Reminder"]:
        """重复提醒：计算下一次触发时间"""
        if not self.repeat:
            return None
        if self.repeat == "daily":
            next_dt = self.trigger_at + timedelta(days=1)
        elif self.repeat == "weekly":
            next_dt = self.trigger_at + timedelta(weeks=1)
        elif self.repeat == "weekdays":
            next_dt = self.trigger_at + timedelta(days=1)
            while next_dt.weekday() >= 5:  # 跳过周末
                next_dt += timedelta(days=1)
        else:
            return None
        return Reminder(text=self.text, trigger_at=next_dt, repeat=self.repeat, rid=self.id)


def load_reminders() -> list[Reminder]:
    """从文件加载提醒列表"""
    if not REMINDERS_FILE.exists():
        return []
    try:
        data = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
        return [Reminder.from_dict(d) for d in data]
    except Exception:
        return []


def save_reminders(reminders: list[Reminder]):
    """保存提醒列表到文件"""
    REMINDERS_FILE.write_text(
        json.dumps([r.to_dict() for r in reminders], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_remind_command(text: str) -> Optional[Reminder]:
    """解析 /remind 命令，返回 Reminder 或 None

    支持格式：
      /remind 7:30 上班              → 今天 7:30（如果已过则明天）
      /remind 7:30 上班              → 今天 7:30
      /remind 19:30 下班             → 今天 19:30
      /remind 明天 8:00 微信读书     → 明天 8:00
      /remind 每天 7:00 起床         → 每日重复
      /remind 周一 9:00 晨会         → 每周重复
    """
    # 去掉 /remind 前缀
    text = text.strip()
    if text.startswith("/remind"):
        text = text[7:].strip()
    elif text.startswith("/提醒"):
        text = text[3:].strip()
    
    if not text:
        return None

    now = datetime.now()
    repeat = ""
    base_date = now

    # ─── 重复模式 ───
    if text.startswith("每天") or text.startswith("每日"):
        repeat = "daily"
        text = text[2:].strip()
    elif text.startswith("工作日"):
        repeat = "weekdays"
        text = text[3:].strip()

    # ─── 日期修饰 ───
    weekday_match = re.match(r"(周[一二三四五六日天])\s+", text)
    if weekday_match:
        cn_day = weekday_match.group(1)
        target_wd = WEEKDAY_CN.get(cn_day)
        if target_wd is not None:
            repeat = "weekly"
            days_ahead = (target_wd - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # 如果是今天，跳到下周
            base_date = now + timedelta(days=days_ahead)
            text = text[weekday_match.end():].strip()

    if text.startswith("明天"):
        base_date = now + timedelta(days=1)
        text = text[2:].strip()
    elif text.startswith("后天"):
        base_date = now + timedelta(days=2)
        text = text[3:].strip()
    elif text.startswith("今晚"):
        # 今晚 = 今天晚上（不调整日期）
        text = text[2:].strip()
    elif text.startswith("明晚"):
        base_date = now + timedelta(days=1)
        text = text[2:].strip()

    # ─── 时间解析 ───
    # 匹配 HH:MM 或 H:MM
    time_match = re.match(r"(\d{1,2}):(\d{2})\s*(.*)", text)
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        reminder_text = time_match.group(3).strip()
    else:
        # 匹配 H点 / HH点
        time_match = re.match(r"(\d{1,2})点\s*(半|刻)?\s*(.*)", text)
        if time_match:
            hour = int(time_match.group(1))
            half = time_match.group(2)
            if half == "半":
                minute = 30
            elif half == "刻":
                minute = 15
            else:
                minute = 0
            reminder_text = time_match.group(3).strip() or time_match.group(0)
        else:
            # 匹配纯数字后面跟文字如 "7点半上班"
            time_match = re.match(r"(\d{1,2})(半|点)?\s*(.*)", text)
            if time_match:
                hour = int(time_match.group(1))
                minute = 30 if time_match.group(2) == "半" else 0
                reminder_text = time_match.group(3).strip()
            else:
                return None

    if not reminder_text:
        reminder_text = "提醒"  # 兜底

    trigger_at = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # 如果不是指定了"明天"等，且时间已过，推到明天
    if trigger_at <= now and not repeat and base_date == now:
        # 今天已过 → 推到明天（除非是重复提醒）
        if repeat != "daily" and repeat != "weekly" and repeat != "weekdays":
            trigger_at += timedelta(days=1)

    return Reminder(text=reminder_text, trigger_at=trigger_at, repeat=repeat)


def format_reminder(r: Reminder) -> str:
    """格式化提醒为可读字符串"""
    now = datetime.now()
    delta = r.trigger_at - now
    time_str = r.trigger_at.strftime("%H:%M")
    
    if r.repeat == "daily":
        repeat_str = "📅 每天"
    elif r.repeat == "weekly":
        repeat_str = "📅 每周"
    elif r.repeat == "weekdays":
        repeat_str = "📅 工作日"
    else:
        repeat_str = "⏰"
    
    if delta.total_seconds() < 0:
        when = "已过期"
    elif delta.total_seconds() < 3600:
        when = f"{int(delta.total_seconds()/60)}分钟后"
    elif delta.total_seconds() < 86400:
        when = f"{int(delta.total_seconds()/3600)}小时后"
    else:
        when = f"{delta.days}天后"
    
    return f"{repeat_str} {time_str} {r.text}（{when}）"


def check_due_reminders(reminders: list[Reminder], now: datetime = None) -> list[Reminder]:
    """返回已到期的提醒列表"""
    now = now or datetime.now()
    due = []
    for r in reminders:
        if r.trigger_at <= now:
            due.append(r)
    return due


def remove_fired_reminders(reminders: list[Reminder], fired: list[Reminder]) -> list[Reminder]:
    """移除已触发的提醒（重复的则更新下次触发时间）"""
    fired_ids = {r.id for r in fired}
    remaining = []
    for r in reminders:
        if r.id in fired_ids:
            # 重复提醒：计算下次触发
            next_r = r.next_trigger()
            if next_r:
                remaining.append(next_r)
            # 非重复：直接移除
        else:
            remaining.append(r)
    return remaining