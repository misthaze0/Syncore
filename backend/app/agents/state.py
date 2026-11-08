"""
AgentState 定义 —— LangGraph 图状态

字段设计要点:
- `messages` 用 `add_messages` 减速器自动累积，配合 Redis Checkpointer 实现多轮记忆
- `pending_calls` 是本轮 LLM 决定的「待执行工具队列」，tool_executor 每次只消费一个，
  这样每次工具执行都是独立的 superstep —— 是「一次执行只触发一次 interrupt」的前提，
  避免节点重放导致的重复副作用
- `agent_chain` / `tool_calls` 用于前端 AgentStatus / ToolCallView 可视化

★ Per-Agent State Projection（Per-Agent 状态投影）

图状态（AgentState）是全局唯一的持久化容器，但**每个节点读到的状态是被收窄的**：
`build_graph` 为每个节点声明了 `input_schema`（见 NODE_INPUT_SCHEMAS），
节点只会收到自己声明需要的字段的投影。这样做的价值：

1. **契约显式化**：一个节点依赖哪些状态、污染哪些状态，直接看投影定义即可，
   不必通读节点实现 —— 新增子 Agent 时不会顺手把别的专员的内部状态也读了。
2. **误用编译期可见**：节点里 `state["xxx"]` 读投影外字段，
   在类型检查与测试（test_agents.py 的投影用例）里都会立刻暴露。
3. **写入天然收窄**：LangGraph 节点本来就只返回增量更新（partial update），
   写侧无需额外机制；投影补的是读侧。

注意：投影只影响「节点运行时收到什么」，不影响图状态本身 ——
Checkpointer 里的完整状态、条件路由函数（它们读的是合并后的全局状态）都不变。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class PendingCall(TypedDict, total=False):
    """待执行的工具调用"""

    call_id: str          # 工具调用唯一 ID（与前端 ToolCallView 对应）
    tool_name: str
    args: dict[str, Any]
    danger_level: str     # safe / moderate / danger
    description: str      # 工具用途，用于审批弹窗展示


class AgentState(TypedDict, total=False):
    """多 Agent 图状态"""

    # ---------- 对话 ----------
    messages: Annotated[list[AnyMessage], add_messages]  # 对话历史（自动累积）
    user_input: str                                      # 当前轮用户输入
    final_answer: str                                    # 当前轮最终回答

    # ---------- 意图与路由 ----------
    intent: str                 # pc / file / software / browser / general
    intent_confidence: float    # 意图置信度 0-1
    active_agent: str           # 当前正在执行的子 Agent 节点名

    # ---------- 工具调用 ----------
    pending_calls: list[PendingCall]      # 待执行工具队列
    current_call: PendingCall | None      # tool_executor 正在处理的调用
    tool_calls: list[dict[str, Any]]      # 本轮全部工具调用记录（含结果）
    pending_approval: dict[str, Any] | None  # 当前待审批操作
    approval_log: list[dict[str, Any]]    # 审批历史

    # ---------- 上下文 ----------
    sources: list[dict[str, Any]]   # RAG 检索来源
    # 检索是否已经**跑过**（而不是「是否检索到了东西」）。
    #
    # ★ 为什么需要它：主 Agent 会在识别意图的**同时**先把检索跑起来（见
    # `main_agent_node`，两件事只依赖同一个 `user_input`，串行等待等于把两段耗时相加）。
    # 于是 file_agent 不能再靠 `if not sources` 判断「要不要自己检索」——
    # 检索为空（知识库没命中 / 依赖全挂）与「还没检索过」是两回事，
    # 前者若被误判成后者，就会白跑第二遍检索。
    sources_ready: bool
    sub_agent_result: str           # 子 Agent 结果
    context: dict[str, Any]         # 共享上下文
    agent_chain: list[str]          # Agent 调用链（前端状态指示器）
    thread_id: str                  # 会话 ID（Checkpointer 用）
    #: 本轮轮次 ID（断流终止后台子任务时按它定位「本轮派发的任务」）
    turn_id: str

    # ---------- 本轮选项（Phase 8：用户对「怎么答」的干预） ----------
    # ★ 为什么放进图状态而不是只当作一次性参数
    # 它们要在**两个不同的节点**里生效：`web_enabled` 影响 planner 的工具绑定、
    # `graph_enabled` 影响 file_agent 的检索、`model` 影响 planner 的 LLM 构造。
    # 走图状态是唯一能让「同一轮的每个节点看到同一份选项」的地方，
    # 也让 HITL 恢复（Command(resume=…)）之后这些选项依然有效 ——
    # 审批挂起再恢复时，参数早就没了，状态还在。
    web_enabled: bool               # 是否允许联网搜索（默认 True）
    graph_enabled: bool             # 是否走图路召回（默认 True）
    model: str                      # 本轮模型名（空串 = 跟随全局配置）
    thinking: bool                  # 本轮是否开深度思考（None = 跟随全局配置）
    #: 本轮权限模式（Phase 5）。存的是**已经算完的生效值**：
    #: 「全局默认档 + 本轮收紧」由 `permissions.policy.effective_mode` 在入口算一次，
    #: 节点与工具只读这一个值。两处各算会出现「审批按 auto、文件按 manual」的分裂。
    permission_mode: str
    #: 本轮工作区的**绝对路径**（空串 = 没绑，文件工具走沙箱）。
    #:
    #: ★ 为什么存路径而不是工作区 id
    #: 与 `permission_mode` 同一个道理：入口算一次，全轮读同一个值。
    #: id 要在每个用目录的地方再查一次登记表，而登记表随时可能被另一个标签页改掉 ——
    #: 于是「这一轮的前半跑在 A 目录、后半跑在 B 目录」会静默发生。
    #: 路径在这里是**快照**，而绑定表只在 `chat` 入口被读一次。
    #:
    #: ★ 它同时是一条边界（见 `core/sandbox.writable_roots`）
    #: 绑了工作区不等于解除了沙箱：文件工具能碰的范围是「沙箱 + 授权目录 + 这一个」。
    workdir_root: str
    #: `UserPromptSubmit` 钩子要塞进上下文的文本（没有钩子时是空串）
    hook_context: str

    # ---------- Supervisor 的拆解结果（Phase 9，见 ADR 0019）----------
    # ★ 这三个字段是 supervisor → dispatch / respond 之间的契约。
    # 放在图状态里（而不是节点间直接传参）是因为
    # `route_after_plan` 条件边与两个下游节点都要读它们 ——
    # 图状态是唯一能让三者看到同一份值的地方。
    #
    # ⚠️ `plan_subtasks` 存的是 `SubtaskSpec.model_dump()` 的结果
    # （纯 dict 列表），不是 Pydantic 对象 ——
    # 图状态要进 Checkpointer，而序列化 dict 比序列化对象稳定
    # （换 LangGraph 版本时 Pydantic 类的模块路径可能变，dict 不会）。
    plan_mode: str                  # direct / delegate
    plan_reply: str                 # direct 时的回答 / delegate 时的开场白
    plan_subtasks: list[dict[str, Any]]   # 待派发子任务（见上）

    # ---------- direct 轮内的工具循环（可挂起，Phase 10） ----------
    # ★ 为什么不能像原来那样把 `messages` 放在一个函数里的局部变量
    #
    # 原实现把「LLM 流 → 执行工具 → 再调 LLM」整圈写在一个节点函数里。
    # 加入 `ask_user` 之后，这个循环必须能在中途**挂起**；而 LangGraph
    # 恢复时会把节点从头重放 —— 局部变量全部丢失，已经执行过的工具会被再执行一遍。
    #
    # 所以循环状态必须进图状态：每一步结束时把「这一轮已经发生的消息」存进
    # `turn_messages`，恢复后从那里重建上下文。这样挂起/恢复与普通重入走同一条路。
    #
    # `turn_messages` 用**覆盖**语义（不是 add_messages）：它只是本轮工具循环的
    # 临时草稿，答案落盘后会被清空；而 `messages` 才是跨轮历史。
    turn_messages: list[AnyMessage]
    #: 已挂起、等待 `ask_user` 节点处理的提问载荷（None = 没有）
    pending_question: dict[str, Any] | None
    #: direct 轮内已经调用过几次 LLM（用于工具步数上限）
    respond_step: int
    #: 本轮在工具循环里已经产出的文本（跨步累积，最后落成一条 AIMessage）
    turn_text: str
    #: 本轮是否已经产出最终回答（无工具调用而结束）
    respond_done: bool


#: 每轮用户消息的 id 前缀 —— 删除某一轮时靠它把 Checkpointer 里的消息**分段**。
#:
#: 一个轮次在上下文里可能不止两条消息（工具调用、工具结果、多段回答），
#: 逐条记录 id 既要在落盘时回查状态、又会被断连/中断搅乱；
#: 只给「这一轮的起点」一个稳定 id，相邻两个起点之间就是这一轮的全部痕迹。
TURN_MSG_PREFIX = "turn:"


def turn_message_id(turn_id: str) -> str:
    """某个轮次在模型上下文里的用户消息 id（与轮次文件的 `turn_id` 一一对应）"""
    return f"{TURN_MSG_PREFIX}{turn_id}"


def initial_state(
    user_input: str,
    thread_id: str,
    attachments: list[dict[str, Any]] | None = None,
    *,
    turn_id: str = "",
    web: bool = True,
    graph: bool = True,
    model: str | None = None,
    thinking: bool | None = None,
    permission_mode: str | None = None,
    workdir_root: str | None = None,
    hook_context: str = "",
) -> dict[str, Any]:
    """构造一轮对话的初始状态增量

    由于启用了 Checkpointer，历史 `messages` 已在检查点中，
    这里只提交本轮增量，`add_messages` 会自动追加。

    附件（拖进输入框的文件）：正文拼进本轮 HumanMessage（模型自然看到，
    历史窗口内可追问），而 `user_input` 保持干净 —— RAG 检索不该拿
    附件全文当 query。见 `app/attachments.py` 模块注释。

    `web` / `graph` / `model` 是本轮选项（见 `ChatRequest`），
    随状态进图，保证同一轮里所有节点看到的是同一份选择。

    `permission_mode` 是本轮**请求**的模式，进来先过一道 `effective_mode`：
    只允许比全局默认更紧，不允许更松（理由见 `permissions/policy.py`）。
    存进去的已经是算好的生效值 —— 全轮只有一个地方做过这个判断。

    `turn_id` 用来给本轮的 HumanMessage 一个**稳定 id**（见 `TURN_MSG_PREFIX`）：
    用户删掉某一轮时，后端要能在 Checkpointer 里认出「从哪到哪」是这一轮。
    没传（例如子图路径）就不带 id，由 `add_messages` 自行分配。
    """
    from langchain_core.messages import HumanMessage

    from app.attachments import compose_message
    from app.permissions.policy import effective_mode

    message = HumanMessage(
        content=compose_message(user_input, attachments),
        id=turn_message_id(turn_id) if turn_id else None,
    )

    return {
        "messages": [message],
        "user_input": user_input,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "intent": "",
        "intent_confidence": 0.0,
        "active_agent": "",
        "pending_calls": [],
        "current_call": None,
        "tool_calls": [],
        "pending_approval": None,
        "sources": [],
        "sources_ready": False,
        "sub_agent_result": "",
        "final_answer": "",
        "web_enabled": bool(web),
        "graph_enabled": bool(graph),
        "model": (model or "").strip(),
        "thinking": thinking,
        "permission_mode": effective_mode(permission_mode),
        # 工作区路径由入口从绑定表算好（`api/chat.py`）。这里**不再查表**：
        # 传进来什么就是什么，空串 = 不指定 = 文件工具走沙箱默认根。
        "workdir_root": (workdir_root or "").strip(),
        "hook_context": (hook_context or "").strip(),
        # ---------- Supervisor 拆解结果 ----------
        # 每轮重置：上一轮的 plan 绝不能影响这一轮 ——
        # 残留的 `plan_mode="delegate"` 会让新会话一进去就跳过作答。
        "plan_mode": "",
        "plan_reply": "",
        "plan_subtasks": [],
        # direct 轮内的工具循环（见 AgentState 注释）。每轮必须复位 ——
        # 残留的 `turn_messages` 会让下一轮把上一轮的工具调用当成自己的上下文。
        "turn_messages": [],
        "pending_question": None,
        "respond_step": 0,
        "turn_text": "",
        "respond_done": False,
    }


# ---------- Per-Agent State Projection —— 节点级输入投影 ----------
# 每个 TypedDict 列出对应节点**允许读取**的字段。
# build_graph 通过 add_node(input_schema=…) 把投影接到运行时；
# 字段清单必须与节点实现实际读取的字段严格一致 ——
# 节点里多读一个字段，测试（test_planner_receives_projected_state 等）就会失败。
#
# 为什么只有输入投影、没有输出投影：
#   LangGraph 1.x 移除了节点级 output_schema（0.2.x 时代的能力）。
#   而写侧收窄本来就由 LangGraph 的增量更新语义天然保证 ——
#   节点只能「声明自己写入了什么」，不可能悄悄覆写整个状态。
#
# ⚠️ 主图与子图的投影是**两套**（Supervisor 架构，见 ADR 0019）
# 主图三个节点（supervisor / dispatch / respond）用下面这一套；
# 子图两个节点用 `app/agents/subgraph.py::SUBGRAPH_NODE_INPUT_SCHEMAS`。
# 不复用是因为它们读的是不同的东西 ——
# 主图读「这一轮对话进行到哪」，子图读「这个子任务干到哪」。


class SupervisorInput(TypedDict, total=False):
    """
    supervisor 节点的状态投影。

    只需要「本轮用户说了什么」与「本轮的两个开关」——
    它不读对话历史（拆解是一次性的判断，不需要历史），
    也不读任何子任务状态（子图在主图之外，它的状态这里根本看不到）。

    ⚠️ `web_enabled` / `graph_enabled` 必须在这里：
    `direct` 模式下 supervisor 会**预取**检索（与意图识别并发，不变量 50），
    而检索要不要带图路由由 `graph_enabled` 决定。
    投影里缺它 = 预取时读不到开关 = **用户关掉的图路又被偷偷走了一遍**。
    """

    user_input: str
    graph_enabled: bool
    web_enabled: bool
    model: str
    #: 规划要判断「这一轮是不是助理的电脑操控回合」——它决定 prompt 里是否
    #: 宣传桌面工具、以及是否要求模型直接执行而不是派给管家。
    thread_id: str


class DispatchInput(TypedDict, total=False):
    """
    dispatch 节点的状态投影：只读 supervisor 交下来的子任务计划。

    它**不该**看到 `sources` / `messages` / 任何子 Agent 的运行时状态：
    派发是「把计划交给后台执行器」，不需要知道任何执行细节。
    看得越少，将来主图加字段时它越不会被意外牵连。
    """

    user_input: str
    plan_mode: str
    plan_reply: str
    plan_subtasks: list[dict[str, Any]]
    #: 派发时要写进任务记录（子 Agent 的审批 key 与事件落盘都按它归属）
    thread_id: str
    #: 本轮轮次 ID，写进任务记录的 `parent_turn_id` —— 断流终止时按它
    #: 定位「本轮派发的任务」，不会误杀上一轮还在跑的。
    turn_id: str
    web_enabled: bool
    graph_enabled: bool
    model: str
    #: ★ Phase 5：派发时必须把本轮的权限档一起带走。
    #: 投影里缺这一位的症状不是报错，而是「前台只读、后台照改」——
    #: 子任务拿的是它自己那一份默认档，与用户这一轮看到的不是同一档。
    permission_mode: str
    #: 同上，工作区也要跟着进任务记录：前台在用户选的项目里干活、后台却在沙箱里
    #: 找同一个文件，症状是「子任务报没找到，主对话却能打开它」。
    workdir_root: str


class RespondInput(TypedDict, total=False):
    """
    respond 节点的状态投影：只读「这轮该说什么」。

    两种模式都经它：
    - direct：流式给出基于 `sources` 的完整回答
    - delegate：流式给出 supervisor 写的开场白，然后结束

    ⚠️ 它**读不到子任务的进度**（子图在主图之外）——
    所以 delegate 模式下它只能讲 supervisor 交代的那几句话。
    「等子任务跑完再汇总」是一件**做不到也不该做**的事：
    用户在等这一轮结束（不变量 52），子任务还没跑完。
    汇总发生在**下一轮**（见 `supervisor.py` 的 drain 逻辑）。
    """

    user_input: str
    plan_mode: str
    plan_reply: str
    sources: list[dict[str, Any]]
    sources_ready: bool
    messages: list[AnyMessage]
    #: 只用于「后台任务注入块」按会话过滤 —— 它读的是任务状态，不参与回答生成
    thread_id: str
    web_enabled: bool
    graph_enabled: bool
    model: str
    thinking: bool
    #: ★ 主对话**也**要执行工具（supervisor 的 direct 模式里那圈工具循环）。
    #: 权限模式必须进这个投影，否则主对话侧的判定读不到用户那一档 ——
    #: 而主对话恰好是唯一「靠不绑 danger 工具」来代替审批的那条路，
    #: 一旦规则要 ask，它得知道自己该怎么办（见 `supervisor.py` 的注释）。
    permission_mode: str
    #: ★ 与 `permission_mode` 同一类：主对话那圈工具循环在这里构造 `ToolContext`，
    #: 投影里缺这一位 = 工作区对主对话根本不生效，而且不报错
    #: （症状是「说了要改我桌面上的文件，它却说找不到」——它其实在沙箱里翻）。
    workdir_root: str
    #: `UserPromptSubmit` 钩子的补充上下文
    hook_context: str
    #: direct 轮内工具循环的草稿消息（见 `AgentState` 的同名说明）
    turn_messages: list[AnyMessage]
    pending_question: dict[str, Any] | None
    respond_step: int
    turn_text: str
    respond_done: bool


class AskUserInput(TypedDict, total=False):
    """
    `ask_user` 节点的状态投影。

    它只做一件事：把 `pending_question` 交给 `interrupt()`，
    恢复后把回答拼成 ToolMessage 追到 `turn_messages`。
    因此它只需要这两块，不需要对话历史、检索结果、权限档。
    """

    pending_question: dict[str, Any] | None
    turn_messages: list[AnyMessage]


# ★ 下面的投影只有**子图适配层**在用
# （`app/agents/nodes/subtask.py` 把子图状态翻译成它期望的名字，
#   好让 `runner.py` 的实现一行都不用改）。
class PlannerInput(TypedDict, total=False):
    """
    子 Agent（planner）的状态投影。

    与 tool_executor 的边界：planner 负责 LLM 决策，不碰审批留痕
    （approval_log）与执行中的调用（pending_calls / current_call）；
    `tool_calls` 只读 —— 用于步数上限判断，不在这里写入。
    """

    messages: list[AnyMessage]           # 对话历史（裁剪后送 LLM）
    user_input: str                      # 当前轮用户输入（RAG 预检索 query）
    active_agent: str                    # 判断 agent_switch 事件是否需要推送
    agent_chain: list[str]               # Agent 调用链（追加当前节点）
    sources: list[dict[str, Any]]        # RAG 检索来源
    sources_ready: bool                  # 检索是否已执行过（主 Agent 可能已预取）
    tool_calls: list[dict[str, Any]]     # 已执行工具记录（步数上限判断）
    # 本轮选项：web 决定工具绑定、graph 决定检索是否带图路、model 决定用哪个模型
    web_enabled: bool                    # 是否允许联网搜索
    graph_enabled: bool                  # 是否走图路召回
    model: str                           # 本轮模型名（空 = 跟随全局配置）
    #: `UserPromptSubmit` 钩子的补充上下文（空串 = 没有钩子说话）
    hook_context: str


class ToolExecutorInput(TypedDict, total=False):
    """
    tool_executor 的状态投影。

    只读执行所需的队列与留痕上下文 —— 对话历史（messages）、
    意图（intent）、检索来源（sources）等与「执行一个工具」无关，
    不进入本节点的视野；执行结果经 ToolMessage 回到全局 messages。

    ★ 下面三个「身份」字段是**例外**，但必须是例外而不是顺手加的：
    工具需要知道「是谁在调我」才能做对两件事 ——
    会话级状态隔离（shell 的 cwd、后台任务、写前的读校验）与
    越权拦截（`resolve_agent_path` 的授权根）。
    这些信息**绝不能**让模型填：模型填一个别人的 thread_id 就是跨会话越权，
    所以它走 `ToolContext`（见 `tools/context.py`），而 `ToolContext` 只能
    从状态里的这几个字段构造 —— 投影里没有，节点就构造不出，
    两个功能会一起静默失效（曾经就是这样）。
    """

    pending_calls: list[PendingCall]     # 待执行工具队列（消费队头一个）
    active_agent: str                    # 当前子 Agent（权限校验、路由回跳）
    tool_calls: list[dict[str, Any]]     # 全部工具调用记录（追加本次）
    approval_log: list[dict[str, Any]]   # 审批历史（danger 级追加）
    thread_id: str                       # 用户会话（工具侧会话状态的隔离键）
    task_id: str                         # 后台子任务 id（同会话并发时区分彼此）
    employee_id: str                     # 数字员工（工作空间形态下的授权范围）
    #: 协作批次 id。自动放行的留痕要靠它定位（护栏 6），而判定发生在节点里，
    #: 所以它必须进投影 —— 主图路径下拿不到，留空即可（那条路没有批次概念）。
    run_id: str
    #: 本轮生效的权限模式。★ 必须在投影里：本节点是判定的**现场**，
    #: 缺了它子图会一律按默认档判定，
    #: 用户在设置里调的那一档对子任务完全不起作用（`ToolContext` 也从这里取，两处一起失效）。
    permission_mode: str
    #: 派发那一刻冻结的工作区路径（同上：`ToolContext.workspace_root` 从状态里取，
    #: 投影里没有就等于子任务的 shell 与文件工具各回各的根）。
    workdir_root: str
