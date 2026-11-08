# -*- coding: utf-8 -*-
"""
定时任务接口。

★ 路由顺序（照抄 `api/hooks.py` 的教训）：静态段先于 `/{schedule_id}`
`/api/schedules/status` 如果挂在 `/{schedule_id}` 之后，会被当成
schedule_id="status" 的详情请求 —— 返回 404「任务不存在」而不是调度器状态。

★ `storage` / 统一 404 / `response_model_exclude_none` 三条铁律与 tasks 同源：
- 查不了必须说「查不了」，不许谎报「没有任务」；
- 非法 id 与不存在用同一句 detail（不变量 47，路径可探测性也是攻击面）；
- Pydantic v2 里 exclude_none 必须**逐端点显式传**（model_config 对
  FastAPI 的 response_model 不生效，`api/tasks.py` 有实测记录）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.schemas.schedules import (
    EnabledRequest,
    RunOut,
    ScheduleCreateRequest,
    ScheduleDetailResponse,
    ScheduleListResponse,
    ScheduleOut,
    ScheduleRunListResponse,
    ScheduleRunNowResponse,
    ScheduleStatusResponse,
    ScheduleUpdateRequest,
)
from app.schedules import models as sm
from app.schedules import scheduler as sched
from app.schedules.store import schedule_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedules", tags=["schedules"])

#: 非法与不存在**用同一句** 404 detail（不变量 47）
_NOT_FOUND = "定时任务不存在"


def _schedule_to_out(record: dict[str, Any]) -> ScheduleOut:
    data: dict[str, Any] = {
        "schedule_id": str(record.get("schedule_id") or ""),
        "title": str(record.get("title") or ""),
        "kind": str(record.get("kind") or sm.KIND_CHAT),
        "instruction": str(record.get("instruction") or ""),
        "freq": dict(record.get("freq") or {}),
        "freq_text": sm.describe_freq(record.get("freq") or {}),
        "enabled": bool(record.get("enabled")),
    }
    for key in ("workflow_id", "start_date", "end_date", "created_at", "updated_at", "agent",
                "model", "permission_mode", "workdir_id", "next_run_at", "last_run_at",
                "last_status", "last_thread_id", "last_task_id"):
        value = record.get(key)
        if value:  # ★ 假值一律不带
            data[key] = value
    for key in ("web_enabled", "graph_enabled"):
        value = record.get(key)
        if isinstance(value, bool):
            data[key] = value
    return ScheduleOut(**data)


def _run_to_out(run: dict[str, Any], *, schedule_title: Optional[str] = None) -> RunOut:
    data: dict[str, Any] = {
        "run_id": str(run.get("run_id") or ""),
        "schedule_id": str(run.get("schedule_id") or ""),
        "trigger": str(run.get("trigger") or sm.TRIGGER_SCHEDULED),
        "status": str(run.get("status") or ""),
    }
    for key in ("thread_id", "task_id", "workflow_run_id", "started_at", "finished_at",
                "result", "error", "files", "usage"):
        value = run.get(key)
        if value:
            data[key] = value
    if schedule_title:
        data["schedule_title"] = schedule_title
    return RunOut(**data)


def _load_or_404(schedule_id: str) -> dict[str, Any]:
    if not sm.is_valid_schedule_id(schedule_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    record = schedule_store.get(schedule_id)
    if record is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return record


def _validate_runtime_fields(payload: dict[str, Any]) -> None:
    """跨模块的语义校验集中在这一个口子（400 而不是静默丢弃）。"""
    kind = str(payload.get("kind") or sm.KIND_CHAT)
    if kind not in sm.SCHEDULE_KINDS:
        raise HTTPException(status_code=400, detail=f"不支持的任务类型: {kind}")
    if kind == sm.KIND_WORKFLOW:
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if not workflow_id:
            raise HTTPException(status_code=400, detail="请选择要运行的工作流")
        from app.workflows.store import workflow_store

        if workflow_store.get(workflow_id) is None:
            raise HTTPException(status_code=400, detail="选择的工作流不存在，请重新选择")
    mode = str(payload.get("permission_mode") or "")
    if mode:
        from app.permissions.modes import MODE_ORDER

        if mode not in MODE_ORDER:
            raise HTTPException(status_code=400, detail=f"权限模式不存在: {mode}")
    workdir_id = str(payload.get("workdir_id") or "")
    if workdir_id:
        from app.workdir.store import get_workdir

        if get_workdir(workdir_id) is None:
            raise HTTPException(status_code=400, detail="绑定的工作区不存在，请先在「工作区」里授权该目录")


# ---------- 静态段（必须先于 /{schedule_id} 注册，见模块注释） ----------


@router.get("/status", response_model=ScheduleStatusResponse, response_model_exclude_none=True)
async def schedule_status() -> ScheduleStatusResponse:
    """调度循环健康摘要 —— 「它坏了」必须可见（watcher 的同一纪律）。"""
    return ScheduleStatusResponse(
        scheduler=sched.scheduler_status(),
        storage="ok" if schedule_store.enabled else "unavailable",
        total=len(schedule_store.list()),
        enabled_count=len(schedule_store.list(enabled=True)),
    )


@router.get("/runs", response_model=ScheduleRunListResponse, response_model_exclude_none=True)
async def list_all_runs(
    limit: int = Query(50, ge=1, le=200, description="跨任务的全量运行条数上限"),
) -> ScheduleRunListResponse:
    """跨任务「运行记录」页签（按开始时间倒序，每条带任务名）。"""
    if not schedule_store.enabled:
        return ScheduleRunListResponse(storage="unavailable", storage_error="定时任务功能已关闭")
    try:
        pairs = schedule_store.all_runs(limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取运行记录失败（按不可查询返回）: %s", e)
        return ScheduleRunListResponse(storage="unavailable", storage_error=str(e)[:200])
    return ScheduleRunListResponse(
        storage="ok",
        total=len(pairs),
        runs=[_run_to_out(run, schedule_title=str(s.get("title") or "")) for s, run in pairs],
    )


# ---------- 列表与详情 ----------


@router.get("", response_model=ScheduleListResponse, response_model_exclude_none=True)
async def list_schedules(
    enabled: Optional[bool] = Query(default=None, description="true=已启用 / false=已暂停 / 缺省=全部"),
    q: str = Query("", max_length=100, description="按名称/指令子串搜索"),
) -> ScheduleListResponse:
    if not schedule_store.enabled:
        return ScheduleListResponse(
            storage="unavailable", storage_error="定时任务功能已关闭",
            scheduler=sched.scheduler_status(),
        )
    try:
        records = schedule_store.list(enabled=enabled, query=q)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取定时任务列表失败（按不可查询返回）: %s", e)
        return ScheduleListResponse(
            storage="unavailable", storage_error=str(e)[:200],
            scheduler=sched.scheduler_status(),
        )
    return ScheduleListResponse(
        storage="ok",
        total=len(records),
        schedules=[_schedule_to_out(r) for r in records],
        scheduler=sched.scheduler_status(),
    )


@router.post("", response_model=ScheduleOut, response_model_exclude_none=True, status_code=201)
async def create_schedule(payload: ScheduleCreateRequest) -> ScheduleOut:
    fields: dict[str, Any] = {
        "title": payload.title,
        "kind": payload.kind,
        "workflow_id": payload.workflow_id or "",
        "instruction": payload.instruction,
        "freq": payload.freq.to_freq(),
        "start_date": payload.start_date or "",
        "end_date": payload.end_date or "",
        "enabled": payload.enabled,
        "agent": payload.agent or "",
        "model": payload.model or "",
        "web_enabled": payload.web_enabled,
        "graph_enabled": payload.graph_enabled,
        "permission_mode": payload.permission_mode or "",
        "workdir_id": payload.workdir_id or "",
    }
    _validate_runtime_fields(fields)
    record, err = schedule_store.create(**fields)
    if record is None:
        raise HTTPException(status_code=400, detail=err or "创建失败")
    return _schedule_to_out(record)


@router.get("/{schedule_id}", response_model=ScheduleDetailResponse, response_model_exclude_none=True)
async def get_schedule(
    schedule_id: str,
    runs_limit: int = Query(20, ge=1, le=100, description="随详情返回的运行条数"),
) -> ScheduleDetailResponse:
    record = _load_or_404(schedule_id)
    runs = schedule_store.runs_of(schedule_id, limit=runs_limit)
    return ScheduleDetailResponse(
        storage="ok",
        schedule=_schedule_to_out(record),
        runs=[_run_to_out(r) for r in runs],
        total_runs=len(schedule_store.runs_of(schedule_id, limit=10_000)),
    )


@router.put("/{schedule_id}", response_model=ScheduleOut, response_model_exclude_none=True)
async def update_schedule(schedule_id: str, payload: ScheduleUpdateRequest) -> ScheduleOut:
    current = _load_or_404(schedule_id)
    raw = payload.model_dump(exclude_unset=True)
    if "freq" in raw and raw["freq"] is not None:
        raw["freq"] = payload.freq.to_freq() if payload.freq else None
    # 只改 kind 而不带 workflow_id 时也要校验「合并之后」的绑定是否真实存在
    _validate_runtime_fields({**current, **raw})
    record, err = schedule_store.update(schedule_id, **raw)
    if record is None:
        raise HTTPException(status_code=400, detail=err or "更新失败")
    return _schedule_to_out(record)


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str) -> dict[str, Any]:
    _load_or_404(schedule_id)
    return {"ok": schedule_store.delete(schedule_id)}


@router.put("/{schedule_id}/enabled", response_model=ScheduleOut, response_model_exclude_none=True)
async def set_enabled(schedule_id: str, payload: EnabledRequest) -> ScheduleOut:
    _load_or_404(schedule_id)
    record, err = schedule_store.set_enabled(schedule_id, payload.enabled)
    if record is None:
        raise HTTPException(status_code=400, detail=err or "更新失败")
    return _schedule_to_out(record)


@router.post("/{schedule_id}/run-now", response_model=ScheduleRunNowResponse, response_model_exclude_none=True)
async def run_now(schedule_id: str) -> ScheduleRunNowResponse:
    """立即运行一次。`ok=false` 是业务态（上一次还没跑完 / 功能降级），不是错误。"""
    record = _load_or_404(schedule_id)
    from app.schedules import runner

    if schedule_store.active_run(schedule_id) is not None:
        return ScheduleRunNowResponse(
            ok=False, schedule_id=schedule_id, reason="上一次运行还没结束",
        )
    run = await runner.run_now(record)
    if run is None:
        return ScheduleRunNowResponse(
            ok=False, schedule_id=schedule_id,
            reason="未能派发（后台任务能力不可用，或指令为空）",
        )
    return ScheduleRunNowResponse(ok=True, schedule_id=schedule_id, run=_run_to_out(run))


@router.post("/{schedule_id}/runs/{run_id}/cancel", response_model=ScheduleRunNowResponse, response_model_exclude_none=True)
async def cancel_run(schedule_id: str, run_id: str) -> ScheduleRunNowResponse:
    """取消一次在跑的运行。真取消：掐子任务协程（executor.abort），
    运行记录由 finalizer 观察到 cancelled 终态后如实结算 —— 这里不抢写。"""
    record = _load_or_404(schedule_id)
    run = schedule_store.get_run(schedule_id, run_id)
    if run is None or str(run.get("status")) != sm.RUN_RUNNING:
        return ScheduleRunNowResponse(ok=False, schedule_id=schedule_id, reason="这次运行已经结束或不存在")
    if str(record.get("kind") or sm.KIND_CHAT) == sm.KIND_WORKFLOW:
        wf_run_id = str(run.get("workflow_run_id") or "")
        if not wf_run_id:
            return ScheduleRunNowResponse(
                ok=False, schedule_id=schedule_id, reason="这次运行没有可取消的工作流",
            )
        from app.workflows import runner as workflow_runner

        if not workflow_runner.cancel(str(record.get("workflow_id") or ""), wf_run_id):
            return ScheduleRunNowResponse(
                ok=False, schedule_id=schedule_id, reason="这次运行已经结束或无法取消",
            )
        return ScheduleRunNowResponse(ok=True, schedule_id=schedule_id, run=_run_to_out(dict(run)))

    task_id = str(run.get("task_id") or "")
    if not task_id:
        return ScheduleRunNowResponse(ok=False, schedule_id=schedule_id, reason="这次运行没有可取消的任务")
    from app.tasks.executor import executor
    from app.tasks.store import task_store

    task_store.cancel(task_id)
    executor.abort(task_id)
    return ScheduleRunNowResponse(ok=True, schedule_id=schedule_id, run=_run_to_out(dict(run)))
