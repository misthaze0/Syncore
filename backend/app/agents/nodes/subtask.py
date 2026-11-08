"""
子图的节点实现 —— planner + tool_executor 的**适配层**。

★ 为什么是「适配」而不是「复用」

`runner.py` 里的 `run_planner_turn` / `tool_executor_node` 是 Router 时代
5 个专员共用的实现，逻辑是好的（`interrupt()` 重放安全、工具治理、追踪埋点
全都在里面），所以**一行都不改**。但它们带着两处主图特有的假设：

1. **检索 query 来自 `state["user_input"]`** —— 那是「用户这一轮说的话」。
   子图里对应的是 `instruction`（Supervisor 派下来的任务描述）。
   两者语义不同：用户可能说「查一下差旅标准然后发给张三」，
   而分给知识库专员的 instruction 是「查询差旅报销标准（不含订票流程）」。
   拿原始输入去检索会把「发给张三」也当 query，召回质量直接变差。

2. **`state_update` 是给主对话的顶栏看的** —— 它报的是「当前执行: xxx_agent」，
   在子图里没有对应的界面。而且计划已定：`state_update` 收窄成
   只有审批决策一种（`types/index.ts` 层面），所以子图里那些装饰性的
   必须**不发**，而不是发出去让前端丢掉。

所以这一层只做两件事：字段改名 + 事件过滤。逻辑一行不动。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.emitter import get_emitter
from app.agents.nodes.runner import run_planner_turn, tool_executor_node
from app.agents.subgraph import SubtaskPlannerInput, SubtaskToolExecutorInput

logger = logging.getLogger(__name__)

#: 子图内部要**滤掉**的事件类型。
#:
#: ★ 为什么必须滤，而不是「前端忽略就行」
#:
#: ① `token` —— 子图可能产生几万条增量。它们**不是**发给主对话的
#:    （子任务的回答要走 `task_status` 工具或轮询拿），
#:    全推过去会把主对话的 SSE 通道灌满，用户真正在等的那条流被挤死。
#:
#: ② `state_update` —— 报的是「当前执行: file_agent」这种内部编排信息。
#:    子图跑在后台，它报的「当前执行」对用户毫无意义。
#:    ⚠️ 复用的实现会发两种 `state_key`：`active_agent`（runner.py:299-304）
#:    与 `max_steps_reached`（runner.py:443-448）——**两种都是装饰性的**，
#:    所以整个类型都滤掉。
#:    审批决策那种 `state_update` 由新增的 `approval_flow.py` 单独产出
#:    （不变量 36 的写入点），不经过这个 emit 通道 ——
#:    所以「滤掉复用实现发的」与「放行 approval_flow 发的」不冲突。
#:
#: ③ `agent_switch` —— 同理，是主对话顶栏的编排指示。
#:
#: ⚠️ 注意滤掉 `token` **不会**让子 Agent 拿不到自己的回答：
#: 回答走 `final_answer` / `sub_agent_result` 状态字段，经 `mark_succeeded` 落盘，
#: 不依赖事件流。事件流在这里只是「进度」的载体。
_SUPPRESSED_IN_SUBTASK = frozenset({"token", "state_update", "agent_switch"})


def _adapter_emit(config: RunnableConfig) -> tuple[Any, str]:
    """
    造一个「翻译过的 emit」返回给复用的实现。

    返回 `(emit, 原始 emit)`：节点自己要用原始 emit 发子图特有的事件
    （比如进度的第一行「开始 xxx」），那类事件不该经过过滤。

    ★ `stream_tokens`：员工私聊需要把 `token` 逐字流式推给用户（像主对话那样），
    而普通后台子任务把 token 折叠成 progress（见 `tasks/executor.py`）。
    这个开关放在 `configurable` 里（不进 Checkpointer），由调用方决定。
    """
    raw = get_emitter(config)
    cfg = config.get("configurable", {}) or {}
    stream_tokens = bool(cfg.get("stream_tokens", False))

    def emit(event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "token":
            if not stream_tokens:
                return
        elif etype in _SUPPRESSED_IN_SUBTASK:
            return
        raw(event)

    return emit, raw


async def subtask_planner_node(
    state: SubtaskPlannerInput, config: RunnableConfig
) -> dict[str, Any]:
    """
    子图的 planner：跑一轮 LLM 决策，产出待执行工具队列。

    state 是 `SubtaskPlannerInput` 投影（build_subgraph 按 input_schema 过滤后传入），
    只读任务描述、对话历史、检索来源与本轮选项。
    """
    # 投影里叫 `instruction`，而复用的实现读 `state["user_input"]`。
    # ★ 这里做的是**字段改名**，不是语义替换：
    # 传过去的值就是 instruction 本身（Supervisor 派下来的那份描述），
    # 不做改写、不做拼接 —— 任何加工都会让「派发什么就执行什么」这条保证失效。
    instruction = state.get("instruction", "")
    agent_name = state.get("agent", "")

    emit, _raw = _adapter_emit(config)

    # 复用主图的实现，喂它一份「它认识的名字」的字典
    adapted = {
        "user_input": instruction,  # ★ 检索 query = 任务描述（见模块注释）
        "messages": state.get("messages") or [],
        "active_agent": state.get("active_agent") or agent_name,
        "agent_chain": [],  # 子图里没有编排链路，这个字段不被读取
        "sources": state.get("sources") or [],
        "sources_ready": bool(state.get("sources_ready")),
        "tool_calls": state.get("tool_calls") or [],
        "web_enabled": bool(state.get("web_enabled", True)),
        "graph_enabled": bool(state.get("graph_enabled", True)),
        "model": state.get("model") or "",
        "employee_id": state.get("employee_id") or "",
        # 钩子的补充上下文原样带过去（复用的实现把它拼进 system prompt 末尾）
        "hook_context": state.get("hook_context") or "",
        # ★ 身份字段：上下文摘要按 (thread_id, task_id) 隔离 ——
        # 同一会话里两次「查资料」是两件事，共用摘要会让第二次任务
        # 「记得」第一次的用户诉求。工具输出落盘按 thread_id 归属，
        # 删会话时才能一并清理。
        "thread_id": state.get("thread_id") or "",
        "task_id": state.get("task_id") or "",
    }

    updates = await run_planner_turn(adapted, {**config, "configurable": {**config.get("configurable", {}), "emit": emit}}, agent_name)

    # 过滤掉子图不关心 / 不该外传的字段
    out: dict[str, Any] = {
        "active_agent": agent_name,
        "pending_calls": updates.get("pending_calls") or [],
        "final_answer": updates.get("final_answer") or "",
        "sub_agent_result": updates.get("sub_agent_result") or "",
        "final_failure": updates.get("final_failure") or "",
    }
    if "messages" in updates:
        out["messages"] = updates["messages"]
    if updates.get("sources") is not None:
        out["sources"] = updates["sources"]
    if "sources_ready" in updates:
        out["sources_ready"] = updates["sources_ready"]
    return out


async def subtask_tool_executor_node(
    state: SubtaskToolExecutorInput, config: RunnableConfig
) -> dict[str, Any]:
    """
    子图的 tool_executor：执行队列头的**一个**工具。

    ★ 与主图 `tool_executor_node` 完全同构，不变量 6 在这里同样成立：
    「一次 superstep 一个工具」是 `interrupt()` 重放不重复执行副作用的前提。
    所以这个节点也是原样复用，只换了 emit 与字段名。
    """
    emit, _raw = _adapter_emit(config)

    adapted = {
        "pending_calls": state.get("pending_calls") or [],
        "active_agent": state.get("active_agent") or "",
        "tool_calls": state.get("tool_calls") or [],
        "approval_log": state.get("approval_log") or [],
        # ★ 三个身份字段原样透传（不做任何加工）：复用实现靠它们构造 `ToolContext`。
        # thread_id 用**状态里的**用户会话而不是 config 里那个 `subtask_<task_id>`；
        # task_id 让同一会话里并发的子任务各自有自己的 shell 会话（见 `ToolContext.session_key`）。
        "thread_id": state.get("thread_id") or "",
        "task_id": state.get("task_id") or "",
        "employee_id": state.get("employee_id") or "",
        "run_id": state.get("run_id") or "",
        # ★ 权限模式同样原样透传。它是**任务建立时冻结的那一档**
        # （见 `subgraph.initial_subtask_state`），这里不重算 ——
        # 重算的话跑到一半用户切个档，后半段就换了套判定依据。
        "permission_mode": state.get("permission_mode") or "manual",
        # 工作区根目录同样原样透传：复用实现靠它构造 `ToolContext.workspace_root`，
        # 漏了这一行 = 子任务在沙箱里找用户项目里的文件（不报错，只说没找到）。
        "workdir_root": state.get("workdir_root") or "",
    }

    result = await tool_executor_node(
        adapted,
        {**config, "configurable": {**config.get("configurable", {}), "emit": emit}},
    )

    # 过滤掉与主图同构的部分，只留子图需要的
    out: dict[str, Any] = {
        "pending_calls": result.get("pending_calls") or [],
        "current_call": result.get("current_call"),
    }
    if "messages" in result:
        out["messages"] = result["messages"]
    if "tool_calls" in result:
        out["tool_calls"] = result["tool_calls"]
    if "approval_log" in result:
        out["approval_log"] = result["approval_log"]
    return out


__all__ = ["subtask_planner_node", "subtask_tool_executor_node"]
