"""
提问流程 —— 留痕、推送、等待、结算的**唯一**实现。

两个驱动层（主图 `graph.py` 和子图 `tasks/executor.py`）都只调用这里，
不自己写留痕、不自己推事件。理由与 `agents/approval_flow.py` 相同：
两处各写一遍，第二次有人改其中一处就会漂移；而漂移的症状是
「主对话的问题有记录、后台任务的没有」—— 不报错，只是审计缺了一角。

★ 与审批流程的差异

1. **不持有并发闸门**。闸门的获取/释放由调用方负责（且必须在 finally 里）。
   这样「闸门卡住」和「留痕写失败」不会互相掩盖。
   闸门本身是独立的 `question_gate` —— 审批的队列不能被提问占着，
   否则用户在批一个危险操作时，一条不相关的提问会排在它前面。
2. **事件类型独立**：`question` / `question_resolved`，不复用 `interrupt`。
   复用会让前端必须从 `kind` 猜「该弹审批还是提问」，而那类隐式约定
   在第三个消费方出现时必然错。
3. **超时不是拒绝**：超时的结果是 `timeout`，跳过是 `skipped`。
   两者都让任务继续，但审计里必须能分开。

★ 决策怎么拿（与审批同一个语言限制）

Python 不允许 async generator 带 return 值，而调用方必需这个结果。
所以最后一条消息是 `{"_internal": "_question_answer", "outcome": ...}`，
调用方用 `read_question_answer(event)` 取出并 `break`，不推给前端。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncGenerator

from app.agents.approval_gate import ApprovalGate
from app.config import settings
from app.hooks import NOTIFICATION, build_payload, fire
from app.questions.broker import question_broker
from app.questions.models import (
    ASK_USER_TOOL,
    QUESTION_KIND,
    REASON_CANCELLED,
    STATUS_ANSWERED,
    STATUS_SKIPPED,
    QuestionOutcome,
)
from app.questions.records import question_store
from app.workspace import events as group_events

logger = logging.getLogger(__name__)

#: 提问队列的闸门（与审批队列**分开**）。
#: 同一时刻只暴露一张待回答的卡片：前端的 `pendingQuestion` 是单个对象，
#: 并行子任务同时提问会互相覆盖，用户回答的可能是另一张卡片上的问题。
question_gate = ApprovalGate()

#: 内部决策消息的判别键（不是 SSE 事件）
DECISION_KEY = "_question_answer"


def _decision(outcome: QuestionOutcome) -> dict[str, Any]:
    return {"_internal": DECISION_KEY, "outcome": outcome}


def read_question_answer(event: dict[str, Any]) -> QuestionOutcome | None:
    """从一条消息里读出提问结果；不是决策消息则返回 None。"""
    if event.get("_internal") != DECISION_KEY:
        return None
    outcome = event.get("outcome")
    return outcome if isinstance(outcome, QuestionOutcome) else None


async def run_pending_question(
    *,
    thread_id: str,
    call_id: str,
    questions: list[dict[str, Any]],
    run_id: str = "",
    employee_id: str = "",
    requestor: str = "",
    timeout: int | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """
    处理一次模型提问：留痕 → 推事件 → 等回答 → 结算留痕。

    Args:
        thread_id: 归属会话（工作空间路径下就是群组 id）
        call_id:   模型给的这次工具调用 id（回答要回到同一个 ToolMessage）
        questions: 已归一化的问题列表（见 `models.normalize_ask_user_args`）
        run_id / employee_id / requestor: 护栏 6 的界面与审计半边
        timeout: 等待秒数；None 使用 `QUESTION_TIMEOUT_SECONDS`

    Yields:
        `question`（前端据此画卡片）
        `hook_notice`（通知钩子的补充提示）
        `question_resolved`（回答、跳过或超时后的收尾）
        `{"_internal": "_question_answer", "outcome": QuestionOutcome}`（最后一条）
    """
    from app.agents.tools.registry import tool_registry

    if not questions:
        yield {
            "type": "error",
            "content": "提问没有内容，已跳过。",
        }
        yield _decision(QuestionOutcome())
        return

    effective_timeout = (
        int(timeout)
        if timeout is not None
        else int(getattr(settings, "QUESTION_TIMEOUT_SECONDS", 600))
    )
    question_id = uuid.uuid4().hex[:12]
    tool_label = tool_registry.display_name_for(ASK_USER_TOOL) or "向你提问"

    # ★ 先落痕再推事件：前端看到卡片必然意味着记录已经存在
    record = question_store.open_question(
        question_id=question_id,
        thread_id=thread_id,
        call_id=call_id,
        questions=questions,
        run_id=run_id,
        employee_id=employee_id,
        requestor=requestor,
        timeout_seconds=effective_timeout,
    )

    payload: dict[str, Any] = {
        "kind": QUESTION_KIND,
        "question_id": question_id,
        "call_id": call_id,
        "thread_id": thread_id,
        "tool_name": ASK_USER_TOOL,
        "tool_label": tool_label,
        "questions": questions,
        "run_id": run_id,
        "employee_id": employee_id,
        "requestor": requestor,
        "timeout_seconds": effective_timeout,
    }

    # ★ 先注册等待槽位，再推事件（避免前端「秒回」丢单）
    question_broker.register(
        thread_id, call_id, payload, run_id=run_id, employee_id=employee_id
    )

    yield {"type": "question", **payload}

    group_events.question_required(
        thread_id=thread_id,
        call_id=call_id,
        question_id=question_id,
        questions=questions,
        run_id=run_id,
        employee_id=employee_id,
        requestor=requestor,
        timeout_seconds=effective_timeout,
    )

    # Notification 钩子在等待**之前**：它的用途是「人不在电脑前也能知道她在等」
    try:
        note_outcome = await fire(
            NOTIFICATION,
            build_payload(
                NOTIFICATION,
                message=f"{requestor or '助手'} 有问题需要你回答。",
                reason=str(questions[0].get("question") or ""),
                tool_name=ASK_USER_TOOL,
                session={
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "employee_id": employee_id,
                    "agent": requestor,
                },
            ),
            tool_name=ASK_USER_TOOL,
        )
        for notice in note_outcome.notice:
            yield {
                "type": "hook_notice",
                "content": notice,
                "event": NOTIFICATION,
                "call_id": call_id,
                "tool_name": ASK_USER_TOOL,
                "tool_label": tool_label,
            }
    except Exception as e:  # noqa: BLE001
        # 通知层永远不能把主流程打挂（不变量 18）
        logger.debug("提问通知钩子失败（已忽略）: %s", e)

    try:
        outcome = await question_broker.wait_for_answer(
            thread_id,
            call_id,
            timeout=effective_timeout,
            run_id=run_id,
            employee_id=employee_id,
        )
    except asyncio.CancelledError:
        # 客户端断连 / 应用关停：等待方被取消，但留痕不能留一条永远「等待中」。
        # 这里只结算记录，不 yield（生成器关闭期间 yield 非法），
        # 也不推 resolved —— 通知谁已经没有意义。
        question_store.settle(
            record.record_id,
            status=STATUS_SKIPPED,
            answers={},
            reason=REASON_CANCELLED,
        )
        raise

    logger.info(
        "提问结果: thread=%s call=%s qid=%s status=%s run=%s emp=%s",
        thread_id, call_id, question_id, outcome.status, run_id, employee_id,
    )

    # ★ 留痕结算的唯一写入点
    question_store.settle(
        record.record_id,
        status=outcome.status,
        answers=outcome.answers,
        reason=outcome.reason,
    )

    # ★ 超时也必须推 resolved：否则用户后来打开页面会看到一张挂着的卡片
    group_events.question_resolved(
        thread_id=thread_id,
        call_id=call_id,
        question_id=question_id,
        status=outcome.status,
        answers=outcome.answers,
        run_id=run_id,
        employee_id=employee_id,
    )

    yield {
        "type": "question_resolved",
        "question_id": question_id,
        "call_id": call_id,
        "status": outcome.status,
        "answers": outcome.answers,
        "reason": outcome.reason,
        "run_id": run_id,
        "employee_id": employee_id,
        "content": (
            "已回答"
            if outcome.status == STATUS_ANSWERED
            else ("已跳过" if outcome.skipped else "等待超时，已按跳过继续")
        ),
    }

    yield _decision(outcome)


__all__ = [
    "DECISION_KEY",
    "question_gate",
    "read_question_answer",
    "run_pending_question",
]
