"""
后台子任务的数据契约。

★ 与会话历史（`conversations/models.py`）的关系

两者是同一套取舍的两处落地：文件是事实来源、索引是可再生的缓存。
但子任务多了一个会话历史没有的维度 —— **状态机**。
会话历史的轮次一旦写下就不可变（`append_turn` 只追加），
而子任务会在 `pending → running → waiting_approval → succeeded` 之间迁移，
所以这里比 `ConversationTurn` 多了两样东西：

1. `progress()`：把事件流**折叠**成可查询的进度快照（不是原样倒出来）。
   前端要的是「现在到哪一步了」，不是「一共有多少条事件」。
2. 合法迁移表（`_ALLOWED_TRANSITIONS`）：状态迁移写错是这类系统最典型的
   静默故障 —— 任务卡在一个没人再看的状态里，界面上转圈到天荒地老。

★ 「可选字段」是本项目反复强调的约定，这里严格遵守

`started_at` / `finished_at` / `error` / `result` / `usage` / `files`
在不该有的时候**键都不存在**，不是 `None`、不是 `""`、不是 `0`。
理由：填 0 在界面上看起来像个真数据，比不显示更糟。
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

# ---------- 状态与迁移 ----------

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_WAITING_APPROVAL = "waiting_approval"
#: 停在模型主动提问上（`ask_user`）。与 `waiting_approval` 刻意分开：
#: 前者等的是一条**信息**，后者等的是一次**授权**。界面上一个是问卷、
#: 一个是危险操作确认；混成同一个状态会让「他在等什么」说不清。
STATUS_WAITING_INPUT = "waiting_input"
#: 进程没了、但 checkpointer 里还留着可以接着跑的状态。
#: ★ 它**不是**终态，也**不是**活跃态：任务不会自己动，等人点「继续」。
#: 与 `orphaned` 的区别是「有没有救」——后者意味着没有可恢复的进度。
STATUS_INTERRUPTED = "interrupted"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_ORPHANED = "orphaned"

ALL_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_WAITING_APPROVAL,
        STATUS_WAITING_INPUT,
        STATUS_INTERRUPTED,
        STATUS_SUCCEEDED,
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_ORPHANED,
    }
)

#: 中断后「怎么续」的评估结论（只在 `interrupted` 上有意义，见 `tasks/recovery.py`）。
#: ★ 定义在这里而不是 recovery 里：接口层也要用它判断「要不要先让用户核对」，
#: 而接口不应 import 评估实现 —— 契约与实现分开。
RESUME_MODE_CHECKPOINT = "checkpoint"  # 断点干净，直接继续
RESUME_MODE_APPROVAL = "approval"      # 停在等确认，续跑会重新问一次
RESUME_MODE_RECONCILE = "reconcile"    # 最后一步可能已生效，续前必须先核对
RESUME_MODES = frozenset(
    {RESUME_MODE_CHECKPOINT, RESUME_MODE_APPROVAL, RESUME_MODE_RECONCILE}
)

#: 终态：不会再变化。「任务还在跑吗」这个问题只看它。
TERMINAL_STATUSES = frozenset(
    {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED, STATUS_ORPHANED}
)

#: 会自己往前走的任务 —— 界面上「进行中」与轮询只看这几条。
#: `interrupted` 刻意不在里面：它在等人，不是系统在忙。
#: `waiting_input` 在：它同样占着一个执行槽，且回答一到就会继续跑。
ACTIVE_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_RUNNING, STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT}
)

#: 合法迁移表。
#:
#: ★ 为什么要显式列出来，而不是"随便改"
#:
#: 允许 `succeeded → running` 这种迁移，看起来无害，实际上会掩盖真 bug：
#: 子图的收尾逻辑跑了两遍，或者 `patch` 被调用了两次。
#: 更糟的是它**不会报错**，只会让一个已结束的任务重新转起来。
#: 于是这里把它变成显式白名单 —— 非法迁移抛 `IllegalTransition`，
#: 让 bug 在写它的那一行就炸出来，而不是在界面上表现为「这任务怎么还在跑」。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PENDING: frozenset(
        {STATUS_RUNNING, STATUS_CANCELLED, STATUS_FAILED, STATUS_ORPHANED}
    ),
    STATUS_RUNNING: frozenset(
        {
            STATUS_WAITING_APPROVAL,
            STATUS_WAITING_INPUT,
            STATUS_INTERRUPTED,
            STATUS_SUCCEEDED,
            STATUS_FAILED,
            STATUS_CANCELLED,
            STATUS_ORPHANED,
        }
    ),
    # 挂起审批时可以回去继续跑（审批放行），也可以被取消或被判失败
    STATUS_WAITING_APPROVAL: frozenset(
        {
            STATUS_RUNNING,
            STATUS_INTERRUPTED,
            STATUS_FAILED,
            STATUS_CANCELLED,
            STATUS_ORPHANED,
        }
    ),
    # 停在提问上：回答/跳过/超时后回到 running；进程重启则判中断。
    # ★ 允许直接进 succeeded：与 `interrupted` 同理，图可能已经跑完、
    #   只差落盘，由恢复逻辑补结算。
    STATUS_WAITING_INPUT: frozenset(
        {
            STATUS_RUNNING,
            STATUS_SUCCEEDED,
            STATUS_INTERRUPTED,
            STATUS_FAILED,
            STATUS_CANCELLED,
            STATUS_ORPHANED,
        }
    ),
    # 中断是「暂停」而不是「结束」：可以续跑，也可以被放弃。
    # ★ 允许直接进 succeeded：图其实已经跑完、只差落盘那一步的中断，
    #   由 `recovery.py` 补结算（再跑一遍会重复生成文件）。
    STATUS_INTERRUPTED: frozenset(
        {
            STATUS_RUNNING,
            STATUS_SUCCEEDED,
            STATUS_FAILED,
            STATUS_CANCELLED,
            STATUS_ORPHANED,
        }
    ),
    # 终态不可再迁出 —— 这一条是整个表里最重要的
    STATUS_SUCCEEDED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_CANCELLED: frozenset(),
    STATUS_ORPHANED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """非法的任务状态迁移（见 `_ALLOWED_TRANSITIONS`）"""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"非法的任务状态迁移: {current} → {target}")
        self.current = current
        self.target = target


def can_transition(current: str, target: str) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def is_active(status: str) -> bool:
    return status in ACTIVE_STATUSES


# ---------- 进度 ----------

#: 进度条目的种类。前端按它决定图标与文案。
PROGRESS_PLAN = "plan"
PROGRESS_SOURCES = "sources"
PROGRESS_TOOL_CALL = "tool_call"
PROGRESS_TOOL_RESULT = "tool_result"
PROGRESS_APPROVAL = "approval"
#: 模型主动提问（`ask_user`）。与 `approval` 分开：
#: 一个是「问你选哪个」，一个是「问你同不同意」。
PROGRESS_QUESTION = "question"
PROGRESS_ANSWER = "answer"
PROGRESS_ERROR = "error"
#: ★ 子 Agent 在两次工具调用之间说的那段话（「先看一下最新版本，再决定装什么」）。
#: 它与 `answer` 的区别不是长度而是**位置**：`answer` 是这一步做完了的结论，
#: `narrative` 是「我接下来打算这么干」的过渡叙述。
#: 界面上它承担截图里那种「展开后能看到它到底在想什么」的职责 ——
#: 只有 `label`（≤80 字）时，用户看到的是「调用 web_search」，
#: 而看不到「为什么要查」。正文落 `text`（上限见 PROGRESS_TEXT_CHARS）。
PROGRESS_NARRATIVE = "narrative"

#: 单条进度在界面上显示的文案上限。截断在这里做，
#: 是因为「一句能看的话」只有这一处定义 —— 前端拿到才能直接渲染。
PROGRESS_LABEL_CHARS = 80

#: `narrative` 正文的字符上限。
#: ★ 与 `label` 分开：`label` 是「一行」，正文是「一段」。
#: 上限存在的理由不是省磁盘，而是**同步成本**：正文会随进度一起进快照、
#: 进 SSE 增量、进轮询响应，每一份都是整段重传。一段几千字的独白
#: 会让「查进度」这个小接口变成下载一篇文章。
PROGRESS_TEXT_CHARS = 1200


class ProgressEntry(TypedDict, total=False):
    """
    一步进度的快照 —— 由子 Agent 的事件**折叠**而来，不是新事件类型。

    ★ 为什么不是新事件类型
    子 Agent 发的仍然是那批已有事件（`sources` / `tool_call` / `tool_result` /
    `interrupt` …）。它们在前端有既定的渲染方式；再造一种「进度事件」
    只会让同一件事有两套形状（不变量 15 的精神：一份契约）。
    所以这里是**投影**：`_entry_from_event` 是唯一的折叠函数。

    字段（除 seq/kind/label/status 外**都可选，没数据时键不存在**）：
        seq: 单调递增，等于 jsonl 里的行序。
             ★ 它是必需的：实时 SSE 与 REST 轮询**两条通道会推同一个 seq**，
               前端靠它去重与排序，否则进度会重复出现。
        at:  ISO 时间戳。落盘时加的 —— 事件本身不带时间戳。
        kind / label / status: 见上。
        detail: 工具名 /「命中 3 个来源」这类补充。
        text: 仅 `narrative` 有。子 Agent 在那一步说的**整段话**（已切分推理）。
              ★ 它不进 `label` 是因为两者用途不同：`label` 是列表里那一行，
              正文是展开后才读的段落；挤进同一个字段会让收起的列表被撑长。
        call_id: 仅 tool_call / tool_result / approval 三类有。
    """

    seq: int
    at: str
    kind: str
    label: str
    status: str
    detail: str
    text: str
    call_id: str


class TaskProgressView(TypedDict, total=False):
    """一个任务的进度快照（列表接口与详情接口共用同一个形状）"""

    task_id: str
    status: str
    #: ★ 可选：真的超了上限才带。界面上据此说「只显示了最近 N 步」，
    #: 不能悄悄截断（与 `done.files` 可选字段同一约定）。
    total_steps: int
    entries: list[ProgressEntry]


# ---------- 任务记录 ----------


#: 任务的派生来源。三个值各自对应一条真实入口，多一个就有一个是假的。
SOURCE_BOARD = "board"  # 任务板卡片被认领后派发
SOURCE_SUPERVISOR = "supervisor"  # 主对话的 Supervisor 拆解（ADR 0019）
SOURCE_EMPLOYEE_PRIVATE = "employee_private"  # 员工私聊工作台里自己发起
TASK_SOURCES = (SOURCE_BOARD, SOURCE_SUPERVISOR, SOURCE_EMPLOYEE_PRIVATE)


class TaskRecord(TypedDict, total=False):
    """
    一个后台子任务的完整记录（索引项的内容 = 文件折叠出来的结果）。

    字段取舍见模块注释的「可选字段」一节。
    """

    task_id: str
    #: 归属的用户会话。子图自己的 thread_id 是 `subtask_<task_id>`，
    #: 两者刻意不同 —— 详见 ADR 0019 的命名空间隔离一节。
    thread_id: str
    #: 哪一轮派生的（用于界面分组展示）
    parent_turn_id: str
    #: 内部专员名（pc_agent / file_agent / …），是**工具名不是显示名**。
    #: 界面在基础页签不显示它。
    agent: str
    #: ★ 人话任务名。进 prompt 也进界面 ——
    #: 模型需要它来回答「我那个任务怎么样了」，用户需要它来认领自己的任务。
    title: str
    #: 派给子 Agent 的完整任务描述。刻意**不**给前端看：
    #: 它可能很长，而前端渲染的始终是 title。
    instruction: str
    status: str
    #: 中断后的可续性评估结果（只在 `interrupted` 上有意义）：
    #: `checkpoint` = 可以直接接着跑；`approval` = 停在等确认，续跑会重新问你；
    #: `reconcile` = 最后一步可能已有副作用，续跑前必须先核对。
    #: 空串 = 还没评估 / 不可续。
    resume_mode: str
    #: 给用户看的一句话（「「写入报告.docx」可能已经执行过，继续会重做这一步」）。
    resume_note: str
    created_at: str
    started_at: str
    finished_at: str
    error: str
    #: 子 Agent 的最终回答。已过 `think.split_think` —— 与会话历史同一约定。
    result: str
    usage: dict[str, int]
    files: list[dict[str, str]]
    #: 派发时的运行时选项。★ 必须落盘 —— 重启后 `sweep_orphans` 要把
    #: pending 任务重新派发，不落盘的话恢复出来的任务会悄悄用上默认的
    #: 联网/图路/模型，与用户派发时的选择不一致（用户关了联网，重启后
    #: 任务偷偷联网了 —— 静默失效）。
    web_enabled: bool
    graph_enabled: bool
    model: str
    #: 派发时冻结的权限档（`manual` / `auto` / `read_only` / `full`）。
    #: ★ 为什么也要落盘：除了重启复用，更因为它决定了「后台那次自动放行
    #: 是依据哪一档发生的」。留痕里写了「按权限模式自动放行」，
    #: 而模式是全局可变的 —— 不落这一份，事后翻记录会看到
    #: 「用户设的是 manual，任务却自动批了 danger」这种假矛盾。
    #: 空串 = 旧记录 / 未指定，执行时按当前全局默认档跑。
    permission_mode: str
    #: 派发那一刻所在的工作区绝对路径（空串 = 没指定，文件工具走沙箱默认根）。
    #: ★ 与 `permission_mode` 同一类理由：这是**派发时**的用户决定，必须冻结落盘。
    #: 后台任务可以跑几十分钟，其间用户可能在输入框下面换项目 ——
    #: 换完之后这个任务往新目录里写文件，而留痕写的是它开始时的时间，
    #: 事后翻记录只会看到一桩对不上的账。
    workdir_root: str
    #: 数字员工 id（工作空间模块）。空串 = 普通后台子任务。
    #: 非空时子图注入该员工的 persona / 工具白名单，完成后自动向群组汇报。
    employee_id: str
    #: 归属群组 id（工作空间模块）。与 employee_id 同时非空时，任务完成后
    #: 自动生成一条群组消息（进度汇报）。空串 = 不属于任何群组。
    group_id: str
    # ---- 任务板缝合字段（施工图 §2.6）。三个都可以是空串，含义是「不属于看板」 ----
    #: 归属的协作 run。`sweep_orphans` 重启后靠它把任务重新归到那次协作名下。
    run_id: str
    #: 归属的看板卡片（`run_tasks.id`）。★ 这是两份唯一的真相的缝合键：
    #: 看板状态与执行状态各有一套词表，对账时只认这一条键，
    #: 不靠 title 相似、也不靠时间接近 —— 那两种推断都会在界面上撒出漂亮的谎。
    board_card_id: str
    #: 这个任务是从哪条路派出来的：`board` / `supervisor` / `employee_private`。
    #: 审计与「员工在飞任务数」（认领轮的输入）都按它统计。空串 = 旧记录，不知道。
    source: str
    #: 完成汇报已送达主对话的时间（ISO）。键不存在 = 还没有汇报过。
    #: ★ 与 `status=succeeded` 分开：任务成功 ≠ 用户已经被告诉了。
    #: 汇报是独立的一步（见 `app/tasks/report.py`），它可能失败、可能还没轮到。
    reported_at: str
    #: 汇报已经尝试过的次数（失败也计数，见 TASK_REPORT_MAX_ATTEMPTS）。
    report_attempts: int
    #: 当前挂起的提问载荷（只在 `waiting_input` 上有）。形状同 `questions.models`
    #: 的 interrupt payload。★ 必须落盘：用户刷新页面后要能重新看到卡片，
    #: 而「哪个 call_id / 哪些问题」只有执行器手里那一份。
    pending_question: dict[str, Any]
    depends_on: list[str]
    requires_file: bool


def make_task_id() -> str:
    """生成一个 task_id。

    用 `uuid4().hex[:16]` 而不是完整 hex：它要拼进 `subtask_<id>` 之后
    仍满足不变量 47 的 `^[A-Za-z0-9_-]{1,64}$`（`subtask_` 占 8 个字符，
    16 位 id 总长 24，留足余量）。
    """
    import uuid

    return uuid.uuid4().hex[:16]


def is_valid_task_id(task_id: str) -> bool:
    """
    校验 task_id。

    ★ 复用会话的正则，而不是复制一份
    两者都直接变成文件路径（`<task_id>.jsonl`）与 URL 路径段
    （`/api/tasks/{task_id}`），是同一类攻击面 ——
    两份正则将来必然漂移，而漂移的那一份就是漏掉的那一份。
    """
    from app.conversations.store import is_valid_thread_id

    return is_valid_thread_id(task_id)


def truncate_label(text: str, limit: int = PROGRESS_LABEL_CHARS) -> str:
    """把一句话压到能在界面上显示的长度（超长时补省略号）"""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)] + "…"


def new_task_record(
    *,
    task_id: str,
    thread_id: str,
    agent: str,
    title: str,
    instruction: str,
    parent_turn_id: str = "",
    created_at: str = "",
    web_enabled: bool = True,
    graph_enabled: bool = True,
    model: str = "",
    permission_mode: str = "",
    workdir_root: str = "",
    employee_id: str = "",
    group_id: str = "",
    run_id: str = "",
    board_card_id: str = "",
    source: str = "",
    depends_on: Optional[list[str]] = None,
    requires_file: bool = False,
) -> TaskRecord:
    """构造一条新任务记录（状态恒为 `pending`）"""
    return TaskRecord(
        task_id=task_id,
        thread_id=thread_id,
        parent_turn_id=parent_turn_id or "",
        agent=agent,
        title=title or "",
        instruction=instruction or "",
        status=STATUS_PENDING,
        created_at=created_at or _now_iso(),
        web_enabled=bool(web_enabled),
        graph_enabled=bool(graph_enabled),
        model=(model or "").strip(),
        permission_mode=(permission_mode or "").strip(),
        workdir_root=(workdir_root or "").strip(),
        employee_id=(employee_id or "").strip(),
        group_id=(group_id or "").strip(),
        run_id=(run_id or "").strip(),
        board_card_id=(board_card_id or "").strip(),
        source=(source or "").strip(),
        depends_on=list(depends_on or []),
        requires_file=bool(requires_file),
    )


def _now_iso() -> str:
    from app.conversations.models import now_iso

    return now_iso()


# ---------- 索引 ----------

INDEX_VERSION = 1
TASK_FILE_VERSION = 1

#: 事件文件里的记录类型（与 `conversations/store.py` 的 `meta` / `turn` 同构）
EVENT_TASK = "task"
EVENT_PROGRESS = "progress"


def record_from_dict(data: dict[str, Any]) -> Optional[TaskRecord]:
    """从索引 / 文件里还原一条记录；缺 task_id 或 id 不合法时返回 None

    与 `_read_index` 逐字段挑拣的做法一致：**不信任磁盘上的内容**。
    索引是可以被手工改坏的，坏掉的字段不能变成运行时错误。
    """
    task_id = str(data.get("task_id") or "")
    if not is_valid_task_id(task_id):
        return None
    status = str(data.get("status") or STATUS_PENDING)
    if status not in ALL_STATUSES:
        status = STATUS_PENDING
    record: TaskRecord = TaskRecord(
        task_id=task_id,
        thread_id=str(data.get("thread_id") or ""),
        parent_turn_id=str(data.get("parent_turn_id") or ""),
        agent=str(data.get("agent") or ""),
        title=str(data.get("title") or ""),
        instruction=str(data.get("instruction") or ""),
        status=status,
        created_at=str(data.get("created_at") or ""),
        # 运行时选项：旧记录（改造前落盘的）缺省时回退默认 ——
        # 开联网 / 开图路 / 跟随全局模型。
        web_enabled=bool(data.get("web_enabled", True)),
        graph_enabled=bool(data.get("graph_enabled", True)),
    )
    # model 是可选字符串：空串不带（与 error/result 同一约定）
    model = data.get("model")
    if isinstance(model, str) and model:
        record["model"] = model
    # 工作空间 / 任务板字段：可选字符串，非空才带
    # ★ 这三键加一行的意思是：`TaskRecord` 上新增字段的人**必须同时改这里**。
    for key in ("employee_id", "group_id", "run_id", "board_card_id", "source",
                "permission_mode", "workdir_root", "resume_mode", "resume_note",
                "reported_at"):
        value = data.get(key)
        if isinstance(value, str) and value:
            record[key] = value  # type: ignore[literal-required]
    attempts = data.get("report_attempts")
    dependencies = data.get("depends_on")
    if isinstance(dependencies, list):
        record["depends_on"] = [v for v in dependencies if isinstance(v, str) and is_valid_task_id(v)]
    if isinstance(data.get("requires_file"), bool):
        record["requires_file"] = data["requires_file"]
    if isinstance(attempts, int) and attempts > 0:
        record["report_attempts"] = attempts
    # ---- 可选字段：只有存在且非空才带 ----
    # 直接 `data.get(k, "")` 会把「没这个键」和「有但为空」压成同一个结果，
    # 于是回执里就多出一堆 `""` —— 那正是本项目要避开的「看起来像真数据」。
    for key in ("started_at", "finished_at", "error", "result"):
        value = data.get(key)
        if isinstance(value, str) and value:
            record[key] = value  # type: ignore[literal-required]
    usage = data.get("usage")
    if isinstance(usage, dict) and usage:
        clean = {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
        if clean:
            record["usage"] = clean
    files = data.get("files")
    if isinstance(files, list) and files:
        record["files"] = [f for f in files if isinstance(f, dict)]
    pending_question = data.get("pending_question")
    if isinstance(pending_question, dict) and pending_question:
        record["pending_question"] = pending_question
    return record
