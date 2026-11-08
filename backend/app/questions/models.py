"""
结构化提问的数据契约与归一化。

★ 为什么模型参数必须「先归一化再挂起」

模型返回的是它自己理解的一份 JSON：字段名会在 `multiSelect` / `multi_select`
之间摇摆，选项可能重复、可能一个都没给，问题可能多到界面放不下。
如果把这些原样塞进 `interrupt()`，那么每一次格式抖动都会变成一次
「用户看到一个奇怪的卡片」或者「任务永远停在等人」。归一化的位置只有这里：
两个驱动层拿到的都是同一份已经确定形状的 payload。

★ 回答为什么按 `q1/q2…` 而不是按问题文本定位

问题文本可以被用户改写（或模型在两轮里给出措辞相近的两个问题），
而 id 是一次提问内部的稳定键。前端提交回答、后端回填 ToolMessage、
留痕里记录答案，三处都用同一个 id —— 少了它，多问题的回答会静默错位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: 工具名（模型可见的稳定标识）
ASK_USER_TOOL = "ask_user"

#: interrupt payload 的判别键。审批 payload 没有这个键（或不是 question），
#: 两个驱动层据此分流 —— 这是「提问不是审批」的结构性保证。
QUESTION_KIND = "question"

#: 一次提问最多几个问题（与业界 AskUserQuestion 的 1-4 对齐）
MAX_QUESTIONS = 4
#: 每个问题至少/至多几个选项。少于 2 个时卡片会退化成「一条几乎没有选择的选择题」，
#: 而用户真正需要的是自由输入；多于 4 个时选项会挤成一堵墙。
MIN_OPTIONS = 2
MAX_OPTIONS = 4

MAX_QUESTION_CHARS = 600
MAX_HEADER_CHARS = 24
MAX_OPTION_LABEL_CHARS = 80
MAX_OPTION_DESC_CHARS = 240
MAX_OTHER_CHARS = 2000

#: 提问的生命周期状态（与审批的 approved/rejected/timeout 刻意分开）
STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_SKIPPED = "skipped"
STATUS_TIMEOUT = "timeout"

QUESTION_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_ANSWERED, STATUS_SKIPPED, STATUS_TIMEOUT}
)

#: 内部状态转移的原因（进留痕，不进界面）
REASON_USER = "user"
REASON_SKIPPED = "skipped"
REASON_TIMEOUT = "timeout"
REASON_CANCELLED = "cancelled"
REASON_INVALID = "invalid"


class AskUserOption(BaseModel):
    """一个可点击的选项"""

    model_config = ConfigDict(extra="ignore")

    label: str = Field(description="选项文字，简短、可点击")
    description: str = Field(default="", description="这个选项意味着什么（一句话）")
    recommended: bool = Field(default=False, description="是否推荐这个选项（每题最多一个）")


class AskUserQuestion(BaseModel):
    """一个问题"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    header: str = Field(default="", description="很短的分组标签（可省略）")
    question: str = Field(description="要问用户的完整问题")
    options: list[AskUserOption] = Field(default_factory=list, description="2-4 个选项")
    multi_select: bool = Field(
        default=False,
        alias="multiSelect",
        description="是否允许多选",
    )


class AskUserArgs(BaseModel):
    """`ask_user` 的完整参数"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    questions: list[AskUserQuestion] = Field(
        description="1-4 个问题；一次只问真正阻塞下一步的那些"
    )


class QuestionAnswer(BaseModel):
    """用户对**一个**问题的回答"""

    model_config = ConfigDict(extra="ignore")

    selected: list[str] = Field(default_factory=list, description="选中的选项文字")
    other: str = Field(default="", description="用户在「其他」里补充的文字")


class QuestionSubmission(BaseModel):
    """前端一次提交的整张卡片"""

    model_config = ConfigDict(extra="ignore")

    answers: dict[str, QuestionAnswer] = Field(default_factory=dict)
    skipped: bool = False


@dataclass
class QuestionOutcome:
    """
    一次提问的等待结果。

    与审批的 `ApprovalOutcome` 同构：执行上「跳过」与「超时」等价
    （都不提供答案），但留痕上不是一回事 —— 跳过是人看过后主动放弃，
    超时是根本没人看。这条区分决定了运维应该「改问题」还是「改提醒」。
    """

    status: str = STATUS_SKIPPED
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason: str = REASON_SKIPPED

    @property
    def skipped(self) -> bool:
        return self.status == STATUS_SKIPPED

    @property
    def timed_out(self) -> bool:
        return self.status == STATUS_TIMEOUT

    def to_resume(self) -> dict[str, Any]:
        """构造成 `Command(resume=...)` 的载荷（节点只认这一种形状）。"""
        return {
            "kind": QUESTION_KIND,
            "status": self.status,
            "answers": dict(self.answers),
            "reason": self.reason,
        }


def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def _format_validation_error(exc: ValidationError) -> str:
    hints: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "参数"
        hints.append(f"{loc}: {err.get('msg')}")
    return "；".join(hints) or "参数格式不正确"


def normalize_ask_user_args(raw: Any) -> tuple[list[dict[str, Any]], str | None]:
    """
    把模型给的参数归一化成可直接挂起的 `questions` 列表。

    返回 `(questions, error)`；`error` 非空时**不挂起**，把错误作为
    `tool_result` 回给模型让它自己纠正 —— 一次格式错误不该让整轮对话停住。

    ★ 为什么限制选项数量而不是静默截断
    截断会让模型以为 5 个选项都问到了，而用户只看到 4 个。
    这类「少了一个选项却没人知道」的偏差，会在多轮里被当成用户没选。
    宁可让它收到一句明确的错误，重新问一次。
    """
    try:
        args = AskUserArgs.model_validate(raw if isinstance(raw, dict) else {})
    except ValidationError as exc:
        return [], f"提问参数不合法：{_format_validation_error(exc)}"

    if not args.questions:
        return [], f"一次提问至少需要 1 个问题（最多 {MAX_QUESTIONS} 个）。"
    if len(args.questions) > MAX_QUESTIONS:
        return [], (
            f"一次最多提 {MAX_QUESTIONS} 个问题（收到 {len(args.questions)} 个）。"
            "请把不阻塞下一步的问题删掉，或拆成两次提问。"
        )

    normalized: list[dict[str, Any]] = []
    for index, question in enumerate(args.questions, start=1):
        text = _flat(question.question)
        if not text:
            return [], f"第 {index} 个问题缺少 question 文本。"
        if len(text) > MAX_QUESTION_CHARS:
            return [], f"第 {index} 个问题过长（上限 {MAX_QUESTION_CHARS} 字）。"

        options: list[dict[str, Any]] = []
        seen: set[str] = set()
        recommended_seen = False
        for option in question.options:
            label = _flat(option.label)
            if not label:
                continue
            if label in seen:
                return [], f"第 {index} 个问题有重复选项「{label}」。"
            seen.add(label)
            recommended = bool(option.recommended) and not recommended_seen
            if recommended:
                recommended_seen = True
            options.append(
                {
                    "label": label[:MAX_OPTION_LABEL_CHARS],
                    "description": _flat(option.description)[:MAX_OPTION_DESC_CHARS],
                    "recommended": recommended,
                }
            )

        if len(options) < MIN_OPTIONS:
            return [], (
                f"第 {index} 个问题至少需要 {MIN_OPTIONS} 个选项"
                f"（收到 {len(options)} 个）。请补充可点击的选项；"
                "用户始终可以在「其他」里自由输入。"
            )
        if len(options) > MAX_OPTIONS:
            return [], f"第 {index} 个问题最多 {MAX_OPTIONS} 个选项（收到 {len(options)} 个）。"

        header = _flat(question.header)[:MAX_HEADER_CHARS] or text[:MAX_HEADER_CHARS]
        normalized.append(
            {
                "id": f"q{index}",
                "header": header,
                "question": text,
                "options": options,
                "multi_select": bool(question.multi_select),
            }
        )

    return normalized, None


def normalize_question_submission(
    questions: list[dict[str, Any]],
    raw_answers: Any,
    *,
    skipped: bool = False,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """
    归一化前端提交的回答，返回 `(status, answers)`。

    - 显式「跳过」或**所有问题都没填** → `STATUS_SKIPPED`
    - 至少一项有内容 → `STATUS_ANSWERED`

    未知问题 id、未知选项、空字符串都会被丢掉：前端可能来自旧版本的标签页，
    后端不能因为一个不认识的键就把整次回答判成失败。
    """
    standards = {str(q.get("id") or ""): q for q in questions if isinstance(q, dict)}
    cleaned: dict[str, dict[str, Any]] = {}

    if isinstance(raw_answers, dict):
        for question_id, raw in raw_answers.items():
            question = standards.get(str(question_id))
            if question is None:
                continue
            data = raw.model_dump() if isinstance(raw, QuestionAnswer) else raw
            if not isinstance(data, dict):
                continue

            allowed = {
                str(option.get("label") or "")
                for option in question.get("options") or []
                if isinstance(option, dict)
            }
            selected_raw = data.get("selected") or []
            if isinstance(selected_raw, str):
                selected_raw = [selected_raw]
            selected: list[str] = []
            for item in selected_raw if isinstance(selected_raw, list) else []:
                label = str(item or "")
                if label in allowed and label not in selected:
                    selected.append(label)

            other = str(data.get("other") or "").strip()[:MAX_OTHER_CHARS]
            if selected or other:
                cleaned[str(question_id)] = {"selected": selected, "other": other}

    if skipped or not cleaned:
        return STATUS_SKIPPED, {}
    return STATUS_ANSWERED, cleaned


def normalize_question_resume(value: Any) -> QuestionOutcome:
    """
    把 `Command(resume=...)` 传回来的内容还原成结果。

    ★ 不认识的形状一律按「跳过」而不是「异常」
    恢复路径上抛异常会把已经挂起了半小时的任务判成失败；
    而调用方真正需要知道的是「这次没拿到答案，继续跑」。
    留痕里会写 `reason=invalid`，排查时看得出是协议问题。
    """
    if not isinstance(value, dict):
        return QuestionOutcome(status=STATUS_SKIPPED, reason=REASON_INVALID)
    status = str(value.get("status") or "")
    if status not in QUESTION_STATUSES or status == STATUS_PENDING:
        status = STATUS_SKIPPED
    answers = value.get("answers")
    clean_answers = answers if isinstance(answers, dict) else {}
    return QuestionOutcome(
        status=status,
        answers=clean_answers,
        reason=str(value.get("reason") or (REASON_USER if status == STATUS_ANSWERED else status)),
    )


def is_question_payload(payload: Any) -> bool:
    """判断一个 interrupt payload 是不是提问（而不是审批）。"""
    return isinstance(payload, dict) and payload.get("kind") == QUESTION_KIND


def build_question_payload(
    *,
    call_id: str,
    questions: list[dict[str, Any]],
    run_id: str = "",
    employee_id: str = "",
) -> dict[str, Any]:
    """构造交给 `interrupt()` 的载荷（driver 据此发起提问流程）。"""
    return {
        "kind": QUESTION_KIND,
        "call_id": call_id,
        "tool_name": ASK_USER_TOOL,
        "questions": questions,
        "run_id": run_id,
        "employee_id": employee_id,
    }


def format_question_result(
    questions: list[dict[str, Any]],
    status: str,
    answers: dict[str, dict[str, Any]],
) -> str:
    """
    把回答写成模型能直接读的 `tool_result`。

    ★ 为什么不是 JSON
    模型接下来要做的是「按回答继续干活」，而不是解析数据结构。
    分条的人话能让它把注意力放在内容上；JSON 会让它多花一步解码，
    还会诱使它把原始结构复述给用户。留痕里另存结构化答案，两者不冲突。
    """
    if status == STATUS_SKIPPED:
        return (
            "用户跳过了这次提问，没有提供答案。\n\n"
            "不要再重复询问同一组问题；请基于现有信息选择最合理的做法继续，"
            "并在最终回答里用一句话说明你采用的假设。"
        )
    if status == STATUS_TIMEOUT:
        return (
            "用户没有在等待时限内回答。\n\n"
            "不要再重复询问同一组问题；请基于现有信息选择最合理的做法继续，"
            "并在最终回答里用一句话说明你采用的假设。"
        )

    lines = ["用户已回答你的提问："]
    for index, question in enumerate(questions, start=1):
        question_id = str(question.get("id") or f"q{index}")
        answer = answers.get(question_id) if isinstance(answers, dict) else None
        selected = list((answer or {}).get("selected") or [])
        other = str((answer or {}).get("other") or "").strip()

        lines.append(f"{index}. {question.get('question') or ''}")
        if selected:
            lines.append("   选择：" + "；".join(selected))
        if other:
            lines.append("   补充：" + other)
        if not selected and not other:
            lines.append("   未回答")
    lines.append("请基于这些回答继续完成任务；不要重复询问已经回答过的问题。")
    return "\n".join(lines)


__all__ = [
    "ASK_USER_TOOL",
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "MIN_OPTIONS",
    "QUESTION_KIND",
    "QUESTION_STATUSES",
    "REASON_CANCELLED",
    "REASON_INVALID",
    "REASON_SKIPPED",
    "REASON_TIMEOUT",
    "REASON_USER",
    "STATUS_ANSWERED",
    "STATUS_PENDING",
    "STATUS_SKIPPED",
    "STATUS_TIMEOUT",
    "AskUserArgs",
    "AskUserOption",
    "AskUserQuestion",
    "QuestionAnswer",
    "QuestionOutcome",
    "QuestionSubmission",
    "build_question_payload",
    "format_question_result",
    "is_question_payload",
    "normalize_ask_user_args",
    "normalize_question_resume",
    "normalize_question_submission",
]
