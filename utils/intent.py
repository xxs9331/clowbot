"""自然语言意图识别 — 从聊天消息中提取提醒/待办/查询意图

数据全在日志文件里，这里只识别意图，不做存储。
"""

import re
from datetime import datetime, timedelta

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
    has_time = bool(re.search(r"\d{1,2}[:点]\d{0,2}", text))

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
    original = text
    now = datetime.now()

    # 重复模式
    repeat_prefix = ""
    if "每天" in text or "每日" in text:
        repeat_prefix = "每天 "
        text = text.replace("每天", "").replace("每日", "")
    elif "工作日" in text:
        repeat_prefix = "工作日 "
        text = text.replace("工作日", "")

    # 日期修饰
    date_prefix = ""
    for word, dp in [("明晚", "明天 "), ("今晚", ""), ("明天", "明天 "), ("后天", "后天 ")]:
        if word in text:
            date_prefix = dp
            text = text.replace(word, "", 1)
            break

    # 时间
    time_match = re.search(r"(\d{1,2})[:点](\d{1,2})分?", text)
    if time_match:
        time_str = f"{int(time_match.group(1)):02d}:{int(time_match.group(2)):02d}"
    else:
        half_match = re.search(r"(\d{1,2})点半?", text)
        if half_match:
            h = int(half_match.group(1))
            m = 30 if "半" in half_match.group(0) else 0
            time_str = f"{h:02d}:{m:02d}"
        else:
            time_str = ""

    # 内容：去掉时间、关键词前缀
    content = text
    for prefix in ["提醒我", "提醒", "叫我", "别忘了", "别忘", "记得", "要记得", "提醒一下"]:
        if content.lstrip().startswith(prefix):
            content = content.lstrip()[len(prefix):].lstrip()
            break
    content = re.sub(r"^\d{1,2}[:点]\d{0,2}分?半?\s*[：:]?\s*", "", content).strip()
    content = content.strip("，。、：: ") or "提醒"

    # 组装: "每天 明天 07:30：上班"
    prefix = f"{repeat_prefix}{date_prefix}{time_str}"
    if prefix:
        return f"{prefix}：{content}"
    return content