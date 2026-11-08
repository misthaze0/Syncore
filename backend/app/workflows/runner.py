# -*- coding: utf-8 -*-
"""
工作流的后台运行器 —— 把执行引擎包成「一次可观察、可取消、会落盘的运行」。

★ 为什么运行要活得比 HTTP 请求久

手动运行是「点一下，然后看画布上的节点依次亮起来」。如果用请求本身驱动执行，
用户一刷新页面、一关标签，运行就断了 —— 而模型调用可能已经花掉 token，
却留下一条永远 running 的记录。所以运行一律 `asyncio.create_task` 出去，
请求只负责**启动**和**读状态**（与 `tasks/executor.py` 同一姿态）。

★ 每一步都写盘，而不是只在结束时写

前端靠轮询运行记录来点亮节点。只在结束时落盘的话，跑 40 秒的工作流
在前 39 秒里看起来「什么都没发生」—— 用户会以为按钮没生效。
每完成一步 patch 一次（同步写，单用户桌面足够快），刷新页面也能接着看。

★ 一个工作流同时只允许一次运行（默认）

并发跑同一个工作流会两头烧 token，而单用户桌面上「同时点两次」多半是误触。
需要并行的场景（不同输入各跑一遍）在第一版里不存在；调用方显式传
`allow_concurrent=True` 才放开。**闸门在跑之前判**，不是跑完再拒。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from app.workflows.engine import WorkflowRunError, execute_graph
from app.workflows.models import (
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_OUTPUT_CHARS,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    TRIGGER_MANUAL,
    WorkflowRecord,
    WorkflowRun,
    cap_output,
)
from app.workflows.store import workflow_store

logger = logging.getLogger(__name__)

#: 取消时写进记录的原因（用户看得到）
CANCELLED_REASON = "运行已取消"


@dataclass
class _ActiveRun:
    workflow_id: str
    run_id: str
    task: asyncio.Task


#: run_id → 运行中的协程。**强引用集合**：asyncio 只持弱引用（同不变量 28）。
_active: dict[str, _ActiveRun] = {}


def start(
    workflow: WorkflowRecord,
    input_text: str,
    *,
    trigger: str = TRIGGER_MANUAL,
    allow_concurrent: bool = False,
) -> tuple[Optional[WorkflowRun], str]:
    """启动一次后台运行。返回 (运行记录, 没跑起来的原因)。

    原因只在不跑时非空 —— 调用方据此区分「启动成功」与「被闸门/降级挡下」。
    """
    wid = str(workflow.get("workflow_id") or "")
    if not wid:
        return None, "工作流不存在"
    if not workflow_store.enabled:
        return None, "工作流功能已关闭"
    if not allow_concurrent:
        active = workflow_store.active_run(wid)
        if active is not None:
            return None, "这个工作流还有一次运行没结束，等它跑完再试"

    run = workflow_store.start_run(
        workflow_id=wid, input_text=input_text, trigger=trigger,
    )
    if run is None:
        return None, "无法启动运行（工作流不存在或运行记录写不进去）"

    task = asyncio.create_task(_drive(workflow, run), name=f"xjms-workflow-{run['run_id']}")
    entry = _ActiveRun(workflow_id=wid, run_id=run["run_id"], task=task)
    _active[run["run_id"]] = entry
    task.add_done_callback(lambda _t, rid=run["run_id"]: _active.pop(rid, None))
    return run, ""


async def _drive(workflow: WorkflowRecord, run: WorkflowRun) -> None:
    wid = str(run.get("workflow_id") or "")
    rid = str(run.get("run_id") or "")
    steps: list[dict] = []

    def on_step(step: dict) -> None:
        steps.append(step)
        try:
            workflow_store.patch_run(
                wid, rid,
                steps=list(steps),
                current_node=str(step.get("node_id") or ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("工作流进度落盘失败（执行继续）: %s", e)

    try:
        output, _ = await execute_graph(
            workflow, str(run.get("input") or ""), run_id=rid, on_step=on_step,
        )
        workflow_store.patch_run(
            wid, rid,
            status=RUN_SUCCEEDED,
            output=cap_output(output, RUN_OUTPUT_CHARS),
            steps=steps,
            current_node="",
        )
    except asyncio.CancelledError:
        try:
            workflow_store.patch_run(
                wid, rid,
                status=RUN_CANCELLED,
                error=CANCELLED_REASON,
                steps=steps,
                current_node="",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("取消结算失败: %s", e)
        raise
    except WorkflowRunError as e:
        _settle_failed(wid, rid, steps, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("工作流运行异常: %s/%s", wid, rid)
        _settle_failed(wid, rid, steps, f"运行出错：{e}")


def _settle_failed(wid: str, rid: str, steps: list[dict], message: str) -> None:
    try:
        workflow_store.patch_run(
            wid, rid,
            status=RUN_FAILED,
            error=message[:1000],
            steps=steps,
            current_node="",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("工作流失败结算失败: %s", e)


def cancel(workflow_id: str, run_id: str) -> bool:
    """取消一次在跑的运行。返回是否真的发出了取消。

    终态记录由 `_drive` 的 CancelledError 分支如实结算 —— 这里不抢写，
    否则「取消了」会先于执行真正停下出现在记录里。
    """
    entry = _active.get(run_id)
    if entry is None or entry.workflow_id != workflow_id:
        return False
    run = workflow_store.get_run(workflow_id, run_id)
    if run is None or str(run.get("status")) != RUN_RUNNING:
        return False
    entry.task.cancel()
    return True


async def wait_for(run_id: str, timeout_seconds: float) -> Optional[WorkflowRun]:
    """等一次运行到终态；超时返回它此刻的记录（仍是 running），不取消运行。

    助理工具用它：能等就给出答案，等不到就如实说「还在跑」。
    """
    entry = _active.get(run_id)
    if entry is None:
        return None
    try:
        await asyncio.wait_for(asyncio.shield(entry.task), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        # 不是等的人被取消，而是运行本身被取消 —— wait_for 会把
        # 子任务的 CancelledError 抛过来，那不是「等待失败」。
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("等待工作流结束失败: %s", e)
    return workflow_store.get_run(entry.workflow_id, run_id)


def active_ids() -> dict[str, str]:
    """run_id → workflow_id（运维/测试用）"""
    return {rid: entry.workflow_id for rid, entry in _active.items()}


__all__ = [
    "CANCELLED_REASON",
    "active_ids",
    "cancel",
    "start",
    "wait_for",
]
