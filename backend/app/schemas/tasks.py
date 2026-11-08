"""
后台子任务的接口数据模型（Supervisor 架构，见 ADR 0019）。

★ 为什么要单独一个 schemas 文件，而不用 TypedDict 直出

`app/tasks/models.py` 里的 `TaskRecord` 是**存储层**的形状
（落盘的就是它，字段名与 jsonl 一一对应）。接口要的是**展示层**的形状：
把 `instruction` 藏起来（可能很长且含内部细节）、
把「有没有结果」表达成「有没有 result 字段」而不是 `result: null`。

两套形状刻意分开：存储层跟着落盘格式变，接口层跟着前端变 ——
让它们共用一个定义，就等于把这两件事绑死。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ProgressEntryOut(BaseModel):
    """
    一步进度的对外形状。

    ★ 可选字段一律 `Optional[...] = None` 且序列化时排除 `None`
    这不是「JSON 里写 null」的问题，而是**语义**问题：
    `detail` 缺失 = 「这一步没有补充说明」；
    `detail: null` 看起来像「补充说明是空的」——
    而后者是一个从未发生过的状态。填 0 / 填空串同理（见 `sse.md` 的约定）。
    """

    seq: int = Field(description="单调递增序号，前端据此去重与排序")
    at: Optional[str] = Field(default=None, description="ISO 时间戳")
    kind: str = Field(
        description=(
            "plan/sources/tool_call/tool_result/approval/question/answer/error/narrative"
        )
    )
    label: str = Field(description="人话描述，后端给（前端不自己拼）")
    status: str = Field(description="running/ok/failed/awaiting")
    detail: Optional[str] = Field(default=None, description="工具名等补充信息")
    #: 仅 `narrative` 有：整段正文（可能多行）。与 label 分工见 models.ProgressEntry。
    text: Optional[str] = Field(default=None, description="narrative 的整段正文")
    call_id: Optional[str] = Field(default=None, description="仅工具调用与审批类有")

    model_config = {"exclude_none": True}


class TaskOut(BaseModel):
    """
    一个后台子任务（列表项与详情项共用）。

    ⚠️ `instruction` **刻意不出现**在这个模型里。
    它是派给子 Agent 的完整描述，可能很长，也可能含内部表述
    （比如「收件人是张三」这种从对话里拆出来的细节）。
    前端渲染的始终是 `title`。
    """

    task_id: str
    thread_id: str
    parent_turn_id: Optional[str] = None
    depends_on: Optional[list[str]] = None
    #: 内部专员名 —— **开发者模式才显示**，基础界面不出现
    agent: str
    title: str
    status: str
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[str] = None
    usage: Optional[dict[str, int]] = None
    files: Optional[list[dict[str, str]]] = None
    #: ★ 可选：真的超了上限才带。界面上据此说明「只显示了最近 N 步」——
    #: 悄悄截断与填 0 是同一类谎报（与 `done.files` 可选字段同一约定）
    total_steps: Optional[int] = None
    progress: list[ProgressEntryOut] = Field(default_factory=list)
    #: 这个任务是从哪条路派出来的：`supervisor` / `board` / `employee_private`。
    #: ★ 旧记录没有这一维（空串），刻意**不带**这个字段而不是回一个猜测值 ——
    #: 界面上「没标来源」与「标了 supervisor」是两件事，后者是对历史的断言。
    source: Optional[str] = None
    #: ★ 只有 `interrupted` 才带（见 `tasks/models.py` 的同名字段注释）：
    #: `checkpoint` / `approval` / `reconcile`。别的状态带上是矛盾的 ——
    #: 「任务还在跑」没有「怎么续」这个问题。
    resume_mode: Optional[str] = None
    resume_note: Optional[str] = Field(
        default=None, description="给用户看的一句话（为什么中断 / 续跑要注意什么）"
    )
    #: ★ 只有 `waiting_input` 才带：模型正在等回答的那张卡片。
    #: 形状与 SSE 的 `question` 事件一致；前端刷新 / 换设备后靠它重画问卷 ——
    #: 只给状态不给内容，界面只能显示「等你回答」却说不出在等什么。
    pending_question: Optional[dict[str, Any]] = None

    model_config = {"exclude_none": True}


class TaskListResponse(BaseModel):
    """
    任务列表。

    ★ `storage` 字段是本接口最重要的一处设计
    任务存储是**可降级依赖**（磁盘写失败、目录不可写）。
    那种情况下接口**不返回空列表**，而是返回 `storage="unavailable"` ——
    因为空列表会被前端渲染成「没有任务」，
    而真相是「查不了」。这两件事对用户不是一回事，
    而「谎报没有任务」恰好是这个项目最在意的静默失效形态。
    """

    storage: str = Field(description="ok / unavailable")
    total: int = Field(description="符合过滤条件的任务总数（用于分页）")
    limit: int
    offset: int
    #: 这一屏按哪个口径筛的：`chat` = 只有主对话派发的子智能体，
    #: `workspace` = 只有团队任务，`all` = 不筛。
    #: ★ 它是**回执**而不是开关：前端传了 `scope=chat` 而响应里回的是 `all`，
    #: 说明有一端改错了 —— 而这正是「团队任务混进后台任务页」重演时的第一现场。
    scope: str = Field(default="chat", description="本响应的来源过滤口径")
    tasks: list[TaskOut] = Field(default_factory=list)
    storage_error: Optional[str] = Field(
        default=None, description="storage=unavailable 时的原因"
    )

    model_config = {"exclude_none": True}


class TaskCancelResponse(BaseModel):
    """
    取消任务的响应。

    ★ 为什么 `ok=false` 不是 404
    任务确实存在，只是已经结束了。
    报成「不存在」会让界面显示一个误导性的错误
    （用户会以为是自己记错了 task_id）。
    """

    ok: bool
    task_id: str
    #: 已经是终态时给一句人话，让界面能直接显示
    reason: Optional[str] = None

    model_config = {"exclude_none": True}


class TaskDeleteResponse(BaseModel):
    """
    删除任务的响应。

    ★ 与取消接口同一条约定：`ok=false` = 「这次没删」，不是「任务不存在」
    还在进行的任务删不得（jsonl 一删，子 Agent 还在往里写，会产生孤儿文件），
    返回 `ok=false` + `reason` 让界面提示「先取消再删除」。
    """

    ok: bool
    task_id: str
    reason: Optional[str] = None

    model_config = {"exclude_none": True}


class TaskResumeResponse(BaseModel):
    """
    续跑中断任务的响应。

    ★ `needs_confirm=true` 不是错误
    最后一次工具调用可能已经生效（比如文件已经写完、请求已经发出去），
    这时接口**不直接续跑**，而是把风险原样交回给用户：
    「这一步可能已经做了，继续会重做一次」。用户确认后带 `acknowledge=true`
    再调一次即可。和取消接口同一条约定：`ok=false` 表示「这次没做」，
    而不是「任务不存在」（那才是 404）。
    """

    ok: bool
    task_id: str
    #: 需要用户先核对副作用再决定是否继续
    needs_confirm: bool = False
    #: 人话：为什么没续跑 / 续跑后会怎样
    reason: Optional[str] = None

    model_config = {"exclude_none": True}


class TaskStatsResponse(BaseModel):
    """
    任务统计（运维用，不变量 23：状态查询接口必须自己解析惰性单例）。

    ★ `gate` 暴露审批闸门状态的理由
    闸门死锁是这个系统里唯一「界面完全看不出来」的故障：
    没有报错、任务状态是 waiting_approval、审批接口返回 matched=false，
    用户只看到「点了没反应」。不变量 23 的精神是
    「元数据说错比没有元数据更糟」——同理，**元数据缺失**也让人无从下手。
    """

    enabled: bool
    total: int
    by_status: dict[str, int]
    storage: str = "ok"
    gate: dict[str, Any] = Field(default_factory=dict)

    model_config = {"exclude_none": True}
