"""
会话历史的数据模型

★ 一条「轮次」（turn）而不是一条「消息」是这里的原子单位

对话在存储层被切成 **一轮 = 一次用户输入 + 该轮的完整回答**，
而不是「用户消息一条、助手消息一条」。理由是这两条的生命周期本来就绑在一起：
回答可能被审批打断、可能被用户按停、可能中途报错 ——
拆成两条独立的记录，就会出现「有一条用户消息，但它的回答不知去哪了」的中间态。
一轮一条记录，要么整轮在、要么整轮不在，折叠与删除都不会留下孤儿。

★ 为什么存的是**事件流**而不是渲染好的消息

每轮除了 `user_input` / `answer` 之外，还保存了这轮产生过的全部 SSE 事件
（`token` 除外，它由 `answer` 一次性还原）。这样前端恢复历史时用的是
**和实时流完全相同的那个 reducer** —— 工具卡片、引用来源、审批留痕、
自动放行提示的处理逻辑只有一份，不存在「历史会话看起来和当时不一样」
这类只在恢复路径上出现的偏差。

★ 轮次的四种收尾状态

    ok          正常跑完（有 done 事件）
    aborted     客户端断连 / 用户主动停止（CancelledError）
    error       事件流抛异常
    interrupted 停在 HITL 审批等待（既没 done 也没报错）

`interrupted` 只能由**读取侧推断**得到（写侧在断连时看到的就是 aborted，
两者在事件流里无法区分）—— 见 `store.load_turns()` 的推断规则。
保留了它，界面才能如实说明「这一轮停在等你确认，不是回答完了」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

#: 轮次状态取值
STATUS_OK = "ok"
STATUS_ABORTED = "aborted"
STATUS_ERROR = "error"
STATUS_INTERRUPTED = "interrupted"

STATUS_LABELS = {
    STATUS_OK: "完成",
    STATUS_ABORTED: "已中断",
    STATUS_ERROR: "出错",
    STATUS_INTERRUPTED: "等待确认",
}

#: 折叠时需要从事件流里剔除的类型 —— `token` 由 answer 还原，`done` 由读取侧重放时补
_SKIPPED_EVENTS = frozenset({"token", "done"})

#: 这一轮是谁先开口的。
#: 助理主动问候产生的轮次没有用户输入，重放时不能按「用户一条 + 助理一条」
#: 的固定配对去还原（前端据此决定是否渲染用户气泡）。
INITIATED_BY_USER = "user"
INITIATED_BY_ASSISTANT = "assistant"
INITIATED_BY_VALUES = frozenset({INITIATED_BY_USER, INITIATED_BY_ASSISTANT})


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def default_thread_title(thread_id: str) -> str:
    """
    没有用户输入可派生标题时的兜底。

    助理会话的第一轮可能是她主动开的口（用户没说话），此时「（空会话）」
    是个错误描述 —— 它有内容，只是没有用户原话。
    """
    if (thread_id or "").startswith("assistant_"):
        return "和助理的对话"
    return "（空会话）"


def derive_title(user_input: str, max_chars: int = 40, fallback: str = "（空会话）") -> str:
    """
    从首轮用户输入派生会话标题。

    会话列表里**必须**能一眼认出一个会话 —— 没有标题的列表等于没有列表。
    这里只做最简单可靠的事：压平空白 + 按字数截断（不调模型，
    标题生成再贵也不该出现在「用户刚发完消息」这条路径上）。
    """
    text = " ".join((user_input or "").split())
    if not text:
        return fallback
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def is_trivial_turn(user_input: str, answer: str, events: List[Dict[str, Any]]) -> bool:
    """
    这一轮是否「什么都没发生」——是的话不该落盘。

    判定不看用户输入（用户说了话就值得留痕），只看**有没有产出**：
    既没有任何回答文本、也没有任何非 token 事件（工具调用/检索/审批…）
    意味着这一轮连图都没真正跑起来（例如启动降级时图未初始化、
    或客户端在第一个事件之前就断了）。写进去只会让会话列表里出现一堆空壳。
    """
    return not (answer or "").strip() and not events


@dataclass
class ConversationTurn:
    """一轮对话（用户输入 + 该轮的完整事件流 + 最终回答）"""

    turn_id: str
    thread_id: str = ""
    created_at: str = ""
    user_input: str = ""
    answer: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_OK
    error: str = ""
    #: 这一轮**成功写入**工作目录的文件（由事件流派生，见 `files.collect_written_files`）。
    #: 只存相对路径与工具名 —— 绝对路径会随工作目录配置变化而失效。
    files: List[Dict[str, str]] = field(default_factory=list)
    #: 这一轮用户带了哪些**对话附件**（只存 id + 文件名，正文不落盘 ——
    #: 附件本身是 24h 临时物，留正文只会让会话文件膨胀且必然过期）。
    #: 存它的理由与「重放 = 当时所见」一致：界面上消息带过附件 chip，
    #: 恢复历史时就不能什么都不显示。
    attachments: List[Dict[str, str]] = field(default_factory=list)
    #: 这一轮 LLM 的真实用量（`{"prompt_tokens" / "completion_tokens" /
    #: "total_tokens" / "reasoning_tokens"}`，由 SSE `done` 事件带出）。
    #: 落盘的理由与 `files` 一致：用量是**这一轮对话的属性**，
    #: 重新打开历史会话时也该看得到 —— 只存在当场那一屏等于看完就没了。
    #: 空字典表示「这一轮没拿到用量」（断连、图未初始化、网关不返回 usage）。
    usage: Dict[str, int] = field(default_factory=dict)
    #: 模型自己的推理正文（`answer` 里已经被摘掉，两者互斥不重叠）。
    #:
    #: 落盘理由：网关会把思考型模型的推理混进正文一起吐出来（见 `app/agents/think.py`），
    #: 而「答了什么」与「怎么想的」是两件事 —— 前者要干净，后者不该丢。
    #: 空串表示这一轮没有推理（绝大多数正常回答都是）。
    reasoning: str = ""
    #: 谁先开口：user（正常一问一答）/ assistant（主动问候）。
    #: 缺省为 user，旧文件没有该字段时也按 user 读 —— 不能把历史轮次
    #: 误判成「助理主动」，那会让重放路径把它当成没有人提问的孤儿。
    initiated_by: str = INITIATED_BY_USER

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "thread_id": self.thread_id,
            "created_at": self.created_at,
            "user_input": self.user_input,
            "answer": self.answer,
            "events": self.events,
            "status": self.status,
            "error": self.error,
            "files": self.files,
            "attachments": self.attachments,
            "usage": self.usage,
            "reasoning": self.reasoning,
            "initiated_by": self.initiated_by,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationTurn":
        raw_files = data.get("files") or []
        files = [
            {"path": str(f.get("path", "")), "tool": str(f.get("tool", ""))}
            for f in raw_files
            if isinstance(f, dict) and f.get("path")
        ]
        raw_attachments = data.get("attachments") or []
        attachments = [
            {"id": str(a.get("id", "")), "filename": str(a.get("filename", ""))}
            for a in raw_attachments
            if isinstance(a, dict) and a.get("filename")
        ]
        # 用量只保留整数（旧文件没有这个字段、或被人手改坏时都退化为空）
        raw_usage = data.get("usage") or {}
        usage: Dict[str, int] = {}
        if isinstance(raw_usage, dict):
            for key, value in raw_usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    usage[str(key)] = int(value)
        initiated_by = str(data.get("initiated_by") or INITIATED_BY_USER)
        if initiated_by not in INITIATED_BY_VALUES:
            initiated_by = INITIATED_BY_USER
        return cls(
            turn_id=str(data.get("turn_id", "")),
            thread_id=str(data.get("thread_id", "")),
            created_at=str(data.get("created_at", "")),
            user_input=str(data.get("user_input", "")),
            answer=str(data.get("answer", "")),
            events=[e for e in (data.get("events") or []) if isinstance(e, dict)],
            status=str(data.get("status", STATUS_OK)),
            error=str(data.get("error", "")),
            files=files,
            attachments=attachments,
            usage=usage,
            reasoning=str(data.get("reasoning", "")),
            initiated_by=(
                str(data.get("initiated_by") or INITIATED_BY_USER)
                if str(data.get("initiated_by") or INITIATED_BY_USER) in INITIATED_BY_VALUES
                else INITIATED_BY_USER
            ),
        )


@dataclass
class ConversationSummary:
    """
    会话索引里的一条（列表页只需要这些，不必读轮次文件）。

    `turns` 与 `preview` 是为了让列表「有信息量」：光有标题与时间，
    用户仍然认不出哪个是自己要找的那个会话。
    """

    thread_id: str
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    turns: int = 0
    preview: str = ""
    #: 最后一轮的收尾状态。列表里要能看出「这个会话上次是正常答完的，
    #: 还是停在等你确认/中断了」—— 否则用户点进去才发现是半截回答。
    last_status: str = ""
    #: 该会话的轮次文件是否存在（索引存在但文件被手工删掉时用来暴露异常）
    exists: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": self.turns,
            "preview": self.preview,
            "last_status": self.last_status,
            "exists": self.exists,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationSummary":
        return cls(
            thread_id=str(data.get("thread_id", "")),
            title=str(data.get("title", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            turns=int(data.get("turns", 0) or 0),
            preview=str(data.get("preview", "")),
            last_status=str(data.get("last_status", "")),
            exists=bool(data.get("exists", True)),
        )


def summarize_turns(thread_id: str, turns: List[ConversationTurn]) -> ConversationSummary:
    """由轮次列表派生索引项（idx 重建与写入后更新索引共用同一条口径）"""
    if not turns:
        return ConversationSummary(thread_id=thread_id)
    last = turns[-1]
    return ConversationSummary(
        thread_id=thread_id,
        title=derive_title(
            turns[0].user_input, fallback=default_thread_title(thread_id)
        ),
        created_at=turns[0].created_at,
        updated_at=last.created_at,
        turns=len(turns),
        preview=preview_of(last),
        last_status=last.status,
    )


def preview_of(turn: ConversationTurn, max_chars: int = 60) -> str:
    """列表里的那一行摘要：优先用回答开头，没有回答就退回用户输入"""
    text = " ".join((turn.answer or "").strip().split())
    if not text:
        text = " ".join((turn.user_input or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def events_for_replay(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """剔除由 `answer` / 读取侧重建的事件，避免重放时内容翻倍"""
    return [e for e in events if e.get("type") not in _SKIPPED_EVENTS]


def infer_status(turn: ConversationTurn) -> str:
    """
    读取侧修正轮次状态。

    写侧只知道「这一轮是怎么结束的」，但有一类收尾它看不出来：
    停在 HITL 审批等待时，事件流既没有 done 也没有异常 —— 从写侧看和
    「客户端断连」是同一件事。这里用事件流的**内容**补上这个区分：
    从后往前找到最后一个「有状态含义」的事件，若它是 `interrupt`，
    说明这一轮正卡在等用户点确认（而不是用户中途放弃）。

    ⚠️ 只看最后一个：一轮里可能有多次审批（多工具依次执行），
    最后一次才是「这一轮停在哪」的答案；若最后一次 interrupt 之后
    又出现了 `state_update`/`tool_result`，说明那次审批已经决出，
    这一轮是真中断（用户点了停止 / 页面关了）。
    """
    if turn.status != STATUS_ABORTED:
        return turn.status
    for event in reversed(turn.events):
        etype = event.get("type")
        if etype == "interrupt":
            return STATUS_INTERRUPTED
        if etype in {"tool_result", "state_update"}:
            return STATUS_ABORTED
    return STATUS_ABORTED
