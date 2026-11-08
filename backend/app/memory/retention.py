"""遗忘与回弹的工程模型；S 是半衰期（天），不是事实的置信度。

R(t) = exp(-ln(2) * t / S)。有效回忆后 R 回到 1，S 随间隔增大；
这是受遗忘曲线和间隔学习启发的近似，不宣称复现人脑或原始实验拟合。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.memory.models import MemoryItem

CORE_KEYS = frozenset({"address", "birthday", "important_dates"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def initial_stability(source: str, key: str) -> float:
    if key.removesuffix("_avoid") == "current_focus":
        return settings.MEMORY_FOCUS_STABILITY_DAYS
    return settings.MEMORY_INITIAL_STABILITY_DAYS * {
        "inferred": 0.5, "explicit": 1.0, "manual": 3.0,
    }.get(source, 1.0)


def is_core(item: MemoryItem) -> bool:
    return item.pinned or item.category == "identity" or item.key.removesuffix("_avoid") in CORE_KEYS


def retention(item: MemoryItem, at: datetime | None = None) -> float:
    if not item.active:
        return 0.0
    if is_core(item) or not settings.MEMORY_DECAY_ENABLED:
        return 1.0
    anchor = parse_time(item.last_reinforced_at) or parse_time(item.updated_at) or parse_time(item.created_at)
    if anchor is None:
        return 1.0  # 坏时间戳不吞掉已有记忆；也不在读取时偷偷刷新时间。
    age_days = max(0.0, ((at or utc_now()) - anchor).total_seconds() / 86400)
    return math.exp(-math.log(2) * age_days / item.stability_days)


def memory_state(item: MemoryItem, at: datetime | None = None) -> str:
    if not item.active:
        return "inactive"
    if is_core(item):
        return "core"
    strength = retention(item, at)
    if strength < settings.MEMORY_RECALL_THRESHOLD:
        return "dormant"
    return "fading" if strength < 0.6 else "fresh"


def reinforce(item: MemoryItem, *, at: datetime | None = None, event_id: str = "") -> bool:
    """只强化有效条目；冷却期与事件去重防止工具循环/重复任务刷强度。"""
    if not item.active or (event_id and item.last_reinforced_event == event_id):
        return False
    now = at or utc_now()
    anchor = parse_time(item.last_reinforced_at)
    if anchor is not None and (now - anchor).total_seconds() < settings.MEMORY_REINFORCE_INTERVAL_HOURS * 3600:
        return False
    before = retention(item, now)
    growth = 1.0 + settings.MEMORY_REBOUND_GAIN * (0.25 + 0.75 * (1.0 - before))
    item.stability_days = min(settings.MEMORY_MAX_STABILITY_DAYS, item.stability_days * growth)
    item.last_reinforced_at = now.isoformat(timespec="seconds")
    item.reinforcement_count += 1
    item.last_reinforced_event = event_id
    return True


def cue_score(item: MemoryItem, query: str) -> float:
    """保守的词面线索，供沉睡记忆候选召回；匹配本身不产生强化。"""
    query = re.sub(r"\s+", "", query.lower())
    if not query:
        return 0.0
    value = re.sub(r"\s+", "", item.value.lower())
    if len(value) >= 2 and value in query:
        return 1.0
    labels = {
        "address": ("称呼", "叫什么", "叫我"), "birthday": ("生日",),
        "important_dates": ("纪念日", "周年"), "routine": ("作息", "几点起床", "几点睡"),
        "likes": ("喜好", "喜欢什么"), "current_focus": ("最近在忙", "最近忙什么"),
        "output_format": ("报告格式", "输出格式"), "language": ("语言偏好",),
        "tone": ("语气", "说话风格"), "length": ("详略", "回答长短"),
    }
    if any(label in query for label in labels.get(item.key.removesuffix("_avoid"), ())):
        return 0.8
    # 用现有中文分词器，避免“喜欢/今天”之类通用二字片段唤醒无关记忆。
    import jieba

    stop = {"喜欢", "用户", "以后", "今天", "最近", "记住", "不要", "可以", "报告", "一般", "使用", "自己", "这个", "那个", "时候"}
    tokens = {word.lower() for word in jieba.cut(value) if len(word) >= 2 and word not in stop}
    matched = [word for word in tokens if re.search(r"[\u4e00-\u9fff]", word) and word in query
               or not re.search(r"[\u4e00-\u9fff]", word) and re.search(r"(?<![a-z0-9_])" + re.escape(word) + r"(?![a-z0-9_])", query)]
    return min(0.7, 0.4 + 0.1 * len(matched)) if matched else 0.0
