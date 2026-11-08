# -*- coding: utf-8 -*-
"""
工作流工具 —— 让助理在对话里列出并运行用户画好的工作流。

★ 与手动/定时触发走同一个 runner

三条入口（页面点运行、定时到点、助理调用）共用 `runner.start`，
并发闸门、落盘、取消、工具权限全在同一处。助理这条只是多了一层
「按名字找到工作流」和「等它跑完把结果读回来」。

★ 为什么 run 要等

用户说「帮我跑一下晨报工作流」时，他要的是**结果**，不是一句
「已经启动了」。工具等一个上限（`_WAIT_SECONDS`）：跑完就回答案，
等不到就如实说还在跑 —— 不编造结果，也不无限期占住对话。

★ 工具节点仍然不能无人值守地做危险操作

工作流里的工具节点没有审批弹窗（见 `workflows/engine.py`），
所以助理运行工作流时**不会**顺手把 danger 工具放行。
"""

from __future__ import annotations

import logging

from app.agents.tools.context import ToolContext
from app.agents.tools.decorator import agent_tool
from app.workflows import models as wm
from app.workflows import runner
from app.workflows.store import workflow_store

logger = logging.getLogger(__name__)

#: 助理等待工作流结束的上限（秒）。跑不完就先把控制权还给对话。
_WAIT_SECONDS = 120.0


def _health() -> bool:
    try:
        workflow_store.list()
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("工作流存储健康检查失败: %s", e)
        return False


def _resolve(needle: str) -> tuple[dict | None, str]:
    """按 id 或名称找唯一的工作流；返回 (记录, 没找到时的说明)。"""
    needle = (needle or "").strip()
    if not needle:
        return None, "请给出工作流名称或 id（先用 list_workflows 看有哪些）。"
    record = workflow_store.get(needle)
    if record is not None:
        return record, ""

    records = workflow_store.list()
    exact = [r for r in records if str(r.get("name") or "").strip().casefold() == needle.casefold()]
    candidates = exact or [
        r for r in records
        if needle.casefold() in str(r.get("name") or "").casefold()
    ]
    if len(candidates) == 1:
        return candidates[0], ""
    if not candidates:
        return None, f"没有找到叫「{needle}」的工作流。先用 list_workflows 看看有哪些。"
    names = "、".join(f"{r.get('name')}（id={r.get('workflow_id')}）" for r in candidates[:8])
    return None, f"「{needle}」匹配到多个工作流：{names}。请用 id 指定其中一个。"


@agent_tool(
    display_name="查看工作流",
    description=(
        "列出用户画好的工作流（名称、id、最近一次运行状态）。"
        "用户问「我有哪些工作流」「晨报那个跑得怎么样」时调用。"
    ),
    category="编排",
    danger_level="safe",
    read_only=True,
    allowed_agents=["supervisor"],
    health_handler=_health,
)
def list_workflows() -> str:
    if not workflow_store.enabled:
        return "（工作流功能已关闭）"
    try:
        records = workflow_store.list()
    except Exception as e:  # noqa: BLE001
        return f"（工作流列表暂时查不到：{e}）"
    if not records:
        return "还没有工作流。可以在侧栏「工作流」里画一个。"

    lines = [f"共 {len(records)} 个工作流："]
    for record in records:
        line = f"- {record.get('name')}（id={record.get('workflow_id')}）"
        status = str(record.get("last_status") or "")
        if status:
            line += f" · 最近一次：{_status_text(status)}"
        if record.get("description"):
            line += f" · {record.get('description')}"
        lines.append(line)
    return "\n".join(lines)


@agent_tool(
    display_name="运行工作流",
    description=(
        "运行一个用户画好的工作流，并等待它跑完（最多两分钟）后把结果读回来。"
        "workflow 传 list_workflows 给出的 id 或唯一名称；input 会作为这次运行的输入。"
        "用户说「跑一下 XX 工作流」时调用。"
    ),
    category="编排",
    danger_level="moderate",
    allowed_agents=["supervisor"],
    health_handler=_health,
)
async def run_workflow(workflow: str, input: str = "", ctx: ToolContext | None = None) -> str:
    """运行一个工作流并返回结果。

    Args:
        workflow: 工作流的名称或 id（先用 list_workflows 查）
        input: 传给这次运行的输入文本，工作流里用 {{input}} 引用；没有输入可留空
    """
    if not workflow_store.enabled:
        return "（工作流功能已关闭）"
    record, problem = _resolve(workflow)
    if record is None:
        return problem

    run, reason = runner.start(
        record,
        input or "",
        trigger=wm.TRIGGER_ASSISTANT,
    )
    if run is None:
        return f"没能运行「{record.get('name')}」：{reason}"

    finished = await runner.wait_for(str(run["run_id"]), _WAIT_SECONDS)
    if finished is None:
        return f"「{record.get('name')}」已启动，但运行记录读不到（存储异常）。"
    status = str(finished.get("status") or "")
    name = str(record.get("name") or "工作流")
    if status == wm.RUN_SUCCEEDED:
        output = str(finished.get("output") or "").strip()
        return f"「{name}」运行完成：\n{output or '（没有产生回答内容）'}"
    if status == wm.RUN_FAILED:
        return f"「{name}」运行失败：{finished.get('error') or '未知原因'}"
    if status == wm.RUN_CANCELLED:
        return f"「{name}」的运行已取消。"
    return (
        f"「{name}」还在运行（id={run['run_id']}）。"
        "可以到侧栏「工作流」里看进度和结果。"
    )


_STATUS_TEXT = {
    wm.RUN_RUNNING: "进行中",
    wm.RUN_SUCCEEDED: "成功",
    wm.RUN_FAILED: "失败",
    wm.RUN_CANCELLED: "已取消",
    wm.RUN_ORPHANED: "服务重启时中断",
}


def _status_text(status: str) -> str:
    return _STATUS_TEXT.get(status, status)


__all__ = ["list_workflows", "run_workflow"]
