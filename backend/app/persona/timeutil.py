"""时间工具（persona 包内复用；与 memory.models.now_iso 同口径）"""

from __future__ import annotations

from datetime import datetime, timezone

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def local_now() -> datetime:
    """本地时区的当前时间 —— 「此刻」指用户墙上的钟，不是 UTC"""
    return datetime.now().astimezone()


def period_label(dt: datetime) -> str:
    """
    一天里的粗粒度时段。

    刻意用「早上/上午/中午/下午/晚上/深夜」这种模糊词而不是「19:42」：
    人不会精确念时间，模型拿到精确分钟反而容易生硬地报时。
    """
    hour = dt.hour
    if 5 <= hour < 9:
        return "早上"
    if 9 <= hour < 11:
        return "上午"
    if 11 <= hour < 13:
        return "中午"
    if 13 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


def format_time_line(dt: datetime) -> str:
    """「2026年10月29日 周四 · 晚上」—— 注入动态块的那一行"""
    return f"{dt.year}年{dt.month}月{dt.day}日 {_WEEKDAYS[dt.weekday()]} · {period_label(dt)}"


def _to_minutes(hhmm: str) -> int | None:
    try:
        hour, minute = hhmm.split(":")
        return int(hour) * 60 + int(minute)
    except (AttributeError, TypeError, ValueError):
        return None


def is_quiet_hours(dt: datetime, start: str, end: str) -> bool:
    """
    当前是否落在静默时段内。

    跨午夜（22:00–08:00）是常态而不是边界情况：
    起止相等视为「没有静默时段」，避免 `start <= end` 那种把整天算成静默的写法。
    """
    begin = _to_minutes(start)
    finish = _to_minutes(end)
    if begin is None or finish is None or begin == finish:
        return False
    current = dt.hour * 60 + dt.minute
    if begin < finish:
        return begin <= current < finish
    return current >= begin or current < finish


__all__ = [
    "format_time_line",
    "is_quiet_hours",
    "local_now",
    "now_iso",
    "period_label",
]
