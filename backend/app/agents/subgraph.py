"""
子图 —— 后台子任务的私有执行体（Supervisor 架构，见 ADR 0019）。

★ 为什么子 Agent 需要独立子图，而不是主图里的一个节点

Router 时代 5 个专员是同一个 `run_planner_turn` 的 5 个入口，共享一张
`AgentState`（ADR 0014，理由是「子 Agent 不是自治循环」）。Supervisor 时代
这个前提不成立了：子任务要在主对话那一轮**结束之后**继续跑，
还要能跨 SSE 断连活下来。

共享全局状态在主图里就已经有代价（ADR 0014:21-23 记着）——
`email_agent` 能读到 `calendar_agent` 的审批留痕，没有机制阻止，
也没有文档说它不该读。放到子图上，这个代价会变成**真的 bug**：
4 个子任务并行时，它们互相踩对方的状态。

所以这里换的是**隔离粒度**：
- `SubtaskState` 是**全新的独立 TypedDict**，不复用主图的 `AgentState`；
- 每个子图实例有自己的 `messages`（私有 scratchpad）、自己的 sources；
- 主图**看不到子图的任何状态**，子图也看不到主图的。

★ ⚠️ 但 `planner ⇄ tool_executor` 这个环一个字都不能改

不变量 6 的重放安全完全建立在它上面：
    planner →（有待执行工具）→ tool_executor →（还有剩余）→ tool_executor → … → planner

`interrupt()` 在恢复时会导致「节点从头重放」。如果一次执行多个工具，
重放会把已执行的工具**再执行一遍**（`delete` 删第二遍、邮件发两次）。
而这个论证有一个隐含前提：**一个子图内串行**。

子图之间的并发由 `ApprovalGate`（进程内 FIFO 闸门）处理审批排队，
**不靠重排拓扑** —— 那样等于重审 ADR 0002 的重放安全结论。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.config import settings

#: 子图的工具队列与主图是同一个形状 —— `run_planner_turn` / `tool_executor_node`
#: 直接复用（它们吃的就是这个结构）。所以 `PendingCall` 直接复用主图那一个，
#: 不另立一份：两份形状会漂移，而漂移的那份在 `interrupt()` 时才炸。
from app.agents.state import PendingCall


class SubtaskState(TypedDict, total=False):
    """
    一个后台子任务的私有图状态。

    ★ 与主图 `AgentState` 的关系：**刻意不复用**。
    两者没有任何字段是共享的，因为它们承载的是两种不同的东西：
    主图状态是「这一轮对话进行到哪」，子图状态是「这个子任务干到哪」。

    对照主图 `AgentState`，这里**刻意没有**的字段：
        intent / intent_confidence —— 子任务已经过了分类阶段，没有意图
        agent_chain               —— 子图内只有一个 Agent，链路无意义
        pending_approval          —— 审批挂在 checkpointer 的 interrupt 上，
                                    不是一个可以传进传出的字段
        user_input                —— ★ 有，但语义不同：它是**任务描述**，
                                    不是用户这一轮说的话。见 `instruction`。
        context                   —— 主图的共享上下文袋，没有跨图的意义
    """

    # ---------- 任务本身 ----------
    #: 任务描述（Supervisor 派下来的完整指令）。★ 不是「用户这一轮说了什么」——
    #: 派发时可能被拆解、补充了上下文，与原始输入不是一回事。
    instruction: str
    #: 内部专员名（pc_agent / file_agent / ...），决定工具权限与 prompt
    agent: str
    task_id: str
    #: 协作 run id（任务板 / 群组派发的批次标识）。
    #: ★ 为什么要进子图状态而不是只留在执行器的局部变量里
    #: Phase 5 之后**判定发生在节点内**，自动放行的留痕要带 run_id 才定位得准
    #: （护栏 6：群组里 thread_id 共用，少了 run_id 三条审批记录在数据结构上无法区分）。
    #: 执行器自己拿得到 run_id，但节点拿不到 —— 跨这一层的信息只能走状态。
    run_id: str
    #: 归属的用户会话 —— 只用于审批 key 与事件落盘，**不用于** checkpointer
    #: （checkpointer 用 `subtask_<task_id>`，见 ADR 0019 的命名空间隔离）
    thread_id: str
    #: 数字员工 id（工作空间模块）。空串 = 普通后台子任务（走全局人设）。
    #: 非空时 runner 注入该员工的 persona + 工具白名单（见 `workspace/employee_runtime.py`）。
    employee_id: str

    # ---------- 私有 scratchpad ----------
    #: ★ 这就是 ADR 0014:83 说的「独立内部循环 + 私有 scratchpad」。
    #: 别的子任务读不到它，主图也读不到。
    messages: Annotated[list[AnyMessage], add_messages]
    #: 子 Agent 的中间产物（步骤笔记）。同样私有。
    scratchpad: list[dict[str, Any]]

    # ---------- 工具循环 ----------
    pending_calls: list[PendingCall]
    current_call: PendingCall | None
    tool_calls: list[dict[str, Any]]
    approval_log: list[dict[str, Any]]

    # ---------- 检索 ----------
    #: ★ 与主图的 `sources_ready` 同一个坑：检索为空（没命中 / 依赖全挂）
    #: 与「还没检过」是两回事，误判会白跑第二遍。
    sources: list[dict[str, Any]]
    sources_ready: bool

    # ---------- 结果 ----------
    active_agent: str
    final_answer: str
    sub_agent_result: str
    #: 失败说明（任务没完成时的可读原因）。非空 = 执行器按失败收尾并把它写进
    #: error —— 「没有正文却成功」与「没有正文且原因不明」这两种最糟的收尾
    #: 都被它堵住：终态要么给出答案、要么给出说得清原因的失败。
    final_failure: str

    # ---------- 本轮选项 ----------
    web_enabled: bool
    graph_enabled: bool
    model: str
    #: 本任务生效的权限模式（Phase 5）。
    #: ★ 在**任务建立时**算一次并冻结，不在每次工具调用时现读
    #: 后台任务可以跑几十分钟，其间用户可能来回切档位。跑一半从 `manual` 被调成
    #: `auto`，等于中途无人认领地把审批关掉了 —— 那正是留痕里最不该出现的一种记录。
    #: 要改变一个在跑的任务，先取消再用新档位重派。
    permission_mode: str
    #: 派发那一刻冻结的工作区绝对路径（空串 = 未指定，文件工具走沙箱默认根）。
    #: ★ 与 `permission_mode` 同一理由：后台任务可以跑几十分钟，其间用户完全
    #: 可能在输入框下面换项目。换完之后这个任务往新目录里写文件，
    #: 而它的留痕写的是开始时的时间 —— 事后翻记录只会看到一桩对不上的账。
    workdir_root: str


def initial_subtask_state(
    *,
    instruction: str,
    agent: str,
    task_id: str,
    thread_id: str,
    web: bool = True,
    graph: bool = True,
    model: str | None = None,
    employee_id: str = "",
    permission_mode: str | None = None,
    workdir_root: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """
    构造子任务的初始状态（**整份**，不是增量）。

    ★ 与主图 `initial_state()` 的关键区别

    主图返回的是**本轮增量**，因为历史已在 checkpointer 里（`add_messages` 会追加）。
    子图每个 task_id 是一次全新的执行 —— 没有历史要继承 ——
    所以这里返回完整状态，包括 `messages: [HumanMessage(instruction)]`。

    附件正文不再拼进消息：子任务的输入是 Supervisor 拆解出来的描述，
    它可能已经包含了从主对话里取出的附件要点。真的需要原文附件时，
    由 Supervisor 在 `instruction` 里说明用哪个 `read` 自己去读 ——
    附件在 `read` 的白名单里，这条路是通的。
    """
    from langchain_core.messages import HumanMessage

    from app.permissions.policy import effective_mode

    return SubtaskState(
        instruction=instruction or "",
        agent=agent or "",
        task_id=task_id or "",
        run_id=run_id or "",
        thread_id=thread_id or "",
        employee_id=(employee_id or "").strip(),
        messages=[HumanMessage(content=instruction or "")],
        scratchpad=[],
        pending_calls=[],
        current_call=None,
        tool_calls=[],
        approval_log=[],
        sources=[],
        sources_ready=False,
        active_agent="",
        final_answer="",
        sub_agent_result="",
        final_failure="",
        web_enabled=bool(web),
        graph_enabled=bool(graph),
        model=(model or "").strip(),
        permission_mode=effective_mode(permission_mode),
        workdir_root=(workdir_root or "").strip(),
    )


# ---------- Per-Agent State Projection（子图版）----------

# ★ 与主图 `NODE_INPUT_SCHEMAS` 同一套机制，但**投影定义是分开的**。
#
# 为什么不复用主图那三个（`PlannerInput` / `ToolExecutorInput`）：
# 它们读的字段是主图特有的（`agent_chain` / `active_agent` 的路由语义）。
# 子图里 `agent` 是**固定的**，不需要靠 `active_agent` 猜「我现在是谁」——
# 这是子图隔离带来的一个实质简化：那条「上一次是谁」的判断整个不需要了。
#
# 维护规则与主图相同：给节点新增状态读取时必须同步扩投影字段。


class SubtaskPlannerInput(TypedDict, total=False):
    """
    子图 planner 的状态投影。

    ★ `user_input` 在这里被映射到 `instruction`
    —— `run_planner_turn` 用 `state["user_input"]` 作为 RAG 检索 query，
    而子图状态里那个字段叫 `instruction`（语义是「任务描述」）。
    所以适配层（`nodes/subtask.py`）负责把投影里的 `instruction` 译成
    `user_input` 再交给复用的实现。详见该文件的注释。
    """

    instruction: str
    agent: str
    messages: list[AnyMessage]
    active_agent: str
    sources: list[dict[str, Any]]
    sources_ready: bool
    tool_calls: list[dict[str, Any]]
    web_enabled: bool
    graph_enabled: bool
    model: str
    employee_id: str
    #: `UserPromptSubmit` 钩子的补充上下文（会被拼进 system prompt 末尾）
    hook_context: str
    #: ★ 上下文管理用的身份字段（不进工具权限判定）：
    #: 摘要按 (thread_id, task_id) 隔离；工具输出落盘按 thread_id 归属。
    #: 缺了它们，摘要会退化成「同会话同专员共用一份」，工具输出也清理不掉。
    thread_id: str
    task_id: str


class SubtaskToolExecutorInput(TypedDict, total=False):
    """
    子图 tool_executor 的状态投影。

    与主图 `ToolExecutorInput` 一致：只读执行所需的队列、留痕上下文，
    加上构造 `ToolContext` 所需的三个身份字段（原因见 `ToolExecutorInput` 的注释）。
    对话历史、检索来源这些与「执行一个工具」无关的东西不进这个节点的视野。

    ★ `thread_id` 取的是**状态里**那个（用户的会话），
    不是 `config["configurable"]["thread_id"]` —— 后者是 checkpointer 的命名空间
    key（`subtask_<task_id>`，见 `subtask_thread_id`）。拿它当会话身份的话，
    每个子任务都会得到一个独属于自己的 shell 会话，`cd` 之后重新进来就丢；
    更糟的是它看上去完全正常，没人会怀疑一个前缀字符串。
    """

    pending_calls: list[PendingCall]
    active_agent: str
    tool_calls: list[dict[str, Any]]
    approval_log: list[dict[str, Any]]
    thread_id: str
    task_id: str
    employee_id: str
    run_id: str
    #: 本轮生效的权限模式（缺了它子图会一律按默认档判定，
    #: 用户在设置里调的那一档对后台任务不生效 —— 且不报错）
    permission_mode: str
    #: 工作区根目录：`ToolContext.workspace_root` 只能从这里来。
    #: 投影里缺这一位的症状同样是静默的 —— 子任务在沙箱里找一个
    #: 真实存在于用户项目里的文件，然后回一句「没找到」。
    workdir_root: str


#: 节点名 → 输入投影
SUBGRAPH_NODE_INPUT_SCHEMAS: dict[str, type] = {
    "planner": SubtaskPlannerInput,
    "tool_executor": SubtaskToolExecutorInput,
}


def subtask_thread_id(task_id: str) -> str:
    """
    子图专用的 thread_id（checkpointer 的 key）。

    ★ 前缀是**不变量 58** 的载体，不是命名偏好

    共享 Checkpointer 实例是刻意的（省一套 Redis 连接管理与降级逻辑），
    但**命名空间必须隔离** —— 一个子任务的消息流混进用户会话历史，
    会让 `_trim_history` 的 20 条窗口被后台任务挤满，
    也会让 `seed_thread_from_history` 播回一堆用户从没说过的话。

    前缀让「不相撞」成为结构上的保证；
    主对话入口拒绝该前缀（`graph.get_agent_event_stream`）挡住手工构造的撞入。
    两者缺一不可 —— 只做前缀，用户仍可以传 `thread_id=subtask_xxx` 把自己写进子图。

    下划线让不变量 47 的正则 `^[A-Za-z0-9_-]{1,64}$` 天然放行
    （task_id 自身 16 位，前缀 8 位，总长 24，留足余量）。
    """
    return f"subtask_{task_id}"


#: 子图内需要拒绝的 thread_id 前缀（见 `subtask_thread_id` 与不变量 58）
SUBTASK_THREAD_PREFIX = "subtask_"


def is_subtask_thread_id(thread_id: str) -> bool:
    """这个 thread_id 属于子图命名空间（主对话入口必须拒绝它）"""
    return bool(thread_id) and thread_id.startswith(SUBTASK_THREAD_PREFIX)


# ---------- 图构建 ----------
def build_subgraph(checkpointer: Any = None):
    """
    构建并编译子图（每次派发一个子任务时调用一次）。

    ★ 拓扑与主图的 `planner ⇄ tool_executor` **完全同构** —— 这是刻意的

    子图只需要一个节点名（`planner`）而不是五个，因为 `agent` 是状态字段：
    同一个 planner 实现跑不同的专员，靠的是 `tool_registry` 按身份给不同工具集
    （不变量 8），而不是靠不同的节点。

    不需要 checkpointer 也能编译（测试用）—— 但**后台执行必须传**：
    没有 checkpointer，`interrupt()` 挂起后无法恢复，审批就成了永久等待。
    """
    from langgraph.graph import END, START, StateGraph

    from app.agents.nodes.subtask import (
        subtask_planner_node,
        subtask_tool_executor_node,
    )

    builder = StateGraph(SubtaskState)

    # ---------- 节点（挂上自己的输入投影，见 SUBGRAPH_NODE_INPUT_SCHEMAS）----------
    builder.add_node(
        "planner", subtask_planner_node, input_schema=SUBGRAPH_NODE_INPUT_SCHEMAS["planner"]
    )
    builder.add_node(
        "tool_executor",
        subtask_tool_executor_node,
        input_schema=SUBGRAPH_NODE_INPUT_SCHEMAS["tool_executor"],
    )

    # ---------- 边 ----------
    builder.add_edge(START, "planner")

    def _route_after_planner(state: Any) -> str:
        """有待执行工具就去执行器，否则收尾"""
        return "tool_executor" if state.get("pending_calls") else END

    def _route_after_tool(state: Any) -> str:
        """
        队列没清空就继续执行下一个，清空了就回 planner 继续推理。

        ⚠️ 为什么不回「当前子 Agent」：子图里只有一个 planner（`agent` 是状态字段），
        所以回到 planner 就是回到「自己」—— 主图那条 `route_after_tool`
        的多分支在这里退化成两分支，语义完全一致。
        """
        return "tool_executor" if state.get("pending_calls") else "planner"

    builder.add_conditional_edges(
        "planner", _route_after_planner, {"tool_executor": "tool_executor", END: END}
    )
    builder.add_conditional_edges(
        "tool_executor",
        _route_after_tool,
        {"tool_executor": "tool_executor", "planner": "planner"},
    )

    return builder.compile(checkpointer=checkpointer)


#: 子图内每次工具循环至少消耗 2 步（planner + executor），留足余量。
#: 与主图 `RECURSION_LIMIT` 同口径。
SUBGRAPH_RECURSION_LIMIT = 2 * settings.AGENT_MAX_TOOL_STEPS + 10


__all__ = [
    "SUBGRAPH_NODE_INPUT_SCHEMAS",
    "SUBGRAPH_RECURSION_LIMIT",
    "PendingCall",
    "SubtaskPlannerInput",
    "SubtaskState",
    "SubtaskToolExecutorInput",
    "build_subgraph",
    "initial_subtask_state",
    "is_subtask_thread_id",
    "subtask_thread_id",
]
