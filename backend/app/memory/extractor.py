"""
长期记忆的抽取 —— 两条路径，对应两类完全不同的需求

| 路径        | 触发                    | 实现      | 延迟        | 定位           |
|-------------|-------------------------|-----------|-------------|----------------|
| `explicit`  | 「记住：报告要用 PPT」   | 正则      | 0（同步）    | 精确匹配的保证 |
| `inferred`  | 从对话里推断（无显式指令）| LLM       | 秒级（后台） | 覆盖隐性偏好   |

★ 为什么显式路径不交给模型

用户说「记住：X」时，他的意图是**确定**的，此时调用模型去「理解」只会引入
不确定性（模型可能改写成别的意思，也可能干脆返回空）。正则在这里更快、
更准、免费，而且**可以被单测穷举**。

更重要的是：显式路径要能在一轮对话结束前同步完成，这样才能立刻回一句
「已记住：X」。如果走模型，就只能在后台慢慢跑，用户看不到任何反馈。
—— 与 Phase 3 里「BM25 的价值是精确匹配的确定性保证」是同一种判断。

`inferred` 路径反过来：它处理的是「用户没说，但从对话里能看出来」的偏好，
这类判断只能靠模型，而且**宁可漏记也不能错记**（错误的记忆会污染之后所有回答），
所以 Prompt 里明确要求「没有把握就返回空数组」。

★ 两条硬约束

1. **绝不抛异常**。抽取失败的正确表现是「这次没学到东西」，而不是让回答报错。
2. **显式路径不得重复触发**。同一句话里命中多个模式时按优先级取第一个，
   否则「记住：以后报告都用 PPT」会同时产出「以后报告都用 PPT」和「报告都用 PPT」两条。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from app.memory.models import (
    ACTIONS,
    CATEGORIES,
    RELATIONSHIP_CATEGORIES,
    MemoryCandidate,
    normalize_relationship_key,
)

logger = logging.getLogger(__name__)

# 单条偏好值的长度上限（超出通常是误匹配到了一整段话）
MAX_VALUE_CHARS = 80

# 明显不是偏好的短语（正则命中但语义不是「记住一个偏好」）
_DENY_VALUES = {
    "再说", "再说吧", "了吗", "我说的话", "这个", "一下", "什么", "怎么回事",
}

# 凭据/证件类内容一律不记 —— 记忆会进 Prompt、会写进可读文件、会在控制台展示，
# 一旦把密码或验证码记下来，它就成了一个长期存在的泄露面。
# 宁可误伤一条「密码要用 1Password 管」的偏好，也不冒这个险。
_SENSITIVE_RE = re.compile(
    r"密码|口令|验证码|动态码|短信码|身份证|银行卡|信用卡|卡号|私钥|密钥|"
    r"api[\s_-]*key|access[\s_-]*token|secret|密码保护|安全问题",
    re.IGNORECASE,
)
_SENSITIVE_NUMBER_RE = re.compile(r"(?<!\d)(?:\d{16,19}|\d{17}[\dXx])(?!\d)")

# 显式的关系类指令：只收「确定无疑」的形态，其余交给被动抽取
_RELATIONSHIP_TRIGGERS: List[tuple[str, re.Pattern[str]]] = [
    # (固定键, 模式)；`不要叫我…` 是否定式，交给通用路径，别在这里反着记
    ("address", re.compile(r"(?<!不要)(?:以后|请|就)?叫我(?P<val>[^\s，。！？!?,]{1,12})")),
    ("address", re.compile(r"称呼我(?:为|作)?(?P<val>[^\s，。！？!?,]{1,12})")),
    ("birthday", re.compile(r"(?:我的)?生日(?:是|在)[：:，,\s]*(?P<val>.+)")),
    ("important_dates", re.compile(r"(?:我的)?(?:结婚)?纪念日(?:是|在)[：:，,\s]*(?P<val>.+)")),
]


def is_sensitive(text: str) -> bool:
    """看起来像凭据/证件号码的内容（抽取路径统一用它做最后一道闸）"""
    value = str(text or "")
    return bool(_SENSITIVE_RE.search(value) or _SENSITIVE_NUMBER_RE.search(value))


def derive_relationship_key(value: str) -> str:
    return normalize_relationship_key("", value)


# ---------- 显式路径：触发模式 ----------
# 顺序即优先级：越具体的放前面
_TRIGGERS: List[tuple[re.Pattern[str], bool]] = [
    # (模式, 是否否定式)
    (re.compile(r"(?:请|帮我|麻烦你)?记住[：:，,\s]*(?P<val>.+)"), False),
    (re.compile(r"记一下[：:，,\s]*(?P<val>.+)"), False),
    (re.compile(r"我的(?:偏好|习惯|要求)是[：:，,\s]*(?P<val>.+)"), False),
    (re.compile(r"我(?:更)?(?:喜欢|偏好)[：:，,\s]*(?P<val>.+)"), False),
    (re.compile(r"我(?:不|不太|不想)(?:喜欢|要|希望)[：:，,\s]*(?P<val>.+)"), True),
    (re.compile(r"以后(?:都|请|要|用|按)?[：:，,\s]*(?P<val>.+)"), False),
    (re.compile(r"默认(?:就)?(?:用|按)?[：:，,\s]*(?P<val>.+)"), False),
]

# 类别判定关键词：**顺序有意义**，先匹配到的类别胜出
_CATEGORY_RULES: List[tuple[str, tuple[str, ...]]] = [
    ("relationship", (
        "生日", "纪念日", "重要日子", "叫我", "称呼我", "我老婆", "我老公",
        "我女朋友", "我男朋友", "我妈", "我爸", "我儿子", "我女儿", "家人",
    )),
    ("life", (
        "我住在", "住在", "养了", "养猫", "养狗", "通勤", "上班", "下班",
        "起床", "睡觉", "作息", "周末", "爱好", "最近在", "这阵子",
    )),
    ("identity", ("我叫", "我是", "我的名字", "称呼我", "叫我")),
    ("output_format", (
        "ppt", "幻灯片", "表格", "图表", "word", "excel", "pdf", "markdown",
        "报告", "格式", "排版", "模板", "文档", "清单化",
    )),
    ("language", ("中文", "英文", "英语", "日文", "日语", "语言")),
    ("length", ("简短", "简略", "长一点", "详细一点", "字数", "篇幅", "详略", "啰嗦", "冗长", "精简")),
    ("tone", ("简洁", "详细", "口语", "正式", "专业", "通俗", "语气", "风格")),
    ("workflow", ("步骤", "分点", "条列", "流程", "先", "再", "总结")),
    ("tooling", ("工具", "命令", "脚本", "python", "api", "用")),
    ("schedule", ("每天", "每周", "每月", "提醒", "时间", "早上", "晚上")),
]

# 句末标点与语气词：值不该带着它们
_TRAILING = "。．.！!？?；;，,、 \t\r\n"
_LEADING = "：:，,、 \t\r\n\"'「『（("

# 疑问句标记。显式路径最大的误报来源是「记住…？」其实是**提问**：
#   「记住密码是多少来着？」「你还记得我喜欢什么吗？」
# 它们命中了「记住」，但用户在问问题，不是在给偏好。
# 误记一条假偏好会持续污染之后的回答，所以这里宁可不记。
_INTERROGATIVE = ("？", "?", "吗", "呢", "多少", "什么", "怎么", "为什么", "哪儿", "哪里", "几时", "何时")


def _is_interrogative(sentence: str) -> bool:
    return any(mark in sentence for mark in _INTERROGATIVE)


def _clean_value(raw: str) -> str:
    """裁掉首尾标点与多余空白"""
    value = (raw or "").strip()
    value = value.lstrip(_LEADING).rstrip(_TRAILING)
    # 去掉句末语气词（「…吧」「…哦」）
    for tail in ("吧", "哦", "啊", "呢", "嘛"):
        if len(value) > 2 and value.endswith(tail):
            value = value[: -len(tail)].rstrip(_TRAILING)
    return value.strip()


def _sentences(text: str) -> List[str]:
    """按句切分：显式指令通常只在一句话里，切句能显著降低误匹配"""
    parts = re.split(r"[。！？!?\n\r；;]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def derive_category(value: str) -> str:
    """按关键词判定类别（判定不出就归 other）"""
    lowered = value.lower()
    for category, words in _CATEGORY_RULES:
        if any(w in lowered for w in words):
            return category
    return "other"


def extract_explicit(text: str) -> List[MemoryCandidate]:
    """
    从用户输入里抽取「明确要求记住的偏好」。

    纯函数、零 IO、零网络 —— 因此可以在每轮对话里同步调用，
    并且可以用单测把各种说法穷举一遍。
    """
    if not text or not text.strip():
        return []

    candidates: List[MemoryCandidate] = []
    seen_values: set[str] = set()

    for sentence in _sentences(text):
        # 疑问句直接跳过：命中「记住」不代表在表达偏好（见 _INTERROGATIVE 的说明）
        if _is_interrogative(sentence):
            continue

        # 关系类指令先判：它们的形态最具体（「叫我X」「生日是X」），
        # 让通用触发词去截反而会把「我的生日是…」记成一条语气偏好。
        handled = False
        for rel_key, pattern in _RELATIONSHIP_TRIGGERS:
            match = pattern.search(sentence)
            if not match:
                continue
            value = _clean_value(match.group("val"))
            if rel_key == "address":
                # 「叫我老王就行」里「就行」是语气尾巴，不是称呼的一部分
                value = re.sub(r"(?:就行|就好了?|就可以了?|好了)$", "", value).strip()
            if not value or len(value) > MAX_VALUE_CHARS or value in _DENY_VALUES:
                continue
            if is_sensitive(value):
                logger.debug("跳过疑似凭据的关系记忆: %s", value[:20])
                continue
            if value in seen_values:
                handled = True
                break
            seen_values.add(value)
            candidates.append(
                MemoryCandidate(
                    key=rel_key,
                    value=value,
                    category="relationship",
                    confidence=0.95,
                    evidence=sentence,
                    source="explicit",
                )
            )
            handled = True
            break
        if handled:
            continue

        for pattern, negative in _TRIGGERS:
            match = pattern.search(sentence)
            if not match:
                continue
            value = _clean_value(match.group("val"))
            if not value or len(value) > MAX_VALUE_CHARS or value in _DENY_VALUES:
                continue
            if is_sensitive(value):
                logger.debug("跳过疑似凭据的记忆: %s", value[:20])
                continue
            if negative:
                value = f"不要{value}"
            if value in seen_values:
                break
            seen_values.add(value)

            category = derive_category(value)
            # 否定式单独一个 key：否则「喜欢简洁」会被「不喜欢啰嗦」顶掉，
            # 而这两句其实可以同时成立
            key = f"{category}_avoid" if negative else category
            candidates.append(
                MemoryCandidate(
                    key=key,
                    value=value,
                    category=category,
                    confidence=0.95,
                    evidence=sentence,
                    source="explicit",
                )
            )
            # 一句话只认第一个命中的模式，避免同义改写重复产出
            break

    return candidates


# ---------- 被动路径：LLM 抽取 + Mem0 式冲突判定 ----------
# 给判定用的现有记忆条数上限：只挑最近更新的，Prompt 不会随记忆增长而膨胀
JUDGE_CONTEXT_MAX_ITEMS = 20

INFERRED_PROMPT = """你负责从一段用户与助手的对话中，提取**用户的长期偏好**与**关于用户本人的稳定信息**，并与现有记忆做冲突判定。

判断标准（很重要）：
1. 只提取「换一个话题依然成立」的稳定信息 —— 输出格式、语言、语气、详略、
   常用工具、工作习惯、称呼、生日、重要日子、作息、长期喜好、最近在忙的事。
2. **不要**提取一次性的任务信息（「今天帮我写个周报」不是偏好）。
3. **不要**提取任何知识性内容或助手的能力说明。
4. 没有把握就**不要输出** —— 错误的记忆会污染之后所有的回答，比漏记严重得多。
   宁缺毋滥，可以返回空数组。
5. `evidence` 必须是对话里真实出现的原话片段，不要自己编。
6. **绝不提取**密码、口令、验证码、身份证号、银行卡号、私钥、API Key、Token、
   密钥等凭据或证件号码；即使原话里出现了也一律跳过。
7. 关于用户本人的信息只在用户**明确说出**时提取；不要从语气或上下文里猜。

category 只能取以下之一：
{category_hint}

relationship / life 两类使用下表的**固定 key**（不要自创键名）：
- address：希望被怎么称呼
- birthday：生日
- important_dates：纪念日 / 重要日期
- routine：作息习惯
- likes：长期喜好
- current_focus：最近在忙的事
其余类别直接用类别名作 key。

现有长期记忆（key: value）：
{existing_memory}

对每条提取结果，对照现有记忆给出 action 判定（参考 Mem0 的做法）：
- `add`：新偏好，现有记忆里没有
- `update`：修正某条现有记忆（用户改变了主意）—— 填 `target_key` 为那条记忆的 key
- `delete`：用户在对话里**明确否定**了某条现有记忆 —— 填 `target_key` 为那条记忆的 key
- `noop`：现有记忆已包含同样内容，无需任何动作
- `reinforce`：用户在本轮明确再次确认了某条旧事实，或明确询问旧记录且助手实际正确使用了它。
  `target_key` 填旧记忆的 key，`value` 必须逐字复制旧值，`evidence` 必须来自本轮用户原话。
  仅仅在提示词中看到、助手主动复述、用户否定/纠正、或只是提到相关话题，都不能强化；
  助手仅在向用户求证旧记录时也不能强化。没有确实回忆/确认则用 noop。
没有现有记忆可对照、或拿不准时，一律用 `add`（宁多记不错删；
delete 只在用户明确说「不要再…」「我不用 X 了」时才允许）。

对话内容：
【用户】{user_input}
【助手】{answer}

只返回 JSON 数组，不要输出任何解释或 Markdown 代码块。元素格式：
[{{"key": "类别键", "value": "一句话偏好", "category": "类别", "confidence": 0.8, "evidence": "原话片段", "action": "add", "target_key": ""}}]

没有任何值得长期记住的内容时，返回 []"""


def _extract_json_array(text: str) -> List[Dict[str, Any]] | None:
    """从模型输出里抠出第一个 JSON 数组（容忍 ```json 包裹与前后废话）"""
    if not text:
        return None
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def parse_inferred(raw_text: str) -> List[MemoryCandidate]:
    """
    把模型返回的文本解析成候选偏好（纯函数，便于单测）。

    逐条做**严格校验**：字段缺失、类别非法、值过长或过短的一律丢弃 ——
    模型偶尔会返回半成品，让它们进库比丢弃它们代价大得多。

    action 判定的校验规则（宁多记不错删）：
    - action 非法或缺失 → 按 `add` 处理（兼容旧版输出格式）
    - `delete` / `update` 没有 `target_key` → 整条丢弃（无法定位作用对象，
      让它以 add 身份进库会造成「同一偏好两条并存」）
    - `update` / `delete` 时 key 让位给 target_key（作用对象才是去重主体）
    """
    data = _extract_json_array(raw_text)
    if not data:
        return []

    out: List[MemoryCandidate] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        value = _clean_value(str(row.get("value", "")))
        if not value or len(value) > MAX_VALUE_CHARS or value in _DENY_VALUES:
            continue
        if is_sensitive(value):
            logger.debug("丢弃疑似凭据的推断记忆: %s", value[:20])
            continue
        category = str(row.get("category", "")).strip().lower()
        if category not in CATEGORIES:
            category = derive_category(value)
        key = str(row.get("key", "")).strip() or category
        if category in RELATIONSHIP_CATEGORIES:
            key = normalize_relationship_key(key, value)
        raw_conf = row.get("confidence", 0.6)
        try:
            confidence = float(raw_conf)
        except (TypeError, ValueError):
            confidence = 0.6
        # 推断路径的置信度上限压在 0.85：它永远不该比用户亲口说的更可信
        confidence = max(0.0, min(0.85, confidence))

        action = str(row.get("action", "add")).strip().lower() or "add"
        if action not in ACTIONS:
            action = "add"
        target_key = str(row.get("target_key", "")).strip()
        if action in {"update", "delete", "reinforce"} and not target_key:
            continue  # 定位不到作用对象的判定无法安全执行
        if action in {"update", "delete", "reinforce"}:
            key = target_key

        out.append(
            MemoryCandidate(
                key=key,
                value=value,
                category=category,
                confidence=confidence,
                evidence=str(row.get("evidence", ""))[:200],
                source="inferred",
                action=action,
                target_key=target_key,
            )
        )
    return out


def _existing_digest(items: List[Any]) -> str:
    """把现有记忆压成「key: value」清单（判定用上下文，最多 JUDGE_CONTEXT_MAX_ITEMS 条）"""
    lines = [f"- {i.key}: {i.value}" for i in items[:JUDGE_CONTEXT_MAX_ITEMS]]
    return "\n".join(lines) if lines else "（空）"


async def extract_inferred(
    user_input: str,
    answer: str,
    existing: List[Any] | None = None,
) -> List[MemoryCandidate]:
    """
    调模型从一轮对话里推断偏好，并对照现有记忆给出 action 判定。

    ⚠️ 任何失败（模型不可用、输出不是 JSON、超时）都返回空列表，绝不抛异常 ——
    调用方是 fire-and-forget 的后台任务，抛出去只会变成一条无人处理的
    "Task exception was never retrieved"。
    """
    if not (user_input or "").strip() or not (answer or "").strip():
        return []

    from app.config import settings

    # 助手的长篇回答对「偏好判断」没有增量信息，反而把 Prompt 撑大。
    # 截断到 600 字：足够体现风格，又不至于浪费 token。
    prompt = INFERRED_PROMPT.format(
        category_hint=" / ".join(CATEGORIES.keys()),
        existing_memory=_existing_digest(existing or []),
        user_input=user_input[:1000],
        answer=answer[:600],
    )

    try:
        from app.core.llm_factory import create_llm

        llm = create_llm(streaming=False, model=settings.MEMORY_EXTRACT_MODEL or None)
        message = await llm.ainvoke(prompt)
        text = message.content if isinstance(message.content, str) else str(message.content)
    except Exception as e:  # noqa: BLE001
        logger.warning("记忆被动抽取调用模型失败（本轮不学习）: %s", e)
        return []

    candidates = parse_inferred(text)
    if candidates:
        logger.info("记忆被动抽取：得到 %s 条候选偏好", len(candidates))
    return candidates


__all__ = [
    "MAX_VALUE_CHARS",
    "INFERRED_PROMPT",
    "JUDGE_CONTEXT_MAX_ITEMS",
    "derive_category",
    "derive_relationship_key",
    "extract_explicit",
    "extract_inferred",
    "is_sensitive",
    "parse_inferred",
]
