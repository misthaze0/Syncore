# -*- coding: utf-8 -*-
"""
可视化工作流的接口数据模型。

★ 存储层与接口层分开

`app/workflows/models.py` 的 TypedDict 是落盘形状（`source_handle` 这类
画布字段原样保留）；这里的 Pydantic 模型是给前端的契约。两者分离的理由与
`schemas/tasks.py` 相同：落盘格式跟着执行引擎变，接口形状跟着画布变。

★ 可选字段一律 `Optional[...] = None`

缺省 = 「没有这件事」，`null` = 「这件事的值是空的」—— 后者在本项目里
基本不存在。路由上必须逐端点传 `response_model_exclude_none=True`
（Pydantic v2 的 model_config 对 FastAPI 响应序列化不生效，见 `api/tasks.py`）。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class NodeIn(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    type: str
    title: str = ""
    x: float = 0
    y: float = 0
    config: dict[str, Any] = Field(default_factory=dict)


class EdgeIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    source: str
    target: str
    source_handle: str = ""


class WorkflowCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    nodes: list[NodeIn] = Field(default_factory=list)
    edges: list[EdgeIn] = Field(default_factory=list)


class WorkflowUpdateRequest(BaseModel):
    """只改传了的字段 —— `model_dump(exclude_unset=True)` 是这里的约定。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    nodes: Optional[list[NodeIn]] = None
    edges: Optional[list[EdgeIn]] = None


class WorkflowOut(BaseModel):
    workflow_id: str
    name: str
    description: str = ""
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None

    model_config = {"exclude_none": True}


class WorkflowListResponse(BaseModel):
    storage: str = Field(description="ok / unavailable")
    total: int = 0
    workflows: list[WorkflowOut] = Field(default_factory=list)
    storage_error: Optional[str] = None

    model_config = {"exclude_none": True}


class RunStepOut(BaseModel):
    node_id: str
    title: str = ""
    type: str = ""
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    branch: Optional[str] = None

    model_config = {"exclude_none": True}


class WorkflowRunOut(BaseModel):
    run_id: str
    workflow_id: str
    status: str
    trigger: str = "manual"
    input: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    current_node: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    steps: list[RunStepOut] = Field(default_factory=list)

    model_config = {"exclude_none": True}


class WorkflowDetailResponse(BaseModel):
    storage: str = "ok"
    workflow: WorkflowOut
    runs: list[WorkflowRunOut] = Field(default_factory=list)
    total_runs: int = 0
    storage_error: Optional[str] = None

    model_config = {"exclude_none": True}


class WorkflowRunDetailResponse(BaseModel):
    storage: str = "ok"
    run: WorkflowRunOut
    storage_error: Optional[str] = None

    model_config = {"exclude_none": True}


class WorkflowRunRequest(BaseModel):
    input: str = Field(default="", max_length=8000)


class WorkflowRunStartResponse(BaseModel):
    """`ok=false` 是业务态（上一次还在跑 / 功能降级），不是 HTTP 错误。"""

    ok: bool
    workflow_id: str
    run: Optional[WorkflowRunOut] = None
    reason: Optional[str] = None

    model_config = {"exclude_none": True}


class WorkflowDeleteResponse(BaseModel):
    ok: bool
    reason: Optional[str] = None

    model_config = {"exclude_none": True}


class NodeTypeOut(BaseModel):
    type: str
    label: str


class OperatorOut(BaseModel):
    value: str
    label: str


class ToolOptionOut(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    category: str = ""
    danger_level: str = "safe"
    #: 需要人工批准的工具在无人值守的运行里会被拒绝 —— 画布上要能提前看出来
    requires_approval: bool = False

    model_config = {"exclude_none": True}


class WorkflowMetaResponse(BaseModel):
    node_types: list[NodeTypeOut]
    operators: list[OperatorOut]
    tools: list[ToolOptionOut]
    max_steps: int = 200

    model_config = {"exclude_none": True}
