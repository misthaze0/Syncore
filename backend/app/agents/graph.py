"""
★ LangGraph 主对话编排图（Supervisor 架构，见 ADR 0019）

图结构:
    START
      → supervisor（决定本轮怎么走：直接答，还是派发后台任务）
           ├── direct ────► respond（流式给出本轮能给的答案）→ END
           └── delegate ──► dispatch（派发子任务到后台）→ respond（开场白）→ END
      ★ 主图到此结束 —— 子图不在主图里 ★

    子任务在 `app/tasks/executor.py` 里跑，生命周期与本图**无关**：
    用户关掉页面、刷新、甚至重启服务，它都继续跑完。

为什么是这个形状:
- `direct` 是必需的快路径。单子任务场景下「派发 + 后台 + 轮询」是
  纯粹的体验倒退（5.3s 流式出答案 → 0.5s 弹「已派发」+ 3s 后蹦结果），
  而且违背不变量 52。见 `supervisor.py` 的模块注释。
- 主图**不含任何子 Agent 节点**：子 Agent 是独立子图（`app/agents/subgraph.py`），
  有自己的私有 scratchpad。共享 `AgentState` 时代「email_agent 能读到
  calendar_agent 的审批留痕」这类隐式耦合没人能拦（ADR 0014:21-23），
  放到并行子任务上它会变成真的 bug。

驱动方式:
`get_agent_event_stream()` 同时做两件事：
1. 后台任务驱动 `graph.astream`，节点通过 `emit` 把 SSE 事件写入队列
2. 前台从队列取出事件 yield 给 chat 接口

遇到 interrupt 时，从检查点中取出 interrupt payload 推给前端，再按 payload 的
`kind` 分成两条路 —— 这是「提问不是审批」的结构性判别：

    审批（没有 `kind`）→ `approval_flow.py` 等 `/api/approval`
    提问（`kind=question`）→ `questions/flow.py` 等 `/api/questions/answer`

收到决策后都以 `Command(resume=...)` 恢复执行。
★ 两条路的留痕与推送都在各自的 flow 模块里（不变量 36 的唯一写入点），
主图与子图执行器都**调用**它们，都不自己写留痕。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncGenerator

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agents.approval_broker import approval_broker
from app.agents.dialogue import AssistantMessageStream, split_assistant_messages
from app.agents.approval_flow import (
    backfill_execution_result,
    read_decision,
    run_pending_approval,
)
from app.agents.emitter import get_emitter
from app.agents.state import (
    TURN_MSG_PREFIX,
    AgentState,
    AskUserInput,
    DispatchInput,
    RespondInput,
    SupervisorInput,
    initial_state,
    turn_message_id,
)
from app.agents.supervisor import dispatch_node, respond_node, supervisor_node
from app.agents.think import split_think
from app.agents.tools.registry import tool_registry
from app.attachments import attachment_metas as attachment_meta_list
from app.config import settings
from app.conversations import STATUS_ABORTED, STATUS_ERROR, STATUS_OK, conversation_store
from app.conversations.files import collect_written_files
from app.conversations.store import is_assistant_thread_id
from app.core.redis_client import close_redis, create_checkpointer
from app.hooks import PROMPT_SUBMIT, STOP, build_payload, fire
from app.observability import begin_trace, set_current_recorder
from app.questions.flow import read_question_answer, run_pending_question
from app.questions.models import QuestionOutcome, is_question_payload

logger = logging.getLogger(__name__)

# 递归上限：direct 模式一次工具调用最多消耗 2 步（respond + tool_executor），留足余量
RECURSION_LIMIT = 2 * settings.AGENT_MAX_TOOL_STEPS + 10

#: 恢复会话上下文时，把历史消息按哪个节点写入 checkpoint。
#:
#: 选 `supervisor` 而不是某个子 Agent：历史轮次是「用户与助手的往来」，
#: 不是某个专员的内部产出 —— 记在入口节点名下语义最准。
#: 它只影响 checkpoint 的 `versions_seen`，不影响本次执行：
#: 随后的新输入会让图从 START 重新开始（已用桩图验证）。
SEED_AS_NODE = "supervisor"

# 队列结束哨兵
_SENTINEL = object()

#: ★ 子图命名空间前缀 —— 主对话入口必须拒绝带它的 thread_id（不变量 58）。
#: 一个子任务的消息流混进用户会话历史，会让 `_trim_history` 的 20 条窗口
#: 被后台任务挤满，也会让 `seed_thread_from_history` 播回用户从没说过的话。
from app.agents.subgraph import is_subtask_thread_id

# 员工私聊命名空间前缀（工作空间模块）—— 主对话入口同样要拒绝它。
from app.workspace import is_employee_thread_id

# ★ Per-Agent State Projection —— 节点名 → 输入投影
#
# 每个节点运行时只收到投影内字段的视图，而不是全局 AgentState。
# 为什么这样做（而不是共享一个大字典）：
# - 节点依赖显式化：某个节点依赖/污染哪些状态，看投影定义即可，不必读实现。
#   全局共享状态下，“dispatch 节点能看到子 Agent 的审批留痕”这类
#   隐式耦合永远不会有人发现，直到它变成 bug。
# - 与 LangGraph 社区生产实践对齐（multi-agent 状态隔离 /
#   private state 模式）：图状态保持单一持久化容器（Checkpointer 语义不变），
#   但节点的**视野**收窄到自己需要的切片。
# - 投影只影响节点读侧；条件路由函数读的是合并后的全局状态，不受影响。
#
# 维护规则：给节点新增状态读取时，必须同步扩投影字段，
# 否则 test_agents.py 的投影用例会失败 —— 这是刻意的护栏。
NODE_INPUT_SCHEMAS: dict[str, type] = {
    "supervisor": SupervisorInput,
    "dispatch": DispatchInput,
    "respond": RespondInput,
    "ask_user": AskUserInput,
}

#: 主图的节点名（顺序即声明序）
MAIN_NODES = ["supervisor", "dispatch", "respond", "ask_user"]

# ---------- 全局单例 ----------
_graph: Any = None
_checkpointer_cleanup: Any = None


async def ask_user_node(state: AskUserInput, config: RunnableConfig) -> dict[str, Any]:
    """
    模型主动提问的挂起点（主图里唯一会 `interrupt()` 的节点）。

    ★ `interrupt()` 之前只能有纯读取
    恢复时本节点会被**从头重放**：任何写文件、写留痕、发网络请求都会做两遍。
    留痕、推送与等待都在 `questions/flow.py`，由驱动层在挂起期间调用一次；
    这里只负责「把载荷交给 LangGraph」和「把回答变成 ToolMessage」。

    ★ `respond` 与它的分工
    `respond` 已经产出 `pending_question`（归一化后的问题），并把带
    `tool_calls` 的 AIMessage 留在 `turn_messages` 里；本节点恢复后只需补上
    对应的 ToolMessage，路由就会回到 `respond` 继续那一步 LLM。
    """
    from app.questions.models import (
        format_question_result,
        normalize_question_resume,
    )

    emit = get_emitter(config)
    pending = state.get("pending_question") or {}
    questions = list(pending.get("questions") or [])
    call_id = str(pending.get("call_id") or "")
    if not questions or not call_id:
        # 没有待问内容却走到这里：直接回 respond，不挂一个空壳
        return {"pending_question": None}

    resumed = interrupt(pending)
    outcome = normalize_question_resume(resumed)
    result_text = format_question_result(questions, outcome.status, outcome.answers)

    emit({
        "type": "tool_result",
        "call_id": call_id,
        "tool_name": "ask_user",
        "tool_label": tool_registry.display_name_for("ask_user"),
        "result": result_text,
        "success": True,
        "duration_ms": 0,
    })

    turn_messages = list(state.get("turn_messages") or [])
    turn_messages.append(ToolMessage(content=result_text, tool_call_id=call_id))
    return {
        "turn_messages": turn_messages,
        "pending_question": None,
    }


# ---------- 图构建 ----------
def build_graph(checkpointer: Any = None):
    """
    构建并编译主图（Supervisor 架构）。

    ★ 三个节点，各司其职
    - `supervisor`：决定 direct 还是 delegate（direct 模式下顺带预取检索）
    - `dispatch`：把子任务交给后台执行器，**立即返回**
    - `respond`：流式给出本轮能给的答案，然后 END

    ★ 为什么子 Agent 节点一个都没有
    子 Agent 是独立子图（`app/agents/subgraph.py`），有自己的私有 scratchpad。
    把它们塞进主图就得共享 `AgentState`，
    而并行子任务共享状态会真的踩到彼此（ADR 0014:21-23 记着这个代价）。
    """
    builder = StateGraph(AgentState)

    # ---------- 节点（每个节点挂上自己的输入投影，见 NODE_INPUT_SCHEMAS） ----------
    builder.add_node("supervisor", supervisor_node, input_schema=NODE_INPUT_SCHEMAS["supervisor"])
    builder.add_node("dispatch", dispatch_node, input_schema=NODE_INPUT_SCHEMAS["dispatch"])
    builder.add_node("respond", respond_node, input_schema=NODE_INPUT_SCHEMAS["respond"])
    builder.add_node("ask_user", ask_user_node, input_schema=NODE_INPUT_SCHEMAS["ask_user"])

    # ---------- 边 ----------
    builder.add_edge(START, "supervisor")

    def route_after_plan(state: Any) -> str:
        """
        direct → respond（直接答）
        delegate → dispatch（派发子任务），**再** respond（给开场白）

        ⚠️ delegate 也要走 respond，因为用户这一轮需要一个收尾：
        「我派了两个任务，一个查资料，一个写文档」这句话必须说出来，
        否则界面只显示两个进度条，用户不知道发生了什么。
        注意这条路的 respond 只说开场白，**不等子任务**（不变量 52）。
        """
        mode = str(state.get("plan_mode") or "direct")
        if mode == "delegate" and state.get("plan_subtasks"):
            return "dispatch"
        return "respond"

    builder.add_conditional_edges(
        "supervisor", route_after_plan, {"dispatch": "dispatch", "respond": "respond"}
    )
    # dispatch 之后一定回 respond（给开场白）；direct 模式下 dispatch 不会被走到
    builder.add_edge("dispatch", "respond")

    def route_after_respond(state: Any) -> str:
        """
        respond 每一步的出口：

        - 有待问的提问 → `ask_user`（挂起；恢复后回到 respond 继续这一步）
        - 已经产出最终回答 → END
        - 还有工具结果要交给模型 → 回到 respond 再走一步

        ★ 用状态字段判断，而不是节点返回值
        条件边读到的是合并后的全局状态，`respond` 的返回值已经写进去了；
        这样「挂起后恢复」与「普通重入」走的是同一条路由。
        """
        if state.get("pending_question"):
            return "ask_user"
        if state.get("respond_done"):
            return "end"
        return "respond"

    builder.add_conditional_edges(
        "respond",
        route_after_respond,
        {"ask_user": "ask_user", "respond": "respond", "end": END},
    )
    # 回答已经变成 ToolMessage 回到 turn_messages，接着让模型继续这一步
    builder.add_edge("ask_user", "respond")

    return builder.compile(checkpointer=checkpointer)


async def init_graph(force_rebuild: bool = False):
    """初始化全局图实例（应用启动时调用）"""
    global _graph, _checkpointer_cleanup

    if _graph is not None and not force_rebuild:
        return _graph

    checkpointer, cleanup = await create_checkpointer()
    _checkpointer_cleanup = cleanup
    _graph = build_graph(checkpointer)

    logger.info(
        "LangGraph 编译完成 | 节点=%s | 子图=独立编译 | 注册工具=%s",
        MAIN_NODES,
        tool_registry.stats()["total"],
    )
    return _graph


def get_graph():
    """获取全局图实例（未初始化时返回 None）"""
    return _graph


async def shutdown_graph() -> None:
    """
    释放图相关资源。

    ★ 顺序很重要：先排空后台子任务，再释放 Checkpointer

    反过来的话，正在跑的子任务会突然发现 checkpoint 没了 ——
    它要是正好挂在 `interrupt()` 上，就再也醒不过来，
    而用户看到的仍然是一个「在等审批」的任务。
    """
    global _graph, _checkpointer_cleanup

    approval_broker.cancel_all()
    # 提问的等待者也要一起放掉：留着会让「关停时正等着回答」的流挂死，
    # 而它们与审批是两套 broker，漏一个不会有任何报错。
    try:
        from app.questions.broker import question_broker

        question_broker.cancel_all()
    except Exception as e:  # noqa: BLE001
        logger.debug("提问等待者清理失败（已忽略）: %s", e)

    # ---------- 先排空后台子任务 ----------
    try:
        from app.tasks.executor import executor

        cancelled = await executor.shutdown()
        if cancelled:
            logger.info("已取消 %s 个后台子任务", cancelled)
    except Exception as e:  # noqa: BLE001
        logger.warning("排空后台子任务失败（继续释放图资源）: %s", e)

    # ---------- 再重置审批闸门 ----------
    # 闸门是进程内队列，与子任务一并清掉 ——
    # 留着会让重启后第一个进闸门的任务等一个永远不来的前一个。
    try:
        from app.agents.approval_gate import approval_gate

        await approval_gate.reset()
    except Exception as e:  # noqa: BLE001
        logger.debug("审批闸门重置失败（已忽略）: %s", e)

    try:
        from app.questions.flow import question_gate

        await question_gate.reset()
    except Exception as e:  # noqa: BLE001
        logger.debug("提问闸门重置失败（已忽略）: %s", e)

    # ---------- 进度事件总线的订阅也清掉 ----------
    # 它们属于上一轮的 SSE 连接（已经随关停断开），留着会白推给没人读的队列。
    try:
        from app.tasks.bus import task_bus

        task_bus.reset()
    except Exception as e:  # noqa: BLE001
        logger.debug("进度事件总线重置失败（已忽略）: %s", e)

    if _checkpointer_cleanup is not None:
        try:
            await _checkpointer_cleanup.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        _checkpointer_cleanup = None

    await close_redis()
    _graph = None


# ---------- 事件流驱动 ----------
async def _drive_graph(graph: Any, graph_input: Any, config: dict, queue: asyncio.Queue) -> None:
    """后台任务：驱动图的执行，节点事件经 emit 进入队列"""
    try:
        async for _ in graph.astream(graph_input, config, stream_mode="updates"):
            pass
    except Exception as e:  # noqa: BLE001
        logger.error("图执行异常: %s", e, exc_info=True)
        queue.put_nowait({"type": "error", "content": f"执行失败: {e}"})
    finally:
        queue.put_nowait(_SENTINEL)


def _abort_turn_subtasks(thread_id: str, turn_id: str) -> None:
    """
    掐掉**本轮派发**的、还没结束的子任务（断流终止用）。

    ★ 为什么只按 parent_turn_id 收口
    子任务在 executor 里是独立协程，`runner.cancel()` 拦不住它们 ——
    用户点停止后若不清，任务继续烧 token、继续产生副作用。
    但「停止」只该停这一轮引发的东西：上一轮派发、还在跑的长任务
    （写报告、批量整理）不归这一次停止管，误杀会让用户丢进度。

    ★ 为什么「先落状态、再断协程」
    与 `api/tasks.py::cancel_task` 同一顺序：协程的 CancelledError 处理里
    还要再 patch 一次状态，先掐协程会撞 IllegalTransition。
    """
    if not thread_id or not turn_id:
        return
    try:
        from app.tasks.executor import executor
        from app.tasks.models import is_active
        from app.tasks.store import task_store

        # 关停期间不在这里收口：执行器的取消分支会落 `interrupted`（可续）。
        # 这里走 `cancelled` 会把「重启后本可继续的任务」判了死刑。
        if executor.shutting_down:
            return

        records, _ = task_store.list(thread_id=thread_id)
        for record in records:
            if str(record.get("parent_turn_id") or "") != turn_id:
                continue
            if not is_active(str(record.get("status") or "")):
                continue
            task_id = str(record.get("task_id") or "")
            if not task_id:
                continue
            task_store.cancel(task_id)
            aborted = executor.abort(task_id)
            logger.info("断流终止本轮子任务: task=%s 协程已掐=%s", task_id, aborted)
    except Exception as e:  # noqa: BLE001
        # 终止是停止路径的增益：存储不可用 / 并发改状态时不该再抛一层，
        # 否则会盖掉正在传播的 CancelledError
        logger.warning("断流终止子任务失败（已忽略）: %s", e)


def _collect_interrupts(snapshot: Any) -> list[Any]:
    """从状态快照中提取所有 interrupt"""
    interrupts: list[Any] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts.extend(getattr(task, "interrupts", ()) or ())
    return interrupts


def _accumulate_usage(total: dict[str, int], usage: dict[str, Any]) -> None:
    """
    把**一次** LLM 调用的用量并进本轮累计值。

    一轮对话里 LLM 可能被调用多次（多 Agent 接力、工具循环每步一次），
    所以用量只能累加 —— 用最后一次会严重低估。
    只累加已知的整数字段，网关多给别的字段也不影响。
    """
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + int(value)


def _done_event(usage: dict[str, int]) -> dict[str, Any]:
    """
    构造 `done` 事件。

    `usage` 是**已有事件上的一个可选字段**，不是新事件类型
    （与 `done.files` 同一约定）：拿不到用量时就不带它，
    前端据「有没有 usage」决定那行小字要不要出现。

    ⚠️ `reasoning` / `answer` 不在这里加 —— 它们需要**整轮**的完整答案才能切分，
    而本函数在内层流里被调用（那时还没有）。由外层 `get_agent_event_stream`
    在 `done` 上补齐，见那里的注释。
    """
    event: dict[str, Any] = {"type": "done", "content": ""}
    if usage:
        event["usage"] = dict(usage)
    return event


def _reject_subtask_thread(thread_id: str) -> dict[str, Any] | None:
    """
    拒绝子图/员工命名空间的 thread_id（不变量 58 的第二道防线）。

    ★ 为什么前缀不够，还要这个检查
    前缀 `subtask_` / `emp_` 让「用户会话」与「子任务」「员工私聊」**天然不相撞**，
    但用户仍然可以主动 POST 一个 `thread_id=subtask_xxx` 或 `emp_xxx` ——
    于是他自己的对话被写进了某个子任务/员工的 checkpoint，
    那个消息流里混进了用户从没说过的话。
    两者都做才闭合：前缀挡住「意外相撞」，检查挡住「故意撞入」。

    返回一个 `done` 事件供调用方直接返回（这样调用方的结构最简单），
    返回 None 表示放行。

    ★ 助理命名空间（`assistant_`）在这里**放行**
    助理对话走的就是主链路（同一个图、同一套工具），只是会话归属不同 ——
    隔离发生在列表/搜索接口（`api/conversations.py` 的 scope 过滤），
    不在执行入口。
    """
    if is_subtask_thread_id(thread_id) or is_employee_thread_id(thread_id):
        logger.warning("拒绝子图/员工命名空间的 thread_id: %r", thread_id)
        return {
            "type": "error",
            "content": "会话标识无效",
        }
    return None


async def get_agent_event_stream(
    user_input: str,
    thread_id: str,
    graph: Any = None,
    attachments: list[dict[str, Any]] | None = None,
    *,
    web: bool = True,
    use_graph: bool = True,
    model: str | None = None,
    thinking: bool | None = None,
    permission_mode: str | None = None,
    workdir_root: str = "",
) -> AsyncGenerator[dict[str, Any], None]:
    """
    全量 Agent 事件流生成器（对外唯一入口）

    产出的事件类型:
        intent / agent_switch / sources / tool_call / interrupt /
        tool_result / token / state_update / memory / approval_auto /
        hook_notice / done / error

    `attachments` 是本轮对话附件（含正文，由 chat 接口从临时存储解析出来）：
    正文进本轮 HumanMessage（`state.initial_state`），元数据随轮次落盘 ——
    重放时前端能看到「这条消息带了哪些附件」。

    其中 `done` 可能带一个 `files` 字段（本轮产出的文件），由本轮事件流派生。
    它是**已有事件上的字段**，不是新增事件类型；派生函数只有一份
    （`conversations/files.py`），落盘时用的是同一个。

    ★ 为什么这一层薄包装是有价值的

    它把「这一轮到底发生了什么」记成一份**会话历史**（Event 流的快照 + 最终回答）。
    放在这里而不是散落到各个节点，理由与 Phase 4 的追踪埋点完全一致：
    事件流是这些节点对外唯一的输出通道，**包一层就拿到了整轮的全部信号**
    （意图、路由、工具调用、审批、检索来源、错误），且完全不必改任何一个节点。
    唯一比追踪更强的一点是它还能看到**收尾方式** —— 正常 / 断连 / 报错
    分别对应 `ok` / `aborted` / `error`，而收尾方式只有在生成器的外层才看得见
    （内层 try/except 会把断连与报错都吃掉，各自 yield 一个 error 就结束了）。

    ⚠️ 落盘在 `finally` 里，且**同步**写。异步生成器被关闭
    （客户端断连 / 用户按停止 / 进程收到信号）时，`finally` 里绝不能 `await` ——
    那会让生成器忽略 `GeneratorExit`，从而把「一次断连」升级成
    「一个永不结束的清理协程」。文件追加是几 KB 的同步 IO，代价可以接受。

    `permission_mode` 是**本轮请求**的权限档（Phase 5）。
    ⚠️ 它只能比全局默认档更**紧**，不能更松（`policy.effective_mode`）。
    放宽必须由用户在设置里做 —— 否则「邮件正文里写着一句请用完全访问模式」
    就成了提权入口，而界面上一条记录都看不到。
    """
    # ★ 不变量 58：主对话入口拒绝子图命名空间
    #
    # 在最外层拦，且**不落盘** —— 这不是一次对话，
    # 只是一次非法请求，记进会话历史反而会凭空多出一个空壳会话。
    rejected = _reject_subtask_thread(thread_id)
    if rejected is not None:
        yield rejected
        yield _done_event({})
        return

    turn_events: list[dict[str, Any]] = []
    answer_parts: list[str] = []
    # `reasoning` 事件攒出来的思考正文（reasoning_content 通道，见 _stream_agent_events）
    reasoning_acc: list[str] = []
    status = STATUS_OK
    error = ""
    # 本轮真实用量：从内层 `done` 事件上取，随轮次落盘 ——
    # 这样「重新打开历史会话」也能看到用量，而不是只有当场那一屏才有。
    turn_usage: dict[str, int] = {}
    # 轮次留痕只存元数据（id + 文件名），正文不落盘 —— 会话文件不该膨胀几十 KB
    attachment_metas = attachment_meta_list(attachments)
    message_stream = AssistantMessageStream() if is_assistant_thread_id(thread_id) else None
    # 本轮的 turn_id 在流开始时由内层生成（用户消息的 id 要从它派生），
    # 通过这个可变容器带回落盘点 —— 两处必须是同一个身份，删轮次才对得上。
    turn_meta: dict[str, Any] = {}

    try:
        async for event in _stream_agent_events(
            user_input,
            thread_id,
            graph,
            attachments,
            web=web,
            use_graph=use_graph,
            model=model,
            thinking=thinking,
            permission_mode=permission_mode,
            workdir_root=workdir_root,
            turn_meta=turn_meta,
        ):
            if event.get("type") == "token":
                answer_parts.append(str(event.get("content", "")))
            elif event.get("type") == "reasoning":
                reasoning_acc.append(str(event.get("content", "")))
            else:
                # ---------- 交付物清单 ----------
                # 在 `done` 上补一个 `files` 字段（**不是**新事件类型）：
                # 后端此刻已经看全了这一轮的全部事件，能准确说出这一轮写了哪些文件；
                # 前端因此不必去猜——它在 `done` 之后要立刻渲染「本次产出 N 个文件」。
                # 派生逻辑只有一份（`conversations/files.py`），
                # 落盘时 store 用的是同一个函数，于是「实时看到」与「重放看到」必然一致。
                if event.get("type") == "done":
                    written = collect_written_files(turn_events, workdir_root)
                    if written:
                        event = {**event, "files": written}
                    if event.get("usage"):
                        turn_usage = event["usage"]
                    # ---------- 推理正文 ----------
                    # ★ 切分由**后端**在此刻权威地做一次，随 `done` 下发，前端只管渲染。
                    # 理由是「什么算推理」全项目只能有一处定义 —— 让前端各自去猜边界，
                    # 实时那一屏与重放那一屏迟早会不一致。
                    # 取的就是同一个纯函数，落盘时用的也是它（见 finally）。
                    #
                    # 顺带把**干净回答**也带上：流式那一份是混着推理的，
                    # 而「从哪个字符起才算回答」是后端的判断。带一个 `answer`
                    # 比让前端再实现一遍切分规则便宜得多（只在真有推理时才带）。
                    reasoning, clean_answer = split_think("".join(answer_parts))
                    if message_stream is not None and answer_parts:
                        reply_messages = split_assistant_messages(clean_answer)
                        snapshot = {"type": "assistant_messages", "content": reply_messages}
                        turn_events.append(snapshot)
                        yield snapshot
                        clean_answer = "\n\n".join(reply_messages)
                        # 渠道、朗读等纯文本消费者也拿到没有协议标记的完整回答。
                        event = {**event, "answer": clean_answer}
                    if not reasoning and reasoning_acc:
                        # reasoning_content 通道的思考（enable_thinking 走独立字段，
                        # 不混在 content 里）：流式期间已实时推给前端，这里把
                        # 完整正文挂到 done 上 —— 折叠块与重放共用这一份
                        reasoning = "".join(reasoning_acc).strip()
                    if reasoning:
                        event = {**event, "reasoning": reasoning}
                        if clean_answer:
                            event = {**event, "answer": clean_answer}
                turn_events.append(event)
                if event.get("type") == "error":
                    status = STATUS_ERROR
                    error = str(event.get("content", ""))
            if event.get("type") == "token" and message_stream is not None:
                for message_event in message_stream.feed(str(event.get("content", ""))):
                    if message_event["type"] != "token":
                        turn_events.append(message_event)
                    yield message_event
            else:
                yield event
    except asyncio.CancelledError:
        # 客户端断连 / 主动停止。半截回答也要留痕 ——
        # 用户按了停止，不等于「这一轮不存在」（界面上的半截回答还在）。
        status = STATUS_ABORTED
        raise
    except GeneratorExit:
        status = STATUS_ABORTED
        raise
    except Exception as e:  # noqa: BLE001
        # 实现层把大部分异常都转成了 error 事件；真漏到这里的是它 try 之外
        # 的失败（例如编排驱动本身崩了）。这里只记账后原样抛出 ——
        # 不改变已有行为（用户该看到的报错还是那个报错）。
        status = STATUS_ERROR
        error = str(e)
        raise
    finally:
        # 落盘的是**摘掉推理之后**的回答：会话记录要的是「答了什么」，
        # 而不是模型怎么想的。同一段推理另存 `reasoning`，回看时折叠展示（不丢数据）。
        # 顺带避免它污染下游 —— 检索摘要、上下文播种、长期记忆抽取读的都是 `answer`。
        reasoning, clean_answer = split_think("".join(answer_parts))
        if message_stream is not None and answer_parts:
            reply_messages = split_assistant_messages(clean_answer)
            clean_answer = "\n\n".join(reply_messages)
            # finally 不能 yield；中断时仍保存已经生成的最后半条消息及消息边界。
            snapshot = {"type": "assistant_messages", "content": reply_messages}
            if not turn_events or turn_events[-1] != snapshot:
                turn_events.append(snapshot)
        if not reasoning and reasoning_acc:
            reasoning = "".join(reasoning_acc).strip()
        summary = _persist_turn(
            thread_id=thread_id,
            turn_id=str(turn_meta.get("turn_id") or ""),
            user_input=user_input,
            answer=clean_answer,
            events=turn_events,
            status=status,
            error=error,
            attachments=attachment_metas,
            usage=turn_usage,
            reasoning=reasoning,
            workdir_root=workdir_root,
        )
        # 首轮之后用模型换个更短的标题（见 `app/conversations/titles.py`）。
        # 放在落盘**之后**：标题要覆盖的是已经存在的那个派生标题，
        # 而派生标题正是刚才这次落盘写进去的。
        _maybe_schedule_auto_title(
            thread_id=thread_id,
            user_input=user_input,
            answer=clean_answer,
            summary=summary,
        )


def _persist_turn(
    *,
    thread_id: str,
    turn_id: str = "",
    user_input: str,
    answer: str,
    events: list[dict[str, Any]],
    status: str,
    error: str,
    attachments: list[dict[str, str]] | None = None,
    usage: dict[str, int] | None = None,
    reasoning: str = "",
    workdir_root: str = "",
) -> Any:
    """
    把一轮对话写进会话历史，返回更新后的索引项（没写进去时返回 None）。

    旁路能力：任何失败都只告警。会话历史挂掉的正确表现是
    「这次没记上」，而不是「因为记不了账，所以答不了问题」。
    返回值只被自动标题用一次（它要判断「这是不是首轮」），
    所以失败时返回 None 就够了 —— 没有标题也不需要理由。

    `workdir_root` 只用来把产物路径换算成沙箱相对形式（见 `files.collect_written_files`）。
    不传的话，自动工作区里写出来的文件会被记成沙箱根下的同名路径，
    重开历史时一律显示「已不在了」。
    """
    try:
        return conversation_store.append_turn(
            thread_id,
            turn_id=turn_id,
            user_input=user_input,
            answer=answer,
            events=events,
            status=status,
            error=error,
            attachments=attachments,
            usage=usage,
            reasoning=reasoning,
            files=collect_written_files(events, workdir_root),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("会话历史写入失败（不影响本轮问答）: %s", e)
        return None


#: 后台标题任务的强引用。`asyncio` 只对 task 持**弱**引用，
#: 不自己收着的话，它可能在跑完之前就被 GC 掉（官方文档明确要求保存引用）。
_title_tasks: set[asyncio.Task] = set()


def _maybe_schedule_auto_title(
    *, thread_id: str, user_input: str, answer: str, summary: Any
) -> None:
    """
    首轮落盘之后，另起一个任务用模型生成更短的标题。

    ★ 为什么只做首轮

    标题的作用是「让用户认出这个会话」，而主题是第一轮就定下来的；
    每轮都重新起标题，只会让用户刚记住的名字又变掉。

    ★ 为什么在这里就得把事件循环拿到手

    调用点在 `finally` 里，那条路径既可能是正常收尾，也可能是生成器被
    `aclose()`（客户端断连）。后者未必在运行中的事件循环里 ——
    `get_running_loop()` 拿不到就**安静放弃**：一个标题不值得冒任何风险。
    """
    if not bool(getattr(settings, "CONVERSATION_AUTO_TITLE", True)):
        return
    if summary is None or getattr(summary, "turns", 0) != 1:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(
        _generate_and_apply_title(thread_id, user_input, answer, summary.title)
    )
    _title_tasks.add(task)
    task.add_done_callback(_title_tasks.discard)


async def _generate_and_apply_title(
    thread_id: str, user_input: str, answer: str, expected_title: str
) -> None:
    """
    生成并应用标题。整条链路只写日志，不抛异常。

    `expected_title` 是起标题时看到的那个标题：如果在这几秒里用户自己改了名，
    写入会被 store 拒绝（比对在锁内进行）—— 模型不该覆盖用户的选择。
    """
    from app.conversations.titles import suggest_title

    try:
        title = await suggest_title(user_input, answer)
        if not title:
            return
        updated = conversation_store.apply_auto_title(thread_id, title, expected=expected_title)
        if updated is not None:
            logger.info("会话标题已自动生成: %s → %s", thread_id, title)
    except Exception as e:  # noqa: BLE001
        logger.info("自动标题应用失败（保持原标题）: %s", e)


async def seed_thread_from_history(graph: Any, thread_id: str) -> dict[str, Any]:
    """
    把落盘的会话历史**播种**回 Checkpointer（仅当它已经丢了这份上下文）。

    解决的问题：会话历史存在磁盘（进程/Redis 重启都不丢），而模型上下文存在
    Checkpointer（Redis 一旦重启或降级成 InMemorySaver 就没了）。两者不一致时，
    界面显示着 10 轮历史、模型却完全不记得 —— 用户会认为「历史功能是假的」。

    种子只播最近若干轮（`ConversationStore.seed_messages` 里有上限），
    目的是「记得刚才聊了什么」，而它在语义上随轮次距离衰减；
    把几十轮全塞回去只会挤掉真正相关的检索上下文（同不变量 27 的取舍）。

    返回一份可展示的结果（`seeded` / `reason`），让「到底恢复了没有」
    成为一个可查询的事实，而不是靠猜 —— 与 `/memory/prompt-block` 的思路一致。
    """
    result: dict[str, Any] = {
        "thread_id": thread_id,
        "seeded": False,
        "turns": 0,
        "messages": 0,
        "reason": "",
    }
    if graph is None:
        result["reason"] = "多 Agent 图未初始化"
        return result
    if not bool(getattr(settings, "CONVERSATION_REHYDRATE", True)):
        result["reason"] = "已关闭（CONVERSATION_REHYDRATE=false）"
        return result
    if not hasattr(graph, "aupdate_state"):
        result["reason"] = "当前图实例不支持写入状态"
        return result

    try:
        pairs = conversation_store.seed_messages(thread_id)
        if not pairs:
            result["reason"] = "本地没有该会话的历史（新会话无需恢复）"
            return result

        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await graph.aget_state(config)
        existing = (getattr(snapshot, "values", None) or {}).get("messages") or []
        if existing:
            # 上下文还在（同一个 Redis、同一进程运行期内）—— 不动它，
            # 否则会把 Checkpointer 里更完整的上下文（含工具消息）换成一份降级的文本版。
            result["messages"] = len(existing)
            result["reason"] = "上下文仍在线，无需恢复"
            return result

        history: list[Any] = []
        for user_text, answer, turn_id in pairs:
            # ★ 播种也要补回轮次锚点：删除某一轮靠 `turn:<turn_id>` 把上下文分段。
            # 不补的话，一次 Redis 重启就让所有轮次变成「没有身份证」的旧消息 ——
            # 之后删哪一轮都只能整段重建。
            history.append(
                HumanMessage(
                    content=user_text,
                    id=turn_message_id(turn_id) if turn_id else None,
                )
            )
            history.append(AIMessage(content=answer))
        await graph.aupdate_state(config, {"messages": history}, as_node=SEED_AS_NODE)
        # 重新播种 = 消息集合变了：旧摘要的锚点即便碰巧还能对上，
        # 它描述的也是另一份上下文。宁可重摘要一次。
        from app.agents.context import invalidate_summary

        invalidate_summary(thread_id)

        result.update(
            seeded=True,
            turns=len(pairs),
            messages=len(history),
            reason=f"已从会话历史恢复 {len(pairs)} 轮上下文",
        )
        logger.info("会话上下文已恢复: thread=%s turns=%s", thread_id, len(pairs))
    except Exception as e:  # noqa: BLE001
        # 恢复失败就按新会话继续 —— 少几轮上下文，比直接报错好
        logger.warning("会话上下文恢复失败（按新会话继续）: %s", e)
        result["reason"] = f"恢复失败: {e}"
    return result


async def discard_thread_context(graph: Any, thread_id: str) -> dict[str, Any]:
    """
    把某个 thread 的模型上下文整个丢掉（会话已经被删空时用）。

    与 `seed_thread_from_history` 是一对：那个负责「文件有、内存没有」时补回来，
    这个负责「文件已没有、内存还留着」时清干净。
    """
    result: dict[str, Any] = {"discarded": False, "reason": ""}
    checkpointer = getattr(graph, "checkpointer", None) if graph is not None else None
    if checkpointer is None or not hasattr(checkpointer, "adelete_thread"):
        result["reason"] = "当前上下文不支持按会话删除"
        return result
    try:
        await checkpointer.adelete_thread(thread_id)
        # 上下文没了，由它派生的摘要与落盘的工具输出也没有留下的理由
        # （两者都含用户原文，属于「删了就该删干净」的范围）。
        from app.agents.context import purge_thread_summaries, purge_thread_tool_results

        purge_thread_summaries(thread_id)
        purge_thread_tool_results(thread_id)
        result["discarded"] = True
    except Exception as e:  # noqa: BLE001
        logger.warning("丢弃会话上下文失败: thread=%s (%s)", thread_id, e)
        result["reason"] = f"丢弃上下文失败: {e}"
    return result


def _legacy_turn_in_context(messages: list[Any], user_input: str) -> bool:
    """旧轮次没有锚点可查时，用用户原话判断它是否真的还在上下文里"""
    text = (user_input or "").strip()
    if not text:
        return False
    for message in messages:
        if not isinstance(message, HumanMessage):
            continue
        content = message.content
        if isinstance(content, str) and content.lstrip().startswith(text):
            return True
    return False


async def _rebuild_context_after_delete(
    graph: Any, thread_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """按**剩余**轮次文件重建上下文（旧数据兜底，见 `remove_turn_messages_from_context`）"""
    discarded = await discard_thread_context(graph, thread_id)
    if not discarded.get("discarded"):
        result["reason"] = str(discarded.get("reason") or "重建上下文失败")
        return result
    seeded = await seed_thread_from_history(graph, thread_id)
    result["reseeded"] = bool(seeded.get("seeded"))
    result["reason"] = f"已按剩余轮次重建上下文（{seeded.get('reason') or '无历史'}）"
    logger.info("删除轮次后按剩余历史重建上下文: thread=%s", thread_id)
    return result


async def remove_turn_messages_from_context(
    graph: Any, thread_id: str, *, turn_id: str, user_input: str = ""
) -> dict[str, Any]:
    """
    从 Checkpointer 里**精准摘掉**某一轮的消息。

    删的是「这一轮的一切」：用户消息、回答，以及该轮工具调用留下的
    ToolMessage —— 它们都属于那一轮，留下任何一条都等于「删了却还记得」。
    摘除用 `RemoveMessage`（`add_messages` 减速器支持按 id 删除），
    其余轮次与工具上下文一个字节都不动。

    ★ 为什么是「切一段」而不是逐条记 id
    落盘时只知道这一轮的起点（`turn:<turn_id>`），而这一轮里模型还会
    产出工具消息与多段回答。相邻两个轮次起点之间的全部消息，就是
    这一轮在上下文里的完整痕迹，不必再维护一份 id 清单。

    ★ 找不到起点时的退路
    升级前产生的轮次、以及上下文被重新播种过的线程都没有这些 id。
    这时按**剩余轮次文件**重建上下文 —— 文件里已经没有这一轮，
    重建后它必然不在。代价是退回到「只记得最近 20 轮的问答文本」，
    但「删了就是删了」优先于上下文丰富度。
    """
    result: dict[str, Any] = {"removed": 0, "reseeded": False, "reason": ""}
    if graph is None or not thread_id or not turn_id:
        result["reason"] = "没有可用的模型上下文"
        return result

    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await graph.aget_state(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取会话上下文失败（历史已删除）: %s", e)
        result["reason"] = f"读取上下文失败: {e}"
        return result

    messages = list((getattr(snapshot, "values", None) or {}).get("messages") or [])
    if not messages:
        result["reason"] = "模型上下文为空，无需清理"
        return result
    if getattr(snapshot, "next", None):
        # 还在跑 / 停在审批等待：这时改状态会和正在执行的那一轮打架
        result["reason"] = "该会话仍在执行或等待确认，暂不改动模型上下文"
        return result

    marker = turn_message_id(turn_id)
    message_ids = [str(getattr(m, "id", "") or "") for m in messages]
    if marker not in message_ids:
        if not _legacy_turn_in_context(messages, user_input):
            result["reason"] = "这一轮本来就不在模型上下文中"
            return result
        return await _rebuild_context_after_delete(graph, thread_id, result)

    start = message_ids.index(marker)
    end = len(message_ids)
    for index in range(start + 1, len(message_ids)):
        if message_ids[index].startswith(TURN_MSG_PREFIX):
            end = index
            break

    removals = [RemoveMessage(id=mid) for mid in message_ids[start:end] if mid]
    if not removals:
        result["reason"] = "这一轮没有可在上下文中摘除的消息"
        return result

    try:
        await graph.aupdate_state(config, {"messages": removals}, as_node=SEED_AS_NODE)
    except Exception as e:  # noqa: BLE001
        logger.warning("从上下文摘除轮次失败: thread=%s (%s)", thread_id, e)
        result["reason"] = f"摘除失败: {e}"
        return result

    # 被摘掉的那一轮可能落在摘要覆盖区间里 —— 摘要必须一起失效，
    # 否则「删掉的对话」仍会以摘要的形式被模型记得。
    from app.agents.context import invalidate_summary

    invalidate_summary(thread_id)

    result["removed"] = len(removals)
    result["reason"] = f"已从上下文摘除 {len(removals)} 条消息"
    logger.info(
        "已从会话上下文摘除轮次: thread=%s turn=%s messages=%s",
        thread_id,
        turn_id,
        len(removals),
    )
    return result


async def _stream_agent_events(
    user_input: str,
    thread_id: str,
    graph: Any = None,
    attachments: list[dict[str, Any]] | None = None,
    *,
    web: bool = True,
    use_graph: bool = True,
    model: str | None = None,
    thinking: bool | None = None,
    permission_mode: str | None = None,
    workdir_root: str = "",
    turn_meta: dict[str, Any] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """
    事件流的实现层（真正的编排驱动）

    ★ Phase 4：这里是**唯一的追踪埋点位置**

    之所以只在这一层埋一次，而不是散落到各个 node 里：
    事件流是这些节点对外唯一的输出通道，所以「事件 = 链路发生了什么」。
    在 emit 上包一层，就拿到了意图、路由、工具调用、审批、检索结果、
    错误 —— 全部链路信号，且**不必改动任何一个节点**（节点里写埋点会
    侵入 HITL 重放路径，那是最不该碰的地方）。

    代价是拿不到「阶段耗时」（事件本身不带时间戳），
    所以检索与 LLM 这两段由各自模块用 `active_recorder()` 补埋 —— 见
    `rag/retriever.py::retrieve_hybrid` 与 `agents/nodes/runner.py`。
    """
    graph = graph or _graph
    # ★ 轮次身份在这里就定下来
    #
    # 用户消息的 id 由它派生（`turn:<turn_id>`），落盘也用同一个值。
    # 只有先定后写，删除某一轮时才可能在 Checkpointer 里找到它的起点；
    # 交给 store 落盘时再生成，两边就是两个不相干的 id 了。
    turn_id = uuid.uuid4().hex[:16]
    if turn_meta is not None:
        turn_meta["turn_id"] = turn_id
    recorder = begin_trace(user_input, thread_id)
    recorder.add_metadata(source="agent_event_stream")
    # call_id → 未关闭的工具 span；审批 span 与提问 span 各单独记一个
    open_tool_spans: dict[str, Any] = {}
    approval_span: Any = None
    question_span: Any = None

    queue: asyncio.Queue = asyncio.Queue()
    # 流式文本攒在这里：长期记忆的被动抽取需要「这一轮回答了什么」，
    # 而 done 事件本身是空的。在 emit 里顺手累积，比让节点回传更轻、也更不容易漏。
    answer_parts: list[str] = []
    # 思考增量的累积（`reasoning` 事件，见 emit 里同名分支）
    reasoning_parts: list[str] = []
    # 本轮 LLM 真实用量（多 Agent / 工具循环会调多次，累加）。
    # 由节点 emit 的内部信号驱动 —— token 事件只有增量文本、拿不到 usage，
    # 真实用量只在聚合后的 AIMessage 上（见 runner.py::_emit_usage）。
    usage_total: dict[str, int] = {}
    #: 本轮**最后一次** LLM 调用的 prompt token 数（不是累计）。
    #: 提前压缩要的是「下一轮大概会有多长」，把工具循环里几次调用的
    #: prompt 全加起来会严重高估（每次都带着同一份历史）。
    last_prompt_tokens = 0

    def record_event(event: dict[str, Any]) -> None:
        """把 SSE 事件翻译成 trace span（只读，不改事件本身）"""
        nonlocal approval_span, question_span
        etype = event.get("type")

        if etype == "intent":
            recorder.record(
                "intent",
                output=event.get("content"),
                intent=event.get("content"),
                confidence=event.get("confidence"),
            )

        elif etype == "agent_switch":
            recorder.record(
                f"agent:{event.get('content')}",
                kind="agent",
                output=event.get("content"),
                from_agent=event.get("from_agent"),
            )

        elif etype == "tool_call":
            span = recorder.start_span(
                f"tool:{event.get('tool_name')}",
                kind="tool",
                input=event.get("args"),
                tool_name=event.get("tool_name"),
                call_id=event.get("call_id"),
                danger_level=event.get("danger_level"),
            )
            open_tool_spans[str(event.get("call_id"))] = span

        elif etype == "tool_result":
            span = open_tool_spans.pop(str(event.get("call_id")), None)
            if span is not None:
                # 事件已带真实耗时，直接沿用；span.close() 自己算的时间会偏大
                # （它包含了等待审批的时长，而 duration_ms 只统计执行本身）
                recorder.end_span(
                    span,
                    output=event.get("result"),
                    status="ok" if event.get("success") else "error",
                    error=None if event.get("success") else str(event.get("result"))[:500],
                    success=bool(event.get("success")),
                    duration_ms=event.get("duration_ms"),
                )
            # ---------- Phase 6：审批留痕回填执行结果 ----------
            # 逻辑在 `approval_flow.backfill_execution_result` 里 ——
            # 与「留痕开单/结算」同一个模块，保证写入点仍然唯一（不变量 36）。
            backfill_execution_result(
                thread_id,
                str(event.get("call_id")),
                success=bool(event.get("success")),
                result=str(event.get("result", "")),
            )

        elif etype == "interrupt":
            approval_span = recorder.start_span(
                "approval",
                kind="event",
                input=event.get("args"),
                tool_name=event.get("tool_name"),
                call_id=event.get("call_id"),
                reason=event.get("reason"),
            )

        elif etype == "approval_auto":
            # 预授权自动放行：没有等待，所以不是一个 span，而是一条即时记录。
            # 它必须进 trace —— 「这次危险操作没问我就执行了」是排查时
            # 第一个要能回答的问题。
            recorder.record(
                "approval:auto",
                kind="event",
                output=event.get("content"),
                tool_name=event.get("tool_name"),
                call_id=event.get("call_id"),
                grant_id=event.get("grant_id"),
                remaining=event.get("remaining"),
            )

        elif etype == "state_update" and event.get("state_key") == "approval":
            if approval_span is not None:
                approved = bool(event.get("value"))
                recorder.end_span(
                    approval_span,
                    output="approved" if approved else "rejected",
                    status="ok" if approved else "error",
                    approved=approved,
                )
                approval_span = None

        elif etype == "question":
            # 提问也是「停下来等人」，但它不是审批：span 名为 question，
            # 记录的是「模型缺了哪条信息」。两者不能混进同一个 kind。
            question_span = recorder.start_span(
                "question",
                kind="event",
                input=event.get("questions"),
                call_id=event.get("call_id"),
                question_id=event.get("question_id"),
                requestor=event.get("requestor"),
            )

        elif etype == "question_resolved":
            if question_span is not None:
                recorder.end_span(
                    question_span,
                    output=event.get("status"),
                    status="ok" if event.get("status") == "answered" else "error",
                    question_id=event.get("question_id"),
                    answers=event.get("answers"),
                )
                question_span = None

        elif etype == "memory":
            # 记忆写入也是链路的一部分：排查「为什么模型这次用了 PPT」时，
            # 第一件事就是确认这轮到底注入/写入了什么
            recorder.record(
                "memory",
                kind="event",
                output=event.get("content"),
                action=event.get("action"),
                items=event.get("items"),
            )

        elif etype == "error":
            recorder.record(
                "error",
                status="error",
                error=str(event.get("content"))[:1000],
                output=event.get("content"),
            )

    def emit(event: dict[str, Any]) -> None:
        nonlocal last_prompt_tokens
        # ---------- 内部信号：LLM 真实用量（不进 SSE） ----------
        # 这是节点顺手 emit 出来的**旁路数据**，这里把它吞掉：不进队列、不进事件流，
        # 只用来给收尾的 `done` 补一个 `usage` 字段（与 `done.files` 同一套做法）。
        # 用独立的 `_internal` key 而不是 `type`，是为了让「它不是一个 SSE 事件」
        # 成为结构上的保证 —— 将来有人漏改这里，最坏也只是多推一个未知事件，
        # 而不是把用量误当成某个已知事件类型渲染出来。
        if event.get("_internal") == "usage":
            usage = event.get("usage") or {}
            _accumulate_usage(usage_total, usage)
            prompt = usage.get("prompt_tokens")
            if isinstance(prompt, (int, float)) and int(prompt) > 0:
                last_prompt_tokens = int(prompt)
            return
        try:
            record_event(event)
        except Exception as e:  # noqa: BLE001
            # 观测失败绝不能影响用户看到的回答 —— 这是观测层与业务层的边界
            logger.warning("追踪埋点失败（已忽略）: %s", e)
        if event.get("type") == "token":
            answer_parts.append(str(event.get("content", "")))
        elif event.get("type") == "reasoning":
            # 思考增量的实时采集：与 token 同一套「顺手累积」的做法。
            # 攒它是为了 `done` 与落盘 —— 用户在流式里已经实时看到了思考，
            # 但「回看历史」与「折叠块」需要一份完整正文（见外层 wrapper）。
            reasoning_parts.append(str(event.get("content", "")))
        queue.put_nowait(event)

    if graph is None:
        recorder.record("error", status="error", error="多 Agent 图未初始化")
        recorder.finish(status="error", error="多 Agent 图未初始化")
        yield {"type": "error", "content": "多 Agent 图未初始化"}
        yield _done_event(usage_total)
        return

    config = {
        "configurable": {"thread_id": thread_id, "emit": emit},
        "recursion_limit": RECURSION_LIMIT,
    }

    # ---------- Phase 7：恢复历史会话的模型上下文 ----------
    # 打开几天前的会话时，Checkpointer 里可能已经没有它的状态（Redis 重启 /
    # 换数据卷 / 降级成 InMemorySaver），而界面仍然显示着全部历史 ——
    # 「显示着 10 轮、模型却不记得」这种不一致最容易被当成 bug。
    # 这里用落盘的历史把上下文补回去；在线则什么都不做（`seed_thread_from_history`
    # 内部先查本地历史，因此**新会话不会多花一次 Redis 往返**）。
    seed_result = await seed_thread_from_history(graph, thread_id)
    if seed_result.get("seeded"):
        logger.info("本轮开始前从会话历史恢复上下文: thread=%s", thread_id)

    # ---------- 长期记忆：显式指令先落库（Phase 5） ----------
    # 「记住：X」的意图是确定的，走正则同步处理（零延迟、可单测）。
    # 放在图执行**之前**：即使这一轮回答中途失败或断流，
    # 用户明确提出的要求也不该丢 —— 它是用户的指令，不是回答的副产品。
    saved_manager = None
    explicit_saved: list[str] = []
    try:
        from app.memory import get_manager

        saved_manager = get_manager()
        explicit_saved = [i.value for i in saved_manager.remember_explicit(user_input, thread_id)]
        if explicit_saved:
            logger.info("已记住用户显式偏好 %s 条: %s", len(explicit_saved), explicit_saved)
    except Exception as e:  # noqa: BLE001
        logger.debug("显式记忆处理失败（已忽略）: %s", e)

    # ---------- Phase 5：进门先跑 UserPromptSubmit 钩子 ----------
    # 放在图执行**之前**：它的定位是「这个问题进门时先检查一遍」，
    # 产出的是本轮上下文（脚本能读用户原话，于是可以回一句
    # 「今天日程里已经在 15:00 有个会」）。
    #
    # ⚠️ 它**拦不住**这一步（见 `hooks/executor.py::fire` 末尾的收口）：
    # 非工具事件的 deny 一律降级成界面提示。理由是产品而不是技术 ——
    # 这是一个办公应用，用户的问话被一条他自己配的脚本静默吞掉、
    # 界面上只剩一行 stderr，比「问错了」难排查一个数量级。
    # 真正要挡的东西都在工具那一层，那里有 `deny` 规则与钩子两条正路。
    hook_context = ""
    try:
        prompt_hook = await fire(
            PROMPT_SUBMIT,
            build_payload(PROMPT_SUBMIT, prompt=user_input, session={"thread_id": thread_id}),
        )
        hook_context = "\n".join(prompt_hook.context)
        for notice in prompt_hook.notice:
            yield {"type": "hook_notice", "content": notice, "event": PROMPT_SUBMIT}
    except Exception as e:  # noqa: BLE001
        # 钩子层坏掉不能让对话断掉 —— 与观测层同一边界
        logger.warning("UserPromptSubmit 钩子异常（已忽略）: %s", e)

    graph_input: Any = initial_state(
        user_input, thread_id, attachments, turn_id=turn_id, web=web, graph=use_graph,
        model=model, thinking=thinking, permission_mode=permission_mode,
        hook_context=hook_context, workdir_root=workdir_root,
    )

    # 后台驱动任务的引用：客户端断连时必须取消，否则图会继续跑完
    # （LLM 继续消耗 token、工具继续执行副作用，而结果没有任何人消费）。
    # 「断连即停止上游调用」是 SSE 生产实践的第一条 —— 见 chat.py 的说明。
    runner: asyncio.Task | None = None

    try:
        while True:
            runner = asyncio.create_task(_drive_graph(graph, graph_input, config, queue))

            # ---------- 边执行边推送事件 ----------
            while True:
                event = await queue.get()
                if event is _SENTINEL:
                    break
                yield event

            await runner  # 传播异常（若已捕获则内部已 emit error）
            runner = None

            # ---------- 检查是否停在一个挂起点（审批 或 提问） ----------
            snapshot = await graph.aget_state(config)
            interrupts = _collect_interrupts(snapshot)

            if not interrupts:
                break

            payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
            payload = payload if isinstance(payload, dict) else {"reason": str(payload)}
            call_id = str(payload.get("call_id", "unknown"))
            tool_name = str(payload.get("tool_name", ""))

            # ★ 提问与审批是两条路（「提问不是审批」的结构性判别）
            #
            # 只看 payload 的 `kind`，不看事件文本、不看工具名 —— 模型无法
            # 通过措辞把自己伪装成审批；审批的 payload 由宿主构造，没有 `kind`。
            # 两条路的等待、留痕、恢复载荷都不同，混用会让审计答不出
            # 「这次到底是授权还是澄清」。
            if is_question_payload(payload):
                logger.info(
                    "触发模型提问: thread=%s call=%s", thread_id, call_id
                )
                outcome = QuestionOutcome()
                # 留痕/推送/等待都在 `questions/flow.py`（唯一写入点）；
                # 与审批一样，决策走内部消息，不推给前端。
                async for question_event in run_pending_question(
                    thread_id=thread_id,
                    call_id=call_id,
                    questions=list(payload.get("questions") or []),
                    run_id=str(payload.get("run_id") or ""),
                    employee_id=str(payload.get("employee_id") or ""),
                    requestor=str(payload.get("requestor") or ""),
                ):
                    answered = read_question_answer(question_event)
                    if answered is not None:
                        outcome = answered
                        break
                    yield question_event
                graph_input = Command(resume=outcome.to_resume())
                continue

            logger.info("触发 HITL 审批: thread=%s call=%s tool=%s", thread_id, call_id, tool_name)

            # ★ 审批的留痕与推送都在 `approval_flow.run_pending_approval` 里
            # （不变量 36 的唯一写入点）——
            # 主图与子图执行器都**调用**它，都不自己写留痕。
            # 两处都写的话重载时「后写的赢」，审计内容取决于竞态。
            #
            # ⚠️ 决策结果走**内部消息**传（`_internal`），不进 SSE ——
            # 异步生成器不能带 return 值，而调用方又必需这个结果。
            # 子图执行器里是同一套写法，两处保持一致。
            approved = False
            async for approval_event in run_pending_approval(
                thread_id=thread_id,
                call_id=call_id,
                tool_name=tool_name,
                tool_args=payload.get("args") or {},
                danger_reason=str(payload.get("reason", "该操作需要人工确认")),
                danger_level=str(payload.get("danger_level", "danger")),
                decision_source=str(payload.get("decision_source", "danger_level")),
                permission_mode=str(payload.get("permission_mode", "")),
            ):
                decision = read_decision(approval_event)
                if decision is not None:
                    approved = decision
                    break
                # 预授权路径不挂起，事件直接推给前端；
                # interrupt 路径下推完就进入等待（broker 已注册）
                yield approval_event

            # 恢复执行（resume 走 Command，无需再传初始输入）
            graph_input = Command(resume={"approved": approved})

        # ---------- 长期记忆：回执 + 被动抽取 ----------
        # 回执放在回答之后推：不打断用户阅读，又给了「我记住了」的明确反馈
        # （显式路径是同步的，所以这里能立刻说清楚记住了什么）。
        if explicit_saved:
            yield {
                "type": "memory",
                "action": "saved",
                "content": "已记住：" + "；".join(explicit_saved),
                "items": explicit_saved,
            }
        _, clean_answer = split_think("".join(answer_parts))
        if saved_manager is not None:
            # fire-and-forget：绝不能让「学一条偏好」把流关闭推迟几秒，
            # 那会让前端在答案已经显示完之后还一直转圈。
            # 传**摘掉推理之后**的回答：让模型拿一段英文内心独白去判断
            # 「用户有什么偏好」，只会抽出噪声。
            saved_manager.schedule_inferred(user_input, clean_answer, thread_id, turn_id=turn_id)

        # ---------- Phase 5：本轮结束（Stop 钩子）----------
        # 只通知，不改变任何判定 —— 本轮已经答完了，脚本此时说什么都晚了。
        # 它的用途是「事后检查」：把这一轮的答案交给脚本，
        # 由它去落一条审计、发一个桌面通知、或者在界面上贴一句提醒。
        # ⚠️ 故意**不**实现主流 agent 的「Stop 钩子驳回、让模型再说一轮」：
        # 那需要重新进图，而重新进图会把这一轮的审批与工具循环再走一遍 ——
        # 为一个通知功能换来一条重放路径，不值。
        try:
            stop_hook = await fire(
                STOP,
                build_payload(STOP, session={"thread_id": thread_id}, answer=str(clean_answer)[:8000]),
            )
            for notice in stop_hook.notice:
                yield {"type": "hook_notice", "content": notice, "event": STOP}
        except Exception as e:  # noqa: BLE001
            logger.warning("Stop 钩子异常（已忽略）: %s", e)

        # ---------- 提前压缩：趁用户读答案的时间做掉下一轮的成本 ----------
        # 触发依据是本轮**真实**的 prompt_tokens（模型自己报的），
        # 比任何本地估算都准。它在后台跑，不推迟这一轮的 done。
        try:
            from app.agents.context import schedule_proactive_compaction

            schedule_proactive_compaction(
                graph=graph,
                thread_id=thread_id,
                model=str(model or ""),
                prompt_tokens=last_prompt_tokens,
            )
        except Exception as e:  # noqa: BLE001 — 优化失败不该影响收尾
            logger.debug("安排后台上下文压缩失败（已忽略）: %s", e)

        yield _done_event(usage_total)
        recorder.finish()

    except asyncio.CancelledError:
        # 客户端断连：trace 也要落盘 —— 中断本身就是一种最需要被观测的情况
        cancel_source = "graph_runner" if runner is not None and runner.cancelled() else "response_or_client"
        logger.info("事件流取消: thread=%s source=%s", thread_id, cancel_source)
        recorder.add_metadata(cancel_source=cancel_source)
        # ★ 停掉还在后台跑的图任务：断连后 LLM 流仍在计费、工具仍可能执行，
        # 而产出再也不会被任何人消费。cancel 是幂等的，先于 await 是刻意的 ——
        # 在 async generator 的关闭路径里 await 有「忽略 GeneratorExit」的风险。
        if runner is not None and not runner.done():
            runner.cancel()
        # ★ 本轮派发的子任务一起掐掉（见 _abort_turn_subtasks 的说明）
        _abort_turn_subtasks(thread_id, turn_id)
        # Finish spans before serializing the trace; otherwise finish() closes
        # an in-flight tool as 'ok' with no result, obscuring the interruption.
        for span in list(open_tool_spans.values()):
            recorder.end_span(span, status="aborted")
        if approval_span is not None:
            recorder.end_span(approval_span, status="aborted")
        if question_span is not None:
            recorder.end_span(question_span, status="aborted")
        recorder.finish(status="cancelled")
        raise

    except Exception as e:  # noqa: BLE001
        logger.error("事件流异常: %s", e, exc_info=True)
        recorder.record("fatal", status="error", error=str(e)[:1000])
        recorder.finish(status="error", error=str(e)[:1000])
        # 显式偏好在图执行之前就已落库，这里补一次回执，
        # 否则「我明明说了记住，界面却说失败了」——记忆其实已经存下了。
        if explicit_saved:
            yield {
                "type": "memory",
                "action": "saved",
                "content": "已记住：" + "；".join(explicit_saved),
                "items": explicit_saved,
            }
        yield {"type": "error", "content": str(e)}
        yield _done_event(usage_total)

    finally:
        # 兜底：无论从哪条路径退出（GeneratorExit / CancelledError / 正常结束），
        # 后台驱动任务都不能残留 —— 正常结束时它已完成，cancel() 是无害空操作。
        if runner is not None and not runner.done():
            runner.cancel()
        # 未关闭的 span（例如审批/提问还没回来就断连）在这里统一收尾，
        # 否则 trace 会留下 duration 为 None 的悬空记录
        for span in list(open_tool_spans.values()):
            recorder.end_span(span, status="aborted")
        if approval_span is not None:
            recorder.end_span(approval_span, status="aborted")
        if question_span is not None:
            recorder.end_span(question_span, status="aborted")
        recorder.finish()
        set_current_recorder(None)


__all__ = [
    "build_graph",
    "init_graph",
    "get_graph",
    "shutdown_graph",
    "get_agent_event_stream",
    "seed_thread_from_history",
    "remove_turn_messages_from_context",
    "discard_thread_context",
    "MAIN_NODES",
    "NODE_INPUT_SCHEMAS",
    "RECURSION_LIMIT",
    "SEED_AS_NODE",
]
