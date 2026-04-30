"""自然语言意图识别 — 从聊天消息中提取提醒/待办/查询意图

数据全在日志文件里，这里只识别意图，不做存储。
"""

import re
from datetime import datetime

# ─── 意图类型 ───
INTENT_REMIND = "remind"        # 设置提醒
INTENT_TODO = "todo"            # 添加待办
INTENT_QUERY_TODO = "query_todo"    # 查看待办
INTENT_QUERY_REMIND = "query_remind"  # 查看提醒
INTENT_NONE = "none"


def detect_intent(text: str) -> tuple[str, str]:
    """检测用户消息意图

    返回 (intent_type, data):
      - (INTENT_REMIND, "07:30 上班")     → 让 AI 写入日志提醒节
      - (INTENT_TODO, "买牛奶")            → 让 AI 写入日志待办节
      - (INTENT_QUERY_TODO, "")            → 读日志待办节返回
      - (INTENT_QUERY_REMIND, "")          → 读日志提醒节返回
      - (INTENT_NONE, "")                  → 走正常生活日志流程
    """
    text = text.strip()
    if not text:
        return INTENT_NONE, ""

    # ─── 查询类意图（优先） ───
    query_todo_kws = ["查看待办", "看待办", "待办列表", "待办清单", "有什么待办",
                       "有什么事", "要做什么", "今天的待办", "今日待办"]
    query_remind_kws = ["查看提醒", "看提醒", "提醒列表", "有什么提醒", "今天的提醒", "今日提醒"]

    for kw in query_todo_kws:
        if kw in text:
            return INTENT_QUERY_TODO, ""

    for kw in query_remind_kws:
        if kw in text:
            return INTENT_QUERY_REMIND, ""

    # ─── 提醒意图 ───
    # "提醒我7:30上班" / "7:30提醒我上班" / "今晚9点提醒我" / "叫我明天8点起床"
    has_remind_keyword = any(kw in text for kw in ["提醒", "叫我", "别忘了", "别忘"])
    has_time = _extract_time_str(text) is not None

    if has_remind_keyword and has_time:
        reminder_text = _extract_remind_text(text)
        return INTENT_REMIND, reminder_text

    # ─── 待办意图 ───
    todo_kws = ["记个待办", "记个代办", "加个待办", "加个代办",
                 "待办", "代办", "todo",
                 "记一下", "帮我记", "记个", "加个任务",
                 "别忘了", "别忘", "记得", "要记得"]
    for kw in todo_kws:
        if kw in text:
            idx = text.index(kw)
            todo_text = text[idx + len(kw):].strip()
            todo_text = re.sub(r'^[:：\s—\-]+', '', todo_text).strip()
            if todo_text:
                return INTENT_TODO, todo_text
            break

    return INTENT_NONE, ""


def _extract_remind_text(text: str) -> str:
    """从提醒意图中提取完整文本，供 AI 写入日志

    返回格式: "07:30：上班" / "明天 08:00：微信读书" / "每天 07:00：起床"
    解析不清时原样返回，让 AI 自己理解。
    """
    time_str = _extract_time_str(text) or ""

    # 内容：去掉时间、时段词、提醒关键词
    content = text
    content = re.sub(r"^\s*(今天|今早|今晚|明早|明晚|早上|上午|中午|下午|晚上|夜里|夜间)\s*", "", content)
    content = re.sub(
        r"(?:\d{1,2}[:：]\d{1,2}|\d{1,2}点(?:\d{1,2}分?|半)?|[零〇一二两三四五六七八九十百]{1,5}点(?:[零〇一二两三四五六七八九十百]{1,4}分?|半)?)",
        "",
        content,
        count=1,
    )
    content = re.sub(r"(提醒一下|提醒我|提醒|叫我|别忘了|别忘|要记得|记得)", "", content)
    content = re.sub(r"^\s*[:：,，。\s-]+", "", content).strip()
    content = content.strip("，。、：: ") or "提醒"

    if time_str:
        return f"{time_str}：{content}"
    return content


def _extract_time_str(text: str) -> str | None:
    """从文本中提取时间并归一化为 HH:MM"""
    period = _detect_period_hint(text)

    m = re.search(r"(\d{1,2})[:：](\d{1,2})", text)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2))
        h = _apply_period_hint(h, period)
        if 0 <= h <= 23 and 0 <= mm <= 59:
            return f"{h:02d}:{mm:02d}"

    m = re.search(r"(\d{1,2})点(?:([0-5]?\d)分?|半)?", text)
    if m:
        h = int(m.group(1))
        h = _apply_period_hint(h, period)
        if 0 <= h <= 23:
            token = m.group(0)
            if "半" in token:
                mm = 30
            elif m.group(2) is not None:
                mm = int(m.group(2))
            else:
                mm = 0
            if 0 <= mm <= 59:
                return f"{h:02d}:{mm:02d}"

    m = re.search(r"([零〇一二两三四五六七八九十百]{1,5})点(?:([零〇一二两三四五六七八九十百]{1,4})分?|半)?", text)
    if m:
        h = _cn_num_to_int(m.group(1))
        h = _apply_period_hint(h, period) if h is not None else None
        if h is None or not (0 <= h <= 23):
            return None
        if "半" in m.group(0):
            mm = 30
        elif m.group(2):
            mm = _cn_num_to_int(m.group(2))
            if mm is None:
                return None
        else:
            mm = 0
        if 0 <= mm <= 59:
            return f"{h:02d}:{mm:02d}"
    return None


def _detect_period_hint(text: str) -> str | None:
    if any(k in text for k in ["晚上", "今晚", "夜里", "夜间", "凌晨"]):
        return "night"
    if any(k in text for k in ["下午", "傍晚"]):
        return "afternoon"
    if any(k in text for k in ["中午"]):
        return "noon"
    if any(k in text for k in ["早上", "今早", "上午", "早晨", "清晨"]):
        return "morning"
    return None


def _apply_period_hint(hour: int, period: str | None) -> int:
    if period in ("night", "afternoon"):
        if 1 <= hour <= 11:
            return hour + 12
    if period == "noon":
        if hour == 0:
            return 12
        if 1 <= hour <= 10:
            return hour + 12
    if period == "morning" and hour == 12:
        return 0
    return hour


def _cn_num_to_int(token: str) -> int | None:
    token = token.replace("〇", "零").replace("两", "二")
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if token in digits:
        return digits[token]
    if token == "十":
        return 10
    if len(token) == 2 and token[0] == "十" and token[1] in digits:
        return 10 + digits[token[1]]
    if len(token) == 2 and token[1] == "十" and token[0] in digits:
        return digits[token[0]] * 10
    if len(token) == 3 and token[1] == "十" and token[0] in digits and token[2] in digits:
        return digits[token[0]] * 10 + digits[token[2]]
    if all(ch in digits for ch in token):
        value = 0
        for ch in token:
            value = value * 10 + digits[ch]
        return value
    return None