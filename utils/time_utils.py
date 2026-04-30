"""时间工具：获取当前时间、格式化时间戳"""
from datetime import datetime


def now() -> datetime:
    return datetime.now()


def date_str(dt: datetime = None) -> str:
    """返回 YYYY-MM-DD"""
    dt = dt or now()
    return dt.strftime("%Y-%m-%d")


def time_str(dt: datetime = None) -> str:
    """返回 HH:MM"""
    dt = dt or now()
    return dt.strftime("%H:%M")


def datetime_str(dt: datetime = None) -> str:
    """返回 YYYY-MM-DD HH:MM"""
    dt = dt or now()
    return dt.strftime("%Y-%m-%d %H:%M")


def weekday_cn(dt: datetime = None) -> str:
    """返回周X"""
    dt = dt or now()
    return ["一", "二", "三", "四", "五", "六", "日"][dt.weekday()]


def month_dir(dt: datetime = None) -> str:
    """返回 MM 格式（用于目录名）"""
    dt = dt or now()
    return dt.strftime("%m")


def year_dir(dt: datetime = None) -> str:
    """返回 YYYY 格式（用于目录名）"""
    dt = dt or now()
    return dt.strftime("%Y")
