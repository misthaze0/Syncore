"""
审批控制面：提交当前请求的决定、查询记录与撤销临时授权。

临时授权只能从服务端当前待审批快照创建，绑定会话、请求者、工具和全部参数。
审批留痕由 approval_flow 统一结算，本模块负责受理决定和授权管理。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from app.agents.approval_broker import approval_broker
from app.schemas.tools import (
    ApprovalBatchRequest,
    ApprovalRequest,
    ApprovalResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/approval", tags=["approval"])


@router.post("", response_model=ApprovalResponse)
async def submit_approval(request: ApprovalRequest) -> ApprovalResponse:
    """
    提交人工审批结果

    - `approved=true` → 后端继续执行该危险工具
    - `approved=false` → 后端跳过执行，并把「用户拒绝」回传给 LLM

    ★ 这里**只负责唤醒等待者，不写留痕**

    一个决策只应该产生一条留痕。写入点统一在
    `agents/graph.py::get_agent_event_stream`（它同时知道
    「批准/拒绝/超时」与「工具最终有没有执行成功」）。
    两边都写会得到重复事件，而重载时"后写的那条赢"
    会让记录内容取决于竞态 —— 审计数据不能有这种不确定性。

    ★ 四元组定位（护栏 6）：`run_id` / `employee_id` 少一维就是 `success=False`
    这不是「严格」，是「不替别人签字」。群聊里 thread_id 共用，
    宽松匹配会让用户点甲的批准却放行乙的 `delete`。
    """
    from app.approval import approval_store, grant_registry

    pending = approval_broker.pending_payload(
        request.thread_id, request.call_id, run_id=request.run_id, employee_id=request.employee_id,
    )
    if pending and pending.get("expires_at", float("inf")) <= time.time():
        return ApprovalResponse(success=False, call_id=request.call_id, approved=request.approved,
                                message="该审批已超过等待期限，操作未获批准。")
    if request.request_id and (not pending or pending.get("request_id") != request.request_id):
        return ApprovalResponse(success=False, call_id=request.call_id, approved=request.approved,
                                message="该审批已过期或已被替换，请查看当前请求。")
    grant = None
    if request.scope == "same_operation":
        if not request.approved or not request.request_id or not pending or not pending.get("grant_allowed"):
            raise HTTPException(status_code=400, detail="这次操作需要逐次确认，不能创建临时授权。")
        # 授权内容只取服务器保存的待审批快照，客户端无法换工具或改参数。
        grant = grant_registry.create(
            thread_id=request.thread_id, employee_id=request.employee_id,
            tool_names=[pending["tool_name"]], args=pending.get("args", {}),
            max_uses=pending["grant_max_uses"], ttl_seconds=pending["grant_ttl_seconds"],
            note="用户允许当前会话内相同工具、相同参数的操作",
        )

    matched = approval_broker.resolve(
        request.thread_id,
        request.call_id,
        request.approved,
        run_id=request.run_id,
        employee_id=request.employee_id,
    )

    if not matched:
        if grant is not None:
            grant_registry.revoke(grant.grant_id)
        logger.warning(
            "审批未匹配到等待中的请求: thread=%s call=%s run=%s emp=%s",
            request.thread_id, request.call_id, request.run_id, request.employee_id,
        )
        return ApprovalResponse(
            success=False,
            call_id=request.call_id,
            approved=request.approved,
            message="未找到对应的等待中审批（可能已超时、已处理，或请求方标识不符）",
        )

    record = approval_store.find_by_call(
        request.thread_id,
        request.call_id,
        run_id=request.run_id,
        employee_id=request.employee_id,
    )
    return ApprovalResponse(
        success=True,
        call_id=request.call_id,
        approved=request.approved,
        message="已批准执行" if request.approved else "已拒绝执行",
        record_id=record.record_id if record else "",
        grant_id=grant.grant_id if grant else "",
    )


@router.get("/records")
async def list_records(limit: int = Query(50, ge=1, le=200), thread_id: str | None = None) -> dict:
    from app.approval import approval_store
    return {"records": [r.to_dict() for r in approval_store.list_records(limit=limit, thread_id=thread_id)],
            "stats": approval_store.stats()}


@router.get("/grants")
async def list_grants() -> dict:
    from app.approval import grant_registry
    return {"grants": grant_registry.list_active(), "stats": grant_registry.stats()}


@router.delete("/grants/{grant_id}")
async def revoke_grant(grant_id: str) -> dict:
    from app.approval import grant_registry
    if not grant_registry.revoke(grant_id):
        raise HTTPException(status_code=404, detail="授权已过期或已撤销")
    return {"ok": True}


@router.delete("/grants")
async def revoke_all_grants() -> dict:
    from app.approval import grant_registry
    grant_registry.reset()
    return {"ok": True}


@router.get("/pending")
async def list_pending_approvals() -> dict:
    """查看当前所有等待中的审批（调试 / 运维用）"""
    pending = approval_broker.list_pending()
    return {"total": len(pending), "pending": pending}


@router.post("/batch")
async def batch_approval(request: ApprovalBatchRequest) -> Dict[str, Any]:
    """
    一次性处理多个挂起的审批（**跨会话**，不是「多选后一起批」）。

    HITL 有一条硬约束（不变量 6）——一次 superstep 只消费一个工具调用，
    同一轮的多个危险调用从来不会同时处于等待状态。所以这条接口服务的是
    多个浏览器标签 / 并发请求各自挂起了一个审批的场景。

    每条独立处理，互不影响：某一条早已超时不会让其余条目失败，
    而是各自返回 `matched=false`。
    """
    results: List[Dict[str, Any]] = []
    approved_count = 0
    skipped = 0

    for item in request.items:
        matched = approval_broker.resolve(
            item.thread_id, item.call_id, item.approved,
            run_id=item.run_id, employee_id=item.employee_id,
        )
        if matched:
            approved_count += 1 if item.approved else 0
        else:
            skipped += 1
        results.append({
            "thread_id": item.thread_id,
            "call_id": item.call_id,
            "run_id": item.run_id,
            "employee_id": item.employee_id,
            "approved": item.approved,
            "matched": matched,
        })

    summary = {
        "total": len(request.items),
        "matched": len(request.items) - skipped,
        "approved": approved_count,
        "rejected": (len(request.items) - skipped) - approved_count,
        "skipped": skipped,
    }
    logger.info("批量审批: %s", summary)

    if skipped:
        logger.warning(
            "批量审批有 %s 条未匹配到等待中的请求（多半是已超时）：%s",
            skipped, [r["call_id"] for r in results if not r["matched"]],
        )

    payload: Dict[str, Any] = {"ok": True, "summary": summary}
    if request.verbose:
        payload["results"] = results
    return payload
