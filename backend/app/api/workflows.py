# -*- coding: utf-8 -*-
"""
可视化工作流接口：定义 CRUD + 运行、轮询、取消。

★ 路由顺序：`/meta` 这类静态段必须挂在 `/{workflow_id}` 之前
否则 `/api/workflows/meta` 会被当成 workflow_id="meta" 的详情请求，
返回 404「工作流不存在」而不是编辑器要的节点/工具清单（`api/schedules.py`
的同一教训）。

★ 运行时「一次一个」的闸门在 runner.start 里
API 不重复判断 `active_run`：定时触发、助理调用、手动运行三条路都走
同一个 `runner.start`，闸门只能有一处，否则迟早有一条路绕过去。

★ 删除拒绝而不是顺带取消
删掉定义时若还有运行在跑，执行协程手里握着的是定义的内存副本 ——
记录没了它仍会继续调模型、烧 token，而用户以为「删掉就停了」。
所以先让用户取消（或等它跑完），不做「删了顺便掐掉」的静默动作。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.schemas.workflows import (
    EdgeIn,
    NodeIn,
    NodeTypeOut,
    OperatorOut,
    RunStepOut,
    ToolOptionOut,
    WorkflowCreateRequest,
    WorkflowDeleteResponse,
    WorkflowDetailResponse,
    WorkflowListResponse,
    WorkflowMetaResponse,
    WorkflowOut,
    WorkflowRunDetailResponse,
    WorkflowRunOut,
    WorkflowRunRequest,
    WorkflowRunStartResponse,
    WorkflowUpdateRequest,
)
from app.workflows import models as wm
from app.workflows import runner
from app.workflows.store import workflow_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])

#: 非法与不存在**用同一句** 404 detail（不变量 47）
_NOT_FOUND = "工作流不存在"
_RUN_NOT_FOUND = "这次运行不存在"

#: 工作流里不给选这些工具：编排类工具会再拉起一个 agent / 另一个工作流，
#: 在无人值守的运行里既绕不出去也说不清结果归属。
_EXCLUDED_TOOL_NAMES = frozenset({"run_workflow", "list_workflows"})


def _workflow_to_out(record: dict[str, Any]) -> WorkflowOut:
    data: dict[str, Any] = {
        "workflow_id": str(record.get("workflow_id") or ""),
        "name": str(record.get("name") or ""),
        "description": str(record.get("description") or ""),
        "nodes": list(record.get("nodes") or []),
        "edges": list(record.get("edges") or []),
    }
    for key in ("created_at", "updated_at", "last_run_at", "last_status"):
        value = record.get(key)
        if value:
            data[key] = value
    return WorkflowOut(**data)


def _run_to_out(run: dict[str, Any]) -> WorkflowRunOut:
    data: dict[str, Any] = {
        "run_id": str(run.get("run_id") or ""),
        "workflow_id": str(run.get("workflow_id") or ""),
        "status": str(run.get("status") or ""),
        "trigger": str(run.get("trigger") or wm.TRIGGER_MANUAL),
        "steps": [RunStepOut(**step) for step in (run.get("steps") or []) if isinstance(step, dict)],
    }
    for key in ("input", "output", "error", "current_node", "started_at", "finished_at"):
        value = run.get(key)
        if value:
            data[key] = value
    return WorkflowRunOut(**data)


def _load_or_404(workflow_id: str) -> dict[str, Any]:
    if not wm.is_valid_workflow_id(workflow_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    record = workflow_store.get(workflow_id)
    if record is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return record


def _nodes_payload(nodes: Optional[list[NodeIn]]) -> Optional[list[dict[str, Any]]]:
    return None if nodes is None else [n.model_dump() for n in nodes]


def _edges_payload(edges: Optional[list[EdgeIn]]) -> Optional[list[dict[str, Any]]]:
    return None if edges is None else [e.model_dump() for e in edges]


# ---------- 静态段（必须先于 /{workflow_id}） ----------


@router.get("/meta", response_model=WorkflowMetaResponse, response_model_exclude_none=True)
async def workflow_meta() -> WorkflowMetaResponse:
    """编辑器要的静态清单：节点类型、条件运算符、可选工具。"""
    tools: list[ToolOptionOut] = []
    try:
        from app.agents.tools.registry import tool_registry
        from app.computer_control import COMPUTER_CONTROL_SERVER

        for info in tool_registry.list_all():
            if info.name in _EXCLUDED_TOOL_NAMES:
                continue
            if getattr(info, "server", "") == COMPUTER_CONTROL_SERVER:
                continue
            tools.append(ToolOptionOut(
                name=info.name,
                display_name=info.display_name or info.name,
                description=info.description,
                category=info.category,
                danger_level=info.danger_level,
                requires_approval=info.danger_level == "danger",
            ))
    except Exception as e:  # noqa: BLE001
        logger.warning("读取工作流可选工具失败（返回空清单）: %s", e)
    tools.sort(key=lambda t: (t.category, t.name))

    return WorkflowMetaResponse(
        node_types=[NodeTypeOut(type=t, label=wm.NODE_LABELS[t]) for t in wm.NODE_TYPES],
        operators=[OperatorOut(value=op, label=wm.OPERATOR_LABELS[op]) for op in wm.CONDITION_OPERATORS],
        tools=tools,
    )


# ---------- 定义 ----------


@router.get("", response_model=WorkflowListResponse, response_model_exclude_none=True)
async def list_workflows(
    q: str = Query("", max_length=100, description="按名称/描述搜索"),
) -> WorkflowListResponse:
    if not workflow_store.enabled:
        return WorkflowListResponse(storage="unavailable", storage_error="工作流功能已关闭")
    try:
        records = workflow_store.list(query=q)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取工作流列表失败（按不可查询返回）: %s", e)
        return WorkflowListResponse(storage="unavailable", storage_error=str(e)[:200])
    return WorkflowListResponse(
        storage="ok",
        total=len(records),
        workflows=[_workflow_to_out(r) for r in records],
    )


@router.post("", response_model=WorkflowOut, response_model_exclude_none=True, status_code=201)
async def create_workflow(payload: WorkflowCreateRequest) -> WorkflowOut:
    record, errors = workflow_store.create(
        name=payload.name,
        description=payload.description,
        nodes=[n.model_dump() for n in payload.nodes],
        edges=[e.model_dump() for e in payload.edges],
    )
    if record is None:
        raise HTTPException(status_code=400, detail="；".join(errors or ["创建失败"]))
    return _workflow_to_out(record)


@router.get("/{workflow_id}", response_model=WorkflowDetailResponse, response_model_exclude_none=True)
async def get_workflow(
    workflow_id: str,
    runs_limit: int = Query(20, ge=1, le=100, description="随详情返回的运行条数"),
) -> WorkflowDetailResponse:
    record = _load_or_404(workflow_id)
    runs = workflow_store.runs_of(workflow_id, limit=runs_limit)
    return WorkflowDetailResponse(
        storage="ok",
        workflow=_workflow_to_out(record),
        runs=[_run_to_out(r) for r in runs],
        total_runs=len(workflow_store.runs_of(workflow_id, limit=10_000)),
    )


@router.put("/{workflow_id}", response_model=WorkflowOut, response_model_exclude_none=True)
async def update_workflow(workflow_id: str, payload: WorkflowUpdateRequest) -> WorkflowOut:
    _load_or_404(workflow_id)
    record, errors = workflow_store.update(
        workflow_id,
        name=payload.name,
        description=payload.description,
        nodes=_nodes_payload(payload.nodes) if "nodes" in payload.model_fields_set else None,
        edges=_edges_payload(payload.edges) if "edges" in payload.model_fields_set else None,
    )
    if record is None:
        raise HTTPException(status_code=400, detail="；".join(errors or ["更新失败"]))
    return _workflow_to_out(record)


@router.delete("/{workflow_id}", response_model=WorkflowDeleteResponse, response_model_exclude_none=True)
async def delete_workflow(workflow_id: str) -> WorkflowDeleteResponse:
    _load_or_404(workflow_id)
    if workflow_store.active_run(workflow_id) is not None:
        return WorkflowDeleteResponse(ok=False, reason="这个工作流还有一次运行没结束，先取消或等它跑完")
    return WorkflowDeleteResponse(ok=workflow_store.delete(workflow_id))


# ---------- 运行 ----------


@router.post("/{workflow_id}/run", response_model=WorkflowRunStartResponse, response_model_exclude_none=True)
async def run_workflow(workflow_id: str, payload: WorkflowRunRequest) -> WorkflowRunStartResponse:
    record = _load_or_404(workflow_id)
    run, reason = runner.start(record, payload.input, trigger=wm.TRIGGER_MANUAL)
    if run is None:
        return WorkflowRunStartResponse(ok=False, workflow_id=workflow_id, reason=reason)
    return WorkflowRunStartResponse(ok=True, workflow_id=workflow_id, run=_run_to_out(run))


@router.get(
    "/{workflow_id}/runs/{run_id}",
    response_model=WorkflowRunDetailResponse,
    response_model_exclude_none=True,
)
async def get_workflow_run(workflow_id: str, run_id: str) -> WorkflowRunDetailResponse:
    _load_or_404(workflow_id)
    run = workflow_store.get_run(workflow_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND)
    return WorkflowRunDetailResponse(storage="ok", run=_run_to_out(run))


@router.post(
    "/{workflow_id}/runs/{run_id}/cancel",
    response_model=WorkflowRunStartResponse,
    response_model_exclude_none=True,
)
async def cancel_workflow_run(workflow_id: str, run_id: str) -> WorkflowRunStartResponse:
    _load_or_404(workflow_id)
    run = workflow_store.get_run(workflow_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND)
    if not runner.cancel(workflow_id, run_id):
        return WorkflowRunStartResponse(
            ok=False, workflow_id=workflow_id, reason="这次运行已经结束或不存在",
        )
    return WorkflowRunStartResponse(ok=True, workflow_id=workflow_id, run=_run_to_out(run))
