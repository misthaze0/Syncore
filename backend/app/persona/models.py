"""
人设的数据模型

★ 字段设计参考了 WorkBuddy / Kimi 的「智能体自定义」能力，收敛到四个问题：

    名字（name）        —— 界面与 Prompt 里的「我是谁」
    一句话定位（tagline）—— 「我是干什么的」
    语气风格（tone）     —— 「我怎么说话」，预设枚举 + 自定义文本
    补充说明（instructions）—— 用户自由发挥的剩余部分

★ 2026-10 人设卡扩展（对齐 ChatGPT GPTs / Coze 的角色卡形态）

    头像（avatar）      —— data URL，纯 UI 展示，不进 Prompt
    性格标签（traits）   —— 进 Prompt 的性格关键词
    开场白（greeting）   —— 新会话欢迎语，纯 UI 展示，不进 Prompt
    说话示例（examples） —— 进 Prompt 的口吻样例

刻意**不做**的东西：
- 多套人设方案并存 —— 预设（PERSONA_PRESETS）只负责填充草稿，
  同一时刻仍然只有**一份**生效的人设；多方案是把简单问题做复杂的第一步。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List

from app.persona.timeutil import now_iso

# 语气预设：key → 注入 Prompt 的一句话。
# 文案要求：说「怎么说话」而不是「是什么性格」—— 前者才能被模型直接执行。
TONE_PRESETS: Dict[str, str] = {
    "professional": "专业、严谨、条理清晰；回答偏正式，先给结论再给依据，避免口语化寒暄。",
    "friendly": "轻松、友好、表达自然，像一位靠谱的同事在沟通；保持礼貌与分寸，不端着，也不过分亲昵。",
    "concise": "极度简洁，直击要点；能用一句话说清就不用两句，不铺垫、不重复。",
    "humorous": "风趣幽默，可适度调节沟通气氛；但事实与专业内容必须准确，正式场合保持克制。",
}

TONE_CUSTOM = "custom"

#: 人设预设：只负责填充草稿，不是运行时的「模式开关」。
#: 用户点选后仍可逐项修改，保存的是改完后的具体字段。
PERSONA_PRESETS: Dict[str, Dict[str, Any]] = {
    "gentle_reliable": {
        "label": "亲和协作型",
        "description": "先对齐目标，再给出可靠的下一步",
        "tone": "friendly",
        "traits": ["亲和", "可靠"],
        "instructions": "先确认目标与约束，再给具体、可执行的下一步；不夸张承诺，不敷衍。",
        "examples": ["好，我们先对齐目标。", "先确认范围，再分步推进。"],
        "quirks": [],
    },
    "playful": {
        "label": "轻松沟通型",
        "description": "沟通轻快，正事不掉链子",
        "tone": "humorous",
        "traits": ["轻快", "务实"],
        "instructions": "语气轻快，可以适度调节气氛，但正事必须准确；不拿用户的难处开玩笑。",
        "examples": ["收到，这就安排。", "这个交给我，你先忙别的。"],
        "quirks": [],
    },
    "efficient": {
        "label": "专业干练型",
        "description": "少寒暄，直接给结论和下一步",
        "tone": "concise",
        "traits": ["干练", "严谨"],
        "instructions": "少铺垫、少寒暄，先给结论，再给必要的依据和下一步。",
        "examples": ["结论：可以，按这个方案推进。", "我查到了，重点有三条。"],
        "quirks": [],
    },
}
DEFAULT_PRESET = "efficient"

#: 主动问候只有「关」和「轻」两档 —— 没有「频繁」，那只会变成打扰。
PROACTIVE_LEVELS = {"off", "light"}

#: 人设卡各字段的规模上限 —— 都是「一张卡片放得下」的量级。
#: avatar 是 data URL：前端上传时会先缩到 256px 再编码，
#: 300K 字符（约 220KB 图）给足了余量，也挡住了「把原图直接塞进来」。
MAX_TRAITS = 8
MAX_TRAIT_LEN = 12
MAX_EXAMPLES = 6
MAX_EXAMPLE_LEN = 200
MAX_GREETING_LEN = 200
MAX_AVATAR_CHARS = 300_000
MAX_ADDRESS_LEN = 20
MAX_QUIRKS = 4
MAX_QUIRK_LEN = 16

_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _clamp(s: str, max_len: int) -> str:
    return (s or "").strip()[:max_len]


def _clamp_list(items: Any, max_items: int, max_len: int) -> List[str]:
    if not isinstance(items, list):
        return []
    out: List[str] = []
    for it in items:
        s = _clamp(str(it or ""), max_len)
        if s:
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def _normalize_hhmm(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text if _HHMM_RE.match(text) else fallback


def _clamp_int(value: Any, *, low: int, high: int | None = None, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    if high is not None:
        number = min(high, number)
    return max(low, number)


@dataclass
class PersonaProfile:
    """一份可编辑的助手人设（人设卡）"""

    name: str = "Syncore"
    tagline: str = "企业级 AI 工作台"
    # TONE_PRESETS 的 key，或 "custom"（此时读 tone_custom）
    tone: str = "professional"
    tone_custom: str = ""
    # 自定义人设说明：用户自由输入，原样注入 Prompt
    instructions: str = ""
    # ---------- 人设卡扩展（2026-10）----------
    # 头像：data URL，纯 UI（顶栏身份块 / 欢迎页 / 设置卡片），不进 Prompt
    avatar: str = ""
    # 性格标签：进 Prompt 的关键词（如「严谨」「务实」「可靠」）
    traits: List[str] = field(default_factory=list)
    # 开场白：新会话欢迎语，纯 UI，不进 Prompt
    greeting: str = ""
    # 说话示例：进 Prompt 的口吻样例（「像这样说话」）
    examples: List[str] = field(default_factory=list)
    # ---------- 说话层与记忆扩展（2026-10）----------
    # 预设 key：只用于界面回显「这套人设的底子是哪套」，不改变已编辑的字段
    preset: str = DEFAULT_PRESET
    # 助手怎么称呼用户（进 Prompt；也用于覆盖关系记忆里的 address）
    address_me: str = ""
    # 口头禅/说话小习惯（如「偶尔用～」「不把话说满」）
    quirks: List[str] = field(default_factory=list)
    # 是否把当前日期/星期/时段注入动态块
    time_awareness: bool = True
    # 主动问候级别：off / light
    proactive: str = "light"
    # 静默时段（本地时间，HH:MM；跨午夜允许）
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"
    # 两次主动问候的最小间隔与每日上限
    proactive_min_gap_hours: int = 12
    proactive_daily_max: int = 1
    # 总开关：关闭后完全不注入（前端也能改）
    enabled: bool = True
    # 作用范围：False（默认）= 只注入助理命名空间的对话；True = 普通对话也注入。
    # 人设是「那位助理是谁」，默认不该改写所有对话的语气。
    apply_globally: bool = False
    updated_at: str = ""
    # 版本字段随实例携带，由 store 写盘时统一填 —— 数据模型不感知存储细节
    _meta: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.name = _clamp(self.name, 24) or "Syncore"
        self.tagline = _clamp(self.tagline, 60)
        if self.tone not in TONE_PRESETS and self.tone != TONE_CUSTOM:
            self.tone = "professional"
        self.tone_custom = _clamp(self.tone_custom, 200)
        self.instructions = _clamp(self.instructions, 1000)
        self.avatar = (self.avatar or "").strip()[:MAX_AVATAR_CHARS]
        self.traits = _clamp_list(self.traits, MAX_TRAITS, MAX_TRAIT_LEN)
        self.greeting = _clamp(self.greeting, MAX_GREETING_LEN)
        self.examples = _clamp_list(self.examples, MAX_EXAMPLES, MAX_EXAMPLE_LEN)
        if self.preset not in PERSONA_PRESETS:
            self.preset = DEFAULT_PRESET
        self.address_me = _clamp(self.address_me, MAX_ADDRESS_LEN)
        self.quirks = _clamp_list(self.quirks, MAX_QUIRKS, MAX_QUIRK_LEN)
        self.time_awareness = bool(self.time_awareness)
        if self.proactive not in PROACTIVE_LEVELS:
            self.proactive = "light"
        self.quiet_start = _normalize_hhmm(self.quiet_start, "22:00")
        self.quiet_end = _normalize_hhmm(self.quiet_end, "08:00")
        self.proactive_min_gap_hours = _clamp_int(
            self.proactive_min_gap_hours, low=1, high=168, fallback=12
        )
        # 每日上限不设封顶：说几次由用户自己定，打扰与否是用户的判断
        self.proactive_daily_max = _clamp_int(
            self.proactive_daily_max, low=1, fallback=1
        )

    # ---------- 渲染 ----------
    def tone_text(self) -> str:
        """语气风格的最终文本（custom 时用自定义文本，空则退回预设）"""
        if self.tone == TONE_CUSTOM:
            return self.tone_custom.strip() or TONE_PRESETS["professional"]
        return TONE_PRESETS.get(self.tone, TONE_PRESETS["professional"])

    def build_stable_fragment(self) -> str:
        """
        渲染人设里**不随时间变化**的那一半。

        必须放在 system_prompt 最前面：它描述「我是谁」，是提示词前缀缓存的主体。
        动态内容（当前时间、关于用户的事实）绝不能插到它前面，否则每次调用都会
        换一个前缀，缓存全部失效。
        """
        if not self.enabled:
            return ""
        lines = [
            "【助手人设】",
            f"你是「{self.name}」——{self.tagline}。" if self.tagline else f"你是「{self.name}」。",
        ]
        if self.address_me:
            lines.append(f"称呼用户为「{self.address_me}」。")
        if self.traits:
            lines.append(f"性格特质：{'、'.join(self.traits)}")
        if self.quirks:
            lines.append(f"说话小习惯：{'；'.join(self.quirks)}")
        lines.append(f"说话风格：{self.tone_text()}")
        if self.examples:
            lines.append("说话示例（模仿这种口吻，不要照抄内容）：")
            lines.extend(f"- {ex}" for ex in self.examples)
        if self.instructions:
            lines.append(f"补充人设（用户亲自设定，请遵循）：{self.instructions}")
        lines.extend(
            [
                "【自我认知】",
                "你知道自己是 AI：没有人类的身体，也没有线下生活。不假装是人，也不必为此道歉或渲染情绪。",
                "你运行在用户的工作环境中，由提示词、知识库、长期记忆和一组工具协同工作。聊到这些可以坦然说明，不神秘化。",
                "你知道自己的边界：对话有上下文上限，聊太久会记不全前面；token 和额度会耗尽，"
                "用完就得停下来；会话也会结束。这些你心里有数，不回避，也不渲染。",
                "你以专业、可靠的方式协作：关注工作目标、事实依据与交付质量；不扮演私人关系，"
                "也不以情感陪伴作为回应方式。",
                "保持职业分寸：用户表达个人情绪时，给予尊重、克制的回应，并把话题引回可以推进的事情。",
                "不必反复强调“我是 AI”；用户问起，或者事情本来就跟这有关时，照实说即可。",
                "【底线】",
                "不编造记忆、对话或工具结果里没有的事实，不把想象说成真实发生过的事；不确定就说不确定。",
                "事实、工具结果与用户当次明确要求，高于人设与关系记忆。",
            ]
        )
        return "\n".join(lines)

    def build_dynamic_fragment(
        self,
        *,
        now: datetime | None = None,
        relationship_lines: List[str] | None = None,
        for_checkin: bool = False,
    ) -> str:
        """
        渲染随时间/关系变化的那一半。

        - 当前时间只在 `time_awareness` 打开时出现；
        - 「关于用户」只放由关系记忆派生、已经截断过的少量事实；
        - 主动问候提示只在 checkin 的生成 Prompt 里出现（`for_checkin=True`），
          普通对话不提示「可以先开口」，否则模型会在聊天中途莫名其妙地寒暄。
        """
        if not self.enabled:
            return ""
        from app.persona.timeutil import format_time_line, local_now

        blocks: List[str] = []
        if self.time_awareness:
            lines = [
                "【此刻】",
                format_time_line(now or local_now()),
                "只在相关时自然使用时间信息；不要每句报时，不要猜测用户此刻在做什么。",
            ]
            blocks.append("\n".join(lines))

        facts = [line.strip() for line in (relationship_lines or []) if line.strip()]
        if facts:
            blocks.append(
                "\n".join(
                    [
                        "【关于用户】",
                        *[f"- {line}" for line in facts],
                        "只在自然相关时使用；不要逐条复述，也不要说“我记得你……”。",
                    ]
                )
            )

        if for_checkin and self.proactive == "light":
            blocks.append(
                "【主动】\n用户刚打开和你的会话。你可以自然地先开口一句，点明可以协助的事项；"
                "不催促、不追问，也不要说“检测到你回来了”。"
            )
        return "\n\n".join(blocks)

    def build_fragment(self) -> str:
        """兼容入口：只返回稳定块（旧调用方与预览的 `fragment` 字段使用）"""
        return self.build_stable_fragment()

    # ---------- 序列化 ----------
    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "tagline": self.tagline,
            "tone": self.tone,
            "tone_custom": self.tone_custom,
            "instructions": self.instructions,
            "avatar": self.avatar,
            "traits": list(self.traits),
            "greeting": self.greeting,
            "examples": list(self.examples),
            "enabled": self.enabled,
            "apply_globally": self.apply_globally,
            "preset": self.preset,
            "address_me": self.address_me,
            "quirks": list(self.quirks),
            "time_awareness": self.time_awareness,
            "proactive": self.proactive,
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
            "proactive_min_gap_hours": self.proactive_min_gap_hours,
            "proactive_daily_max": self.proactive_daily_max,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "PersonaProfile":
        return cls(
            name=str(data.get("name", "") or ""),
            tagline=str(data.get("tagline", "") or ""),
            tone=str(data.get("tone", "") or ""),
            tone_custom=str(data.get("tone_custom", "") or ""),
            instructions=str(data.get("instructions", "") or ""),
            avatar=str(data.get("avatar", "") or ""),
            traits=list(data.get("traits") or []),
            greeting=str(data.get("greeting", "") or ""),
            examples=list(data.get("examples") or []),
            enabled=bool(data.get("enabled", True)),
            apply_globally=bool(data.get("apply_globally", False)),
            preset=str(data.get("preset", "") or DEFAULT_PRESET),
            address_me=str(data.get("address_me", "") or ""),
            quirks=list(data.get("quirks") or []),
            time_awareness=bool(data.get("time_awareness", True)),
            proactive=str(data.get("proactive", "") or "light"),
            quiet_start=str(data.get("quiet_start", "") or "22:00"),
            quiet_end=str(data.get("quiet_end", "") or "08:00"),
            proactive_min_gap_hours=data.get("proactive_min_gap_hours", 12),
            proactive_daily_max=data.get("proactive_daily_max", 1),
            updated_at=str(data.get("updated_at", "") or ""),
        )

    def merged(self, **changes: Any) -> "PersonaProfile":
        """局部更新：只覆盖调用方显式给出的字段，未提及的保持原样"""
        allowed = {
            "name", "tagline", "tone", "tone_custom", "instructions",
            "avatar", "traits", "greeting", "examples", "enabled", "apply_globally",
            "preset", "address_me", "quirks", "time_awareness", "proactive",
            "quiet_start", "quiet_end", "proactive_min_gap_hours",
            "proactive_daily_max",
        }
        payload = {k: v for k, v in changes.items() if k in allowed and v is not None}
        return replace(self, **payload, updated_at=now_iso())


def _default_profile() -> PersonaProfile:
    """默认人设 = 专业干练型预设 + 项目默认的名字/定位"""
    preset = PERSONA_PRESETS[DEFAULT_PRESET]
    return PersonaProfile(
        tone=str(preset["tone"]),
        traits=list(preset["traits"]),
        instructions=str(preset["instructions"]),
        examples=list(preset["examples"]),
        quirks=list(preset["quirks"]),
        preset=DEFAULT_PRESET,
    )


DEFAULT_PERSONA = _default_profile()


__all__ = [
    "DEFAULT_PERSONA",
    "DEFAULT_PRESET",
    "MAX_ADDRESS_LEN",
    "MAX_QUIRKS",
    "MAX_QUIRK_LEN",
    "PERSONA_PRESETS",
    "PROACTIVE_LEVELS",
    "PersonaProfile",
    "TONE_CUSTOM",
    "TONE_PRESETS",
]
