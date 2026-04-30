"""自然语言意图识别 — 从聊天消息中提取提醒/待办意图

支持自然语言：
  "提醒我7点半上班"       → 提醒
  "晚上11点提醒我买东西"  → 提醒
  "明天8点叫我起床"       → 提醒
  "记个待办 买菜"         → 待办
  "帮我记一下：买牛奶"    → 待办
  "别忘了买牛奶"          → 待办
"""

import re
from datetime import datetime, timedelta
from utils.reminders import Reminder

# ─── 意图类型 ───
INTENT_REMIND = "remind"
INTENT_TODO = "todo"
INTENT_QUERY_TODO = "query_todo"
INTENT_QUERY_REMIND = "query_remind"
INTENT_NONE = "none"

# ─── 提醒意图匹配 ───
REMIND_PATTERNS = [
    # "提醒我7:30上班" / "提醒我 7:30 上班"
    re.compile(r"提醒[我他她]?\s*(.{1,30}?)[\s新明后今晚早天工作日]?(\d{1,2}[:点]\d{0,2})\s*(.*)"),
    # "7:30提醒我上班" / "7点半 提醒我上班"
    re.compile(r"(\d{1,2}[:点]\d{0,2}分?半?)\s*提醒[我他她]?\s*(.*)"),
    # "晚上11点提醒我" / "今晚9点提醒我"
    re.compile(r"(今晚|明晚|明早|今早|明天|后天|今晚)?\s*(\d{1,2}[:点]\d{0,2}分?半?)\s*提醒[我他她]?\s*(.*)"),
    # "叫我7点半起床" / "叫我明天8点"
    re.compile(r"叫[我他她醒起床起]\s*(.{0,10}?)(\d{1,2}[:点]\d{0,2}分?半?)\s*(.*)"),
    # "别忘了明天9点开会"
    re.compile(r"别忘了?\s*(.{0,10}?)(\d{1,2}[:点]\d{0,2}分?半?)\s*(.*)"),
]

# ─── 待办意图匹配 ───
TODO_KEYWORDS = [
    "记个待办", "记个代办", "加个待办", "加个代办",
    "待办", "代办", "todo",
    "记一下", "帮我记", "记个", "加个任务",
    "别忘了", "别忘", "记得", "要记得",
]


def detect_intent(text: str) -> tuple[str, object]:
    """检测用户消息意图

    返回 (intent_type, data):
      - (INTENT_REMIND, Reminder对象)
      - (INTENT_TODO, 待办文本字符串)
      - (INTENT_QUERY_TODO, None)     → 查看待办列表
      - (INTENT_QUERY_REMIND, None)   → 查看提醒列表
      - (INTENT_NONE, None)
    """
    text = text.strip()
    if not text:
        return INTENT_NONE, None

    # ─── 查询类意图（优先） ───
    query_todo_kws = ["查看待办", "看待办", "待办列表", "待办清单", "有什么待办",
                       "有什么事", "要做什么", "待办呢", "todo list", "/todo"]
    query_remind_kws = ["查看提醒", "看提醒", "提醒列表", "有什么提醒", "提醒呢",
                         "/remind list"]

    for kw in query_todo_kws:
        if kw in text.lower():
            return INTENT_QUERY_TODO, None

    for kw in query_remind_kws:
        if kw in text.lower():
            return INTENT_QUERY_REMIND, None

    # ─── 先检查提醒意图（优先级高） ───
    # 关键词触发：包含"提醒"或"叫我"或"别忘了"+时间
    has_remind_keyword = any(kw in text for kw in ["提醒", "叫我", "别忘了"])
    has_time = bool(re.search(r"\d{1,2}[:点]\d{0,2}", text))

    if has_remind_keyword and has_time:
        reminder = parse_natural_remind(text)
        if reminder:
            return INTENT_REMIND, reminder

    # ─── 检查待办意图 ───
    for kw in TODO_KEYWORDS:
        if kw in text:
            # 提取待办内容：去掉关键词前缀
            idx = text.index(kw)
            todo_text = text[idx + len(kw):].strip()
            # 去掉冒号、破折号等分隔符
            todo_text = re.sub(r'^[:：\s—\-]+', '', todo_text).strip()
            if todo_text:
                return INTENT_TODO, todo_text
            break

    return INTENT_NONE, None


def parse_natural_remind(text: str) -> Reminder | None:
    """从自然语言中解析提醒时间

    支持：
      "提醒我7点半上班"        → 今天7:30（已过则明天）
      "7:30提醒我上班"         → 今天7:30
      "明天8点叫我起床"         → 明天8:00
      "今晚11点提醒我买裤子"    → 今天23:00
      "每天7点提醒我吃药"       → 每日重复
      "别忘了明天9点开会"       → 明天9:00
    """
    from utils.reminders import parse_remind_command

    now = datetime.now()
    original = text

    # ─── 尝试用 /remind 的解析器 ───
    # 先看看是否能直接被 parse_remind_command 处理
    # 格式不太兼容，需要先提取时间和文本

    # ─── 提取重复模式 ───
    repeat = ""
    if "每天" in text or "每日" in text:
        repeat = "daily"
        text = text.replace("每天", "").replace("每日", "")
    elif "工作日" in text:
        repeat = "weekdays"
        text = text.replace("工作日", "")

    # ─── 提取日期修饰 ───
    base_date = now
    date_offset = 0

    if "明天" in text:
        date_offset = 1
        text = text.replace("明天", "")
    elif "后天" in text:
        date_offset = 2
        text = text.replace("后天", "")
    elif "今晚" in text:
        text = text.replace("今晚", "")
    elif "明晚" in text:
        date_offset = 1
        text = text.replace("明晚", "")
    elif "今早" in text:
        text = text.replace("今早", "")

    base_date = now + timedelta(days=date_offset)

    # ─── 提取时间 ───
    # 匹配 HH:MM 或 H点MM分 或 H点半
    time_match = re.search(r"(\d{1,2})[:点](\d{1,2})分?", text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        remaining = text[:time_match.start()] + text[time_match.end():]
    else:
        # 匹配 H点半 / H点
        time_match = re.search(r"(\d{1,2})点半?", text)
        if time_match:
            hour = int(time_match.group(1))
            minute = 30 if "半" in time_match.group(0) else 0
            remaining = text[:time_match.start()] + text[time_match.end():]
        else:
            return None  # 找不到时间，无法解析

    # ─── 提取提醒文本 ───
    # 去掉"提醒我""叫我""别忘了"等前缀词
    reminder_text = remaining.strip()
    for prefix in ["提醒我", "提醒", "叫我", "叫我起床", "别忘了", "别忘", "记得", "要记得", "提醒一下"]:
        if reminder_text.startswith(prefix):
            reminder_text = reminder_text[len(prefix):].strip()

    # 也去掉前面的描述词
    reminder_text = re.sub(r"^(快到|大约|大概|差不多)\s*", "", reminder_text)

    if not reminder_text:
        reminder_text = "提醒"

    # ─── 处理"今晚"暗示晚上的时间 ───
    if "今晚" in original or "明晚" in original or "晚" in original[:5]:
        if hour < 12:
            hour += 12  # "晚上7点" → 19点

    # ─── 构造提醒 ───
    trigger_at = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # 如果时间已过且不是指定了"明天"等，推到明天
    if trigger_at <= now and not repeat and date_offset == 0:
        trigger_at += timedelta(days=1)

    return Reminder(text=reminder_text, trigger_at=trigger_at, repeat=repeat)