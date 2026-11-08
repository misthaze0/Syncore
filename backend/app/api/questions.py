"""
模型主动提问的回答接口。

    图里 `ask_user` 挂起 → SSE {question} → 前端 QuestionCard
    → POST /api/questions/answer → 唤醒等待 → Command(resume=...)

★ 这里只负责唤醒等待者并结算「用户给了什么答案」
留痕的**唯一**写入点在 `questions/flow.py`（与审批留痕同一纪律）。
两边都写会得到重复事件，而重载时「后写的赢」会让记录取决于竞态。

★ 为什么回答要在接口层归一化
前端可能来自旧标签页、可能把选项文字改掉、可能塞一个不认识的 question_id。
归一化后再交给 broker，等待方拿到的永远是「合法选项的子集」；
若把原始 JSON 直接塞进 ToolMessage，模型会基于一个不存在的选项继续干活，
而且不会有任何报错。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.questions.broker import question_broker
from app.questions.models import (
    REASON_SKIPPED,
    REASON_USER,
    STATUS_ANSWERED,
    QuestionOutcome,
    normalize_question_submission,
)
from app.questions.records import question_store
from app.schemas.tools import (
    QuestionAnswerRequest,
    QuestionAnswerResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/questions", tags=["questions"])


@router.post("/answer", response_model=QuestionAnswerResponse)
async def answer_question(request: QuestionAnswerRequest) -> QuestionAnswerResponse:
    """提交用户对一张提问卡片的回答（或跳过）。"""
    pending = question_broker.get_pending(
        request.thread_id,
        request.call_id,
        run_id=request.run_id,
        employee_id=request.employee_id,
    )
    if pending is None:
        logger.warning(
            "提问回答未匹配到等待中的请求: thread=%s call=%s qid=%s run=%s emp=%s",
            request.thread_id,
            request.call_id,
            request.question_id,
            request.run_id,
            request.employee_id,
        )
        return QuestionAnswerResponse(
            success=False,
            call_id=request.call_id,
            question_id=request.question_id,
            message="未找到对应的等待中提问（可能已超时、已处理，或请求方标识不符）",
        )

    question_id = request.question_id or str(pending.get("question_id") or "")
    status, answers = normalize_question_submission(
        list(pending.get("questions") or []),
        {key: value.model_dump() for key, value in request.answers.items()},
        skipped=bool(request.skipped),
    )
    outcome = QuestionOutcome(
        status=status,
        answers=answers,
        reason=REASON_USER if status == STATUS_ANSWERED else REASON_SKIPPED,
    )

    matched = question_broker.resolve(
        request.thread_id,
        request.call_id,
        question_id,
        outcome,
        run_id=request.run_id,
        employee_id=request.employee_id,
    )
    if not matched:
        return QuestionAnswerResponse(
            success=False,
            call_id=request.call_id,
            question_id=question_id,
            message="该提问已失效（可能已超时或被另一标签页处理）",
        )

    record = question_store.find_by_call(
        request.thread_id,
        request.call_id,
        run_id=request.run_id,
        employee_id=request.employee_id,
        question_id=question_id,
    )
    return QuestionAnswerResponse(
        success=True,
        call_id=request.call_id,
        question_id=question_id,
        status=status,
        message="已回答" if status == STATUS_ANSWERED else "已跳过",
        record_id=record.record_id if record else "",
    )


@router.get("/pending")
async def list_pending_questions() -> dict:
    """查看当前所有等待中的提问（调试 / 运维用）"""
    pending = question_broker.list_pending()
    return {"total": len(pending), "pending": pending}


__all__ = ["router"]
