"""
长期记忆的数据模型

★ 一条记忆长什么样

    MemoryItem(
        key="output_format",                    # 去重键（见下）
        value="报告一律用 PPT 形式交付",          # 喂给 Prompt 的那句话
        category="output_format",
        confidence=0.95,
        evidence="记住：报告要用 PPT",           # 用户原话，用于追溯与人工审计
        source="explicit",                      # explicit / inferred / manual
    )

★ 为什么 `key` 按「类别」而不是按「内容」派生

自动抽取按类别去重 —— 同一类别只保留最新一条，旧值进 `superseded` 留痕。

如果按内容派生 key，用户的偏好一变就会**两条并存**：
「报告用 PPT」和「报告改用 Word」会被同时注入 Prompt，
模型收到一对互相矛盾的指令，输出变得不可预测 —— 这比漏记一条偏好更糟。

所以自动抽取走保守路线（不制造矛盾）；需要并存多条同类偏好时，
走**手动添加**接口，那里由调用方显式提供 key，不受此限制。

★ 为什么 `evidence` 必须存原话

「我为什么被记住了这个？」是记忆功能最容易被质疑的点。
只存提炼后的 value，用户无法判断系统是不是误解了他的意思；
存下原话，界面上就能展示「因为你说过：『记住：报告要用 PPT』」，
用户可以直接删掉它。可解释性在这里不是加分项，是功能的一部分。

★ 软删除（active / inactive_reason）—— 为什么不真删

参考 Mem0 的 DELETE 判定：用户说「我不要 X 了」时，旧偏好应当失效。
但记忆文件是唯一事实来源，硬删会让「系统学错过什么」失去痕迹 ——
而审计恰恰是记忆功能公信力的来源。所以否定的记忆只打 `active=False`
标记（注入时跳过、控制台可见），upsert 同 key 会自然复活它。
"""


from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

# 记忆类别（key 由此派生，决定去重粒度）
CATEGORIES: Dict[str, str] = {
    "output_format": "输出格式",
    "language": "语言",
    "tone": "语气风格",
    "length": "详略程度",
    "workflow": "工作方式",
    "tooling": "工具偏好",
    "schedule": "时间安排",
    "identity": "身份信息",
    "relationship": "关系与称呼",
    "life": "生活信息",
    "other": "其他",
}

#: 「关于用户本人」的类别：这部分不进通用偏好块，而是作为关系记忆单独注入。
#: 通用偏好块讲的是「怎么干活」，这里讲的是「他是谁」—— 混在一起会让
#: 模型把「生日 10 月 12 日」当成一条工作约定去遵守。
RELATIONSHIP_CATEGORIES = frozenset({"relationship", "life"})

#: 关系记忆的固定键：一眼能看出记的是哪一类事实，也避免同类事实并存。
RELATIONSHIP_KEYS = (
    "address",
    "birthday",
    "important_dates",
    "routine",
    "likes",
    "current_focus",
)

#: 键 → 注入时显示的名字
RELATIONSHIP_KEY_LABELS: Dict[str, str] = {
    "address": "称呼",
    "birthday": "生日",
    "important_dates": "重要日子",
    "routine": "作息",
    "likes": "喜欢",
    "current_focus": "最近在忙",
}

#: 键别名：模型有时会自己发明键名，落库前归一到固定键
_RELATIONSHIP_KEY_ALIASES: Dict[str, str] = {
    "name": "address",
    "nickname": "address",
    "称呼": "address",
    "生日": "birthday",
    "纪念日": "important_dates",
    "重要日子": "important_dates",
    "作息": "routine",
    "喜好": "likes",
    "爱好": "likes",
    "最近": "current_focus",
    "当前关注": "current_focus",
}

# 来源：三者的可信度与可编辑性不同，界面上要能区分
SOURCES: Dict[str, str] = {
    "explicit": "用户明确要求",
    "inferred": "系统推断（可能不准）",
    "manual": "手动添加",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_key(raw: str) -> str:
    """
    规范化去重键：小写、去空白与中英文标点。

    ⚠️ 刻意**保留下划线**：键里会用到 `output_format` / `tone_avoid` 这类
    合成形式，把下划线也当标点吃掉会让它们塌成同一个键。
    """
    key = re.sub(r"[\s\-/、，。；：！？,.;:!?]+", "", (raw or "").strip().lower())
    return key or "other"


#: 值 → 固定键的兜底判定（模型没给合法键时用）
_RELATIONSHIP_KEY_PATTERNS = (
    ("birthday", re.compile(r"生日|生辰|出生")),
    ("important_dates", re.compile(r"纪念日|周年|重要日子|结婚")),
    ("address", re.compile(r"叫我|称呼|昵称|小名")),
    ("routine", re.compile(r"作息|起床|睡觉|上班|下班|通勤|早起|熬夜")),
    ("current_focus", re.compile(r"最近|这阵子|目前|正在|筹备|忙于")),
)


def guess_relationship_key(value: str) -> str:
    """按内容猜关系记忆的固定键；认不出时归到「喜欢」"""
    text = str(value or "")
    for key, pattern in _RELATIONSHIP_KEY_PATTERNS:
        if pattern.search(text):
            return key
    return "likes"


def normalize_relationship_key(raw_key: str, value: str = "") -> str:
    """把任意键名归一到六个固定键之一"""
    key = normalize_key(raw_key)
    if key in RELATIONSHIP_KEYS:
        return key
    alias = _RELATIONSHIP_KEY_ALIASES.get(key) or _RELATIONSHIP_KEY_ALIASES.get(
        (raw_key or "").strip()
    )
    return alias or guess_relationship_key(value)


def relationship_label(key: str) -> str:
    return RELATIONSHIP_KEY_LABELS.get(key, key)


@dataclass
class MemoryItem:
    """一条长期偏好"""

    key: str
    value: str
    category: str = "other"
    confidence: float = 0.8
    evidence: str = ""
    source: str = "explicit"
    created_at: str = ""
    updated_at: str = ""
    thread_id: str = ""
    # 被本条取代的历史值（最近的在前），保留上限由 store 控制
    superseded: List[str] = field(default_factory=list)
    # 软删除标记：False 表示已被后续记忆否定（注入跳过，控制台可见，可复活）
    active: bool = True
    inactive_reason: str = ""
    # 记忆可及性与事实可信度分开：时间会淡化记忆，不会把真事实变成假事实。
    stability_days: float = 0.0
    last_reinforced_at: str = ""
    reinforcement_count: int = 0
    last_reinforced_event: str = ""
    pinned: bool = False

    def __post_init__(self) -> None:
        self.key = normalize_key(self.key)
        self.category = self.category if self.category in CATEGORIES else "other"
        if self.category in RELATIONSHIP_CATEGORIES and not self.key.endswith("_avoid"):
            self.key = normalize_relationship_key(self.key, self.value)
        self.source = self.source if self.source in SOURCES else "explicit"
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if not self.created_at:
            self.created_at = now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at
        from app.memory.retention import initial_stability

        if not math.isfinite(self.stability_days) or self.stability_days <= 0:
            self.stability_days = initial_stability(self.source, self.key)
        self.reinforcement_count = max(0, self.reinforcement_count)
        if not self.last_reinforced_at:
            self.last_reinforced_at = self.updated_at

    @property
    def id(self) -> str:
        """
        对外标识。

        直接用 key 作 id 即可：key 已是规范化且唯一的，
        额外造一个随机 id 只会让「哪个 key 对应哪条」多一层映射。
        """
        return self.key

    @property
    def category_label(self) -> str:
        return CATEGORIES.get(self.category, CATEGORIES["other"])

    @property
    def source_label(self) -> str:
        return SOURCES.get(self.source, self.source)

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "category": self.category,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "thread_id": self.thread_id,
            "superseded": list(self.superseded),
            "active": self.active,
            "inactive_reason": self.inactive_reason,
            "stability_days": self.stability_days,
            "last_reinforced_at": self.last_reinforced_at,
            "reinforcement_count": self.reinforcement_count,
            "last_reinforced_event": self.last_reinforced_event,
            "pinned": self.pinned,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "MemoryItem":
        return cls(
            key=str(data.get("key") or data.get("id") or "other"),
            value=str(data.get("value", "")),
            category=str(data.get("category", "other")),
            confidence=float(data.get("confidence", 0.8) or 0.8),
            evidence=str(data.get("evidence", "")),
            source=str(data.get("source", "explicit")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            thread_id=str(data.get("thread_id", "")),
            superseded=[str(s) for s in (data.get("superseded") or [])][:10],
            # 旧版文件没有 active 字段 —— 缺省视为有效（向后兼容）
            active=bool(data.get("active", True)),
            inactive_reason=str(data.get("inactive_reason", "")),
            stability_days=float(data.get("stability_days", 0) or 0),
            last_reinforced_at=str(data.get("last_reinforced_at") or ""),
            reinforcement_count=int(data.get("reinforcement_count", 0) or 0),
            last_reinforced_event=str(data.get("last_reinforced_event") or ""),
            pinned=bool(data.get("pinned", False)),
        )


# Mem0 式判定动作（推断路径用）。add=新偏好 update=修正旧偏好
# delete=否定旧偏好（target_key 必填） noop=已知无需动作
ACTIONS: Dict[str, str] = {
    "add": "新增",
    "update": "修正",
    "delete": "否定",
    "noop": "已知",
    "reinforce": "回忆强化",
}


@dataclass
class MemoryCandidate:
    """抽取结果（尚未落库）—— 与 MemoryItem 分开，是为了让抽取逻辑可以独立单测"""

    key: str
    value: str
    category: str = "other"
    confidence: float = 0.8
    evidence: str = ""
    source: str = "explicit"
    # 推断路径的判定动作；显式路径恒为 add（用户亲口说的，不存在「已知」）
    action: str = "add"
    # update / delete 的作用对象（现有记忆的 key）
    target_key: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            self.action = "add"
        self.target_key = normalize_key(self.target_key) if self.target_key else ""

    def to_item(self, thread_id: str = "") -> MemoryItem:
        return MemoryItem(
            key=self.key,
            value=self.value,
            category=self.category,
            confidence=self.confidence,
            evidence=self.evidence,
            source=self.source,
            thread_id=thread_id,
        )


__all__ = [
    "ACTIONS",
    "CATEGORIES",
    "RELATIONSHIP_CATEGORIES",
    "RELATIONSHIP_KEYS",
    "RELATIONSHIP_KEY_LABELS",
    "SOURCES",
    "MemoryItem",
    "MemoryCandidate",
    "guess_relationship_key",
    "normalize_key",
    "normalize_relationship_key",
    "now_iso",
    "relationship_label",
]
