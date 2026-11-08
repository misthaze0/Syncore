# -*- coding: utf-8 -*-
"""定时任务的接口数据模型。

★ 存储层 / 接口层两套形状（与 `schemas/tasks.py` 同一理由）
存储跟着落盘格式变，接口跟着前端变。这里比 tasks 多带一个 `instruction`
（编辑弹窗要能看回原文），多派生一个 `freq_text`（人话调度描述由后端
`describe_freq` 统一产出 —— 中文语序规则放前端就会两处漂移）。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class FreqIn(BaseModel):
    """调度规格（入参）。语义校验在后端 `validate_freq`，这里只管形状。"""

    kind: Literal["daily", "weekly", "monthly", "interval", "cron"]
    time: Optional[str] = Field(default=None, description="HH:MM（cron 之外的所有类型都要）")
    days: Optional[list[int]] = Field(default=None, description="weekly：0=周一 … 6=周日")
    day: Optional[int] = Field(default=None, description="monthly：1~31，当月没有则顺延到月末")
    every_days: Optional[int] = Field(default=None, description="interval：每 N 天")
    expr: Optional[str] = Field(default=None, description="cron：五分字段")

    def to_freq(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        for key in ("time", "days", "day", "every_days", "expr"):
            value = getattr(self, key)
            if value is not None and value != []:
                out[key] = value
        return out


class ScheduleCreateRequest(BaseModel):
    title: str = Field(max_length=80)
    #: `chat` = 派给专员的对话任务；`workflow` = 运行一个画好的工作流
    kind: Literal["chat", "workflow"] = "chat"
    workflow_id: Optional[str] = Field(default=None, description="kind=workflow 时必填")
    instruction: str = Field(
        default="", max_length=8000,
        description="kind=chat 时是喂给专员的完整指令；kind=workflow 时是工作流输入（可空）",
    )
    freq: FreqIn
    start_date: Optional[str] = Field(default=None, description="YYYY-MM-DD，含当天；空=不限")
    end_date: Optional[str] = Field(default=None, description="YYYY-MM-DD，含当天；空=不限")
    enabled: bool = True
    agent: Optional[str] = Field(default=None, description="能力名，空=兜底专员")
    model: Optional[str] = Field(default=None, description="空=跟随全局默认模型")
    web_enabled: bool = True
    graph_enabled: bool = True
    permission_mode: Optional[str] = Field(default=None, description="空=按当前全局默认档")
    workdir_id: Optional[str] = Field(default=None, description="绑定的授权工作区；空=沙箱默认目录")


class ScheduleUpdateRequest(BaseModel):
    """局部更新：只带出现的键。"""

    title: Optional[str] = Field(default=None, max_length=80)
    kind: Optional[Literal["chat", "workflow"]] = None
    workflow_id: Optional[str] = None
    instruction: Optional[str] = Field(default=None, max_length=8000)
    freq: Optional[FreqIn] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    enabled: Optional[bool] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    web_enabled: Optional[bool] = None
    graph_enabled: Optional[bool] = None
    permission_mode: Optional[str] = None
    workdir_id: Optional[str] = None


class EnabledRequest(BaseModel):
    enabled: bool


class RunOut(BaseModel):
    """一次运行的对外形状。"""

    run_id: str
    schedule_id: str
    #: ★ 本次运行的会话 id：文件下载（`/api/conversations/{tid}/files/*`）
    #: 与「去会话里看全文」都靠它。
    thread_id: Optional[str] = None
    task_id: Optional[str] = None
    #: kind=workflow 时对应的 `workflows` 运行 id（到「工作流」页看步骤详情）
    workflow_run_id: Optional[str] = None
    trigger: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    files: Optional[list[dict[str, str]]] = None
    usage: Optional[dict[str, int]] = None
    #: 运行归属的任务名（全局「运行记录」页签用；单任务详情不必带）
    schedule_title: Optional[str] = None


class ScheduleOut(BaseModel):
    schedule_id: str
    title: str
    kind: str = "chat"
    workflow_id: Optional[str] = None
    instruction: str
    freq: dict[str, Any]
    #: 人话调度描述（「每天 09:00」），由后端派生
    freq_text: str = ""
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    enabled: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    web_enabled: Optional[bool] = None
    graph_enabled: Optional[bool] = None
    permission_mode: Optional[str] = None
    workdir_id: Optional[str] = None
    #: 下一次执行时间。缺省 = 停用或日期窗已尽 —— 界面显示「暂无后续执行」，
    #: 这与「还没算出来」在数据上同形，但两者在界面上是同一句话，不必伪造区分。
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    last_thread_id: Optional[str] = None
    last_task_id: Optional[str] = None


class ScheduleListResponse(BaseModel):
    """
    ★ `storage` 语义与 tasks 完全一致：查不了 ≠ 没有。
    「谎报没有任务」会让用户在以为一切正常的日子里错过所有提醒。
    """

    storage: Literal["ok", "unavailable"] = "ok"
    total: int = 0
    schedules: list[ScheduleOut] = Field(default_factory=list)
    storage_error: Optional[str] = None
    scheduler: dict[str, Any] = Field(default_factory=dict, description="调度循环健康摘要")


class ScheduleRunListResponse(BaseModel):
    storage: Literal["ok", "unavailable"] = "ok"
    total: int = 0
    runs: list[RunOut] = Field(default_factory=list)
    storage_error: Optional[str] = None


class ScheduleDetailResponse(BaseModel):
    storage: Literal["ok", "unavailable"] = "ok"
    schedule: Optional[ScheduleOut] = None
    runs: list[RunOut] = Field(default_factory=list)
    total_runs: int = 0
    storage_error: Optional[str] = None


class ScheduleRunNowResponse(BaseModel):
    """
    ★ 为什么 `ok=false` 不走 404/500：
    「上一次还没跑完」是正常业务态，不是错误。报 404 会让界面显示
    「任务不存在」这种误导文案（与 TaskCancelResponse 同一取舍）。
    """

    ok: bool
    reason: Optional[str] = None
    schedule_id: str
    run: Optional[RunOut] = None


class ScheduleStatusResponse(BaseModel):
    scheduler: dict[str, Any] = Field(default_factory=dict)
    storage: Literal["ok", "unavailable"] = "ok"
    total: int = 0
    enabled_count: int = 0
