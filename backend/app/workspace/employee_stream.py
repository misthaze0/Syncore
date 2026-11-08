"""
数字员工私聊流 —— 员工在独立聊天界面里解决问题的 SSE 事件源。

★ 它与主对话流（`agents/graph.py::get_agent_event_stream`）的关系

同构，但驱动的是**子图**（`planner ⇄ tool_executor`）而不是主 Supervisor 图：
- 员工是一个固定 `base_agent` 的单 Agent，跑 `run_planner_turn` 那个实现；
- per-employee 的 persona / 工具白名单由 `employee_id` 随子图状态流转，
  在 `runner.py` 单点注入（见那里「数字员工覆盖」的注释）；
- 事件契约与主对话一致（token / sources / tool_call / tool_result /
  interrupt / approval_auto / done / error），所以前端复用同一套渲染。

★ 可见性隔离（需求 3、4）的写入侧保证

这里的 token / reasoning / tool_call 原始流只进**这一条** SSE 通道 +
`conversation_store`（`emp_<id>` 线程），**不写群组消息表**。
群组消息由 `orchestrator.py` 显式生成成品文本，两者在存储层物理分离。

★ 落盘与断连

`finally` 里同步 `conversation_store.append_turn`（与会话历史同一约定：
摘掉推理后的干净回答 + 事件流快照），断连时状态记为 aborted。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator

from langgraph.types import Command

from app.agents.approval_flow import read_decision, run_pending_approval
from app.agents.subgraph import SUBGRAPH_RECURSION_LIMIT, build_subgraph, initial_subtask_state
from app.agents.think import split_think
from app.conversations import STATUS_ABORTED, STATUS_ERROR, STATUS_OK, conversation_store
from app.conversations.files import collect_written_files
from app.config import settings
from app.questions.flow import read_question_answer, run_pending_question
from app.questions.models import QuestionOutcome, is_question_payload
from app.workspace import is_employee_thread_id, workspace_store
from app.workspace.models import normalize_base_agent

logger = logging.getLogger(__name__)

# 队列结束哨兵（与 graph.py 同构）
_SENTINEL = object()


def _done_event(usage: dict[str, int]) -> dict[str, Any]:
    """构造 `done` 事件（与 graph.py::_done_event 同构）。"""
    event: dict[str, Any] = {"type": "done", "content": ""}
    if usage:
        event["usage"] = dict(usage)
    return event


def _accumulate_usage(total: dict[str, int], usage: dict[str, Any]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + int(value)


def _collect_interrupts(snapshot: Any) -> list[Any]:
    """从状态快照中提取所有 interrupt（与 graph.py 同构）。"""
    interrupts: list[Any] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts.extend(getattr(task, "interrupts", ()) or ())
    return interrupts


async def _drive_subgraph(
    subgraph: Any, graph_input: Any, config: dict[str, Any], queue: asyncio.Queue
) -> None:
    """后台任务：驱动子图执行，节点事件经 emit 进入队列。"""
    try:
        async for _ in subgraph.astream(graph_input, config, stream_mode="updates"):
            pass
    except Exception as e:  # noqa: BLE001
        logger.error("员工子图执行异常: %s", e, exc_info=True)
        queue.put_nowait({"type": "error", "content": f"执行失败: {e}"})
    finally:
        queue.put_nowait(_SENTINEL)


async def get_employee_event_stream(
    employee_id: str,
    user_input: str,
    thread_id: str,
    *,
    web: bool = True,
    graph: bool = True,
    model: str | None = None,
    thinking: bool | None = None,  # noqa: ARG001 —— 子图暂不消费，预留对齐主对话
) -> AsyncGenerator[dict[str, Any], None]:
    """
    员工私聊的全量事件流（对外唯一入口）。

    产出的事件类型与主对话一致，前端复用 `applyAgentEvent` 渲染。
    """
    # ---------- 校验 ----------
    employee = workspace_store.get_employee(employee_id)
    if employee is None:
        yield {"type": "error", "content": "数字员工不存在"}
        yield _done_event({})
        return
    if not employee.get("enabled", True):
        yield {"type": "error", "content": "该数字员工已停用"}
        yield _done_event({})
        return
    if not is_employee_thread_id(thread_id):
        yield {"type": "error", "content": "会话标识无效"}
        yield _done_event({})
        return

    from app.core.redis_client import get_checkpointer

    checkpointer = get_checkpointer()
    if checkpointer is None:
        yield {"type": "error", "content": "会话状态存储未就绪，员工暂不可用"}
        yield _done_event({})
        return

    subgraph = build_subgraph(checkpointer=checkpointer)
    base_agent = normalize_base_agent(employee.get("base_agent"))

    # 登记员工线程映射（幂等），供「按线程反查员工」与扩展使用。
    workspace_store.link_thread(employee_id, thread_id, kind="private")

    # ---------- 多轮记忆：从会话历史播种回 Checkpointer ----------
    try:
        from app.agents.graph import seed_thread_from_history

        await seed_thread_from_history(subgraph, thread_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("员工会话上下文恢复失败（按新会话继续）: %s", e)

    state = initial_subtask_state(
        instruction=user_input,
        agent=base_agent,
        task_id=thread_id,
        thread_id=thread_id,
        web=web,
        graph=graph,
        model=model or str(employee.get("model") or "") or settings.LLM_MODEL,
        employee_id=employee_id,
    )

    # ---------- 事件流驱动 ----------
    turn_events: list[dict[str, Any]] = []
    answer_parts: list[str] = []
    status = STATUS_OK
    error = ""
    turn_usage: dict[str, int] = {}
    queue: asyncio.Queue = asyncio.Queue()

    def emit(event: dict[str, Any]) -> None:
        # 内部信号：LLM 真实用量（不进 SSE），只用来给 done 补 usage。
        if event.get("_internal") == "usage":
            _accumulate_usage(turn_usage, event.get("usage") or {})
            return
        queue.put_nowait(event)

    config: dict[str, Any] = {
        # stream_tokens=True：员工私聊要像主对话一样逐字流式（见 subtask.py::_adapter_emit）
        "configurable": {"thread_id": thread_id, "emit": emit, "stream_tokens": True},
        "recursion_limit": SUBGRAPH_RECURSION_LIMIT,
    }

    runner: asyncio.Task | None = None
    try:
        graph_input: Any = state
        while True:
            runner = asyncio.create_task(_drive_subgraph(subgraph, graph_input, config, queue))
            while True:
                event = await queue.get()
                if event is _SENTINEL:
                    break
                if event.get("type") == "token":
                    answer_parts.append(str(event.get("content", "")))
                turn_events.append(event)
                if event.get("type") == "error":
                    status = STATUS_ERROR
                    error = str(event.get("content", ""))
                yield event

            await runner
            runner = None

            # ---------- 检查是否在等待人工（审批或提问） ----------
            snapshot = await subgraph.aget_state(config)
            interrupts = _collect_interrupts(snapshot)
            if not interrupts:
                break

            payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
            payload = payload if isinstance(payload, dict) else {"reason": str(payload)}
            call_id = str(payload.get("call_id", "unknown"))
            tool_name = str(payload.get("tool_name", ""))

            # ★ 提问与审批是两条路（只看 payload 的 `kind`，与主图同一条规则）
            # 员工私聊也是用户直接在看的一条路：她在私聊里问「要不要拆图层」，
            # 用户必须当场答得了；只处理审批的话这张卡片永远弹不出来，
            # 任务会在超时后基于一句假设继续跑。
            if is_question_payload(payload):
                logger.info(
                    "员工触发模型提问: thread=%s call=%s", thread_id, call_id
                )
                outcome = QuestionOutcome()
                async for question_event in run_pending_question(
                    thread_id=thread_id,
                    call_id=call_id,
                    questions=list(payload.get("questions") or []),
                    employee_id=employee_id,
                    requestor=str(employee.get("name") or "").strip() or employee_id,
                ):
                    answered = read_question_answer(question_event)
                    if answered is not None:
                        outcome = answered
                        break
                    turn_events.append(question_event)
                    yield question_event
                graph_input = Command(resume=outcome.to_resume())
                continue

            logger.info("员工触发 HITL 审批: thread=%s call=%s tool=%s", thread_id, call_id, tool_name)

            approved = False
            async for approval_event in run_pending_approval(
                thread_id=thread_id,
                call_id=call_id,
                tool_name=tool_name,
                tool_args=payload.get("args") or {},
                danger_reason=str(payload.get("reason", "该操作需要人工确认")),
                danger_level=str(payload.get("danger_level", "danger")),
                # ★ 护栏 6：审批必须写出「是谁在请求」。私聊线程本身就带着答案 ——
                #   thread_id 形如 `emp_<id>`，但那是标识不是名字，界面上要的是名字。
                employee_id=employee_id,
                requestor=str(employee.get("name") or "").strip() or employee_id,
            ):
                decision = read_decision(approval_event)
                if decision is not None:
                    approved = decision
                    break
                yield approval_event

            graph_input = Command(resume={"approved": approved})

        # ---------- 收尾 done 事件 ----------
        reasoning, clean_answer = split_think("".join(answer_parts))
        done = _done_event(turn_usage)
        written = collect_written_files(turn_events)
        if written:
            done["files"] = written
        if reasoning:
            done["reasoning"] = reasoning
        if clean_answer:
            done["answer"] = clean_answer
        yield done

    except asyncio.CancelledError:
        status = STATUS_ABORTED
        raise
    except GeneratorExit:
        status = STATUS_ABORTED
        raise
    except Exception as e:  # noqa: BLE001
        status = STATUS_ERROR
        error = str(e)
        logger.error("员工事件流异常: %s", e, exc_info=True)
        yield {"type": "error", "content": str(e)}
        yield _done_event(turn_usage)
    finally:
        if runner is not None and not runner.done():
            runner.cancel()
        # 落盘（同步写，与 graph.py 同一约定：摘掉推理后的干净回答 + 事件流快照）
        reasoning, clean_answer = split_think("".join(answer_parts))
        try:
            conversation_store.append_turn(
                thread_id=thread_id,
                user_input=user_input,
                answer=clean_answer,
                events=turn_events,
                status=status,
                error=error,
                usage=turn_usage,
                reasoning=reasoning,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("员工会话历史写入失败（不影响本轮回答）: %s", e)


__all__ = ["get_employee_event_stream"]
