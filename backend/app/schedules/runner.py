# -*- coding: utf-8 -*-
"""
一次定时触发的执行桥 —— 复用后台子任务执行器，不重写第二套。

★ 为什么定时任务不自己驱动子图

`tasks/executor.py` 的存在理由（模块注释写了整个文件）就是：
「一次 agent 执行要活得比产生它的连接久」。定时任务连"产生它的连接"都没有，
它是这套能力最纯粹的消费者：审批闸门、并发信号量、先落盘再执行、
重启对账、真取消，全部现成。另起一条执行路径等于把这些坑再踩一遍。

★ 运行完成靠**轮询子任务终态**，而不是给 executor 加回调

回调要侵入 `executor._run` 的三条收尾路径（正常/失败/取消）各插一刀 ——
而那三条路径收敛在 `finally` 里这件事，正是它自己的设计。
每 5 秒读一次 `task_store.get()`（内存索引，零 IO）换来执行器一行不改。
进程重启时 finalizer 协程会死，运行记录停在 running ——
这一角由 `sweep_on_startup()` 在对账时补平（与子任务的 sweep_orphans 同一姿态：
**落盘状态才是真相，内存协程只是打工的**）。

★ 每次运行新建一个会话（thread）

运行的产物（.md 等）要能下载。下载接口挂在
`/api/conversations/{thread_id}/files/*` 上，其安全边界（`_produced_file_or_404`）
只认「本会话真的产出过的路径」。落一轮进会话历史 = 零新代码拿到
预览 / 下载 / 打包三个接口。代价是会话列表里会出现定时任务专属的会话 ——
这是**特性**：用户点开侧栏历史就能看到「上周三它给我产出了什么」。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from app.schedules import models as sm
from app.schedules.models import RunRecord, ScheduleRecord
from app.schedules.store import schedule_store

logger = logging.getLogger(__name__)

#: finalizer 轮询间隔（秒）。子任务一跑几分钟，5 秒的结算延迟不可见。
_POLL_SECONDS = 5.0
#: 单次运行的兜底上限。超过它只说明一件事：那个子任务永远不会到终态
#: （执行器自身的超时/取消机制没兜住）—— 宁可记一次明确的失败，
#: 也不让运行记录永远转圈。
_RUN_DEADLINE = timedelta(hours=6)


def _now() -> datetime:
    return datetime.now().astimezone()


def _advance_next_run(schedule: ScheduleRecord) -> str:
    """重算并落盘下一次时间（先于派发 —— 见 scheduler.py 的防重理论证）。

    ★ 停用的任务算出空：「没有 next_run_at」是界面判断"它不会自己跑"的依据，
    而「立即运行一次」对暂停的任务也可用 —— 若这里照算未来时间，
    卡片会顶着一个到点不会兑现的时间。
    """
    if not schedule.get("enabled"):
        return ""
    return sm.compute_next_run(
        schedule.get("freq") or {},
        start_date=str(schedule.get("start_date") or ""),
        end_date=str(schedule.get("end_date") or ""),
    )


async def dispatch(schedule: ScheduleRecord, *, trigger: str) -> Optional[RunRecord]:
    """触发一次运行。返回运行记录；None = 没跑（并发闸 / 降级 / 存储不可用）。

    ★ 顺序：推进 next_run_at → 登记运行 → 派发子任务。
    next_run_at 先走：就算后面每一步都炸，调度循环也不会拿同一个时间点
    再触发一次 —— 重复触发的症状是「一天收到两份复习材料并且都烧了 token」，
    比漏一次糟得多。
    """
    sid = str(schedule.get("schedule_id") or "")
    if schedule_store.active_run(sid) is not None:
        logger.info("定时任务上一次运行还没结算，跳过本次触发: %s", sid)
        return None

    if str(schedule.get("kind") or sm.KIND_CHAT) == sm.KIND_WORKFLOW:
        return await _dispatch_workflow(schedule, trigger=trigger)

    instruction = str(schedule.get("instruction") or "").strip()
    if not instruction:
        logger.warning("定时任务指令为空，拒绝触发: %s", sid)
        return None

    thread_id = sm.make_run_id()  # 16 位 hex，天然满足 thread_id 的合法形状
    workdir_root = _resolve_workdir(str(schedule.get("workdir_id") or ""))

    advanced = _advance_next_run(schedule)
    # 先推进再派发（顺序的理由见函数 docstring）。推进失败也不拦派发：
    # 漏跑一次且卡死的后果，比同一分钟窗口里可能重跑一次更糟吗？不是 ——
    # 但 active_run 闸门还在，双保险下这里只留一条告警。
    if not schedule_store.advance_next_run(sid, advanced):
        logger.warning("推进 next_run_at 失败（本次仍继续派发）: %s", sid)

    from app.tasks.executor import executor

    record = executor.start(
        thread_id=thread_id,
        agent=_resolve_agent(str(schedule.get("agent") or "")),
        title=str(schedule.get("title") or "定时任务"),
        instruction=instruction,
        web=bool(schedule.get("web_enabled", True)),
        graph=bool(schedule.get("graph_enabled", True)),
        model=str(schedule.get("model") or ""),
        permission_mode=str(schedule.get("permission_mode") or ""),
        workdir_root=workdir_root,
        source=sm.SOURCE_SCHEDULE,
    )
    if record is None:
        # 降级不是失败，但**必须可见**：静默跳过 = 用户以为它跑了。
        run = schedule_store.start_run(
            schedule_id=sid, thread_id=thread_id, task_id="", trigger=trigger,
        )
        if run is not None:
            _safe_patch_run(sid, run["run_id"], status=sm.RUN_FAILED,
                            error="后台任务能力当前不可用（SUBTASK_ENABLED=false 或存储降级），本次触发未执行")
        logger.warning("定时任务派发降级（executor 返回 None）: %s", sid)
        return run

    task_id = str(record.get("task_id") or "")
    run = schedule_store.start_run(
        schedule_id=sid, thread_id=thread_id, task_id=task_id, trigger=trigger,
    )
    if run is None:
        logger.warning("运行登记失败（子任务照跑，只是这次运行没有留痕）: %s task=%s", sid, task_id)
        return None
    schedule_store.touch_on_dispatch(sid, run=run)

    t = asyncio.create_task(_finalize_async(sid, run["run_id"], task_id, thread_id, schedule),
                            name=f"xjms-schedule-finalize-{run['run_id']}")
    _finalizers.add(t)
    t.add_done_callback(_finalizers.discard)
    return run


async def _dispatch_workflow(schedule: ScheduleRecord, *, trigger: str) -> Optional[RunRecord]:
    """定时触发一个工作流。与对话任务共用调度时间语义，只换执行者。

    ★ 「没跑成」也要留一条可见的运行记录
    绑定被删、工作流存储降级、上一次运行还没完 —— 这些都不是「无事发生」，
    而是「这次到点该跑却没跑」。静默跳过等于让用户以为日报已经生成。
    所以每一种都先登记运行、再如实结算成失败，并写上原因。

    ★ 不新建会话
    工作流的产物与步骤在它自己的运行记录里（`/api/workflows`），
    再落一轮进会话历史会造出第二份真相，而且工作流没有「对话轮次」这个概念。
    """
    sid = str(schedule.get("schedule_id") or "")
    advanced = _advance_next_run(schedule)
    if not schedule_store.advance_next_run(sid, advanced):
        logger.warning("推进 next_run_at 失败（本次仍继续派发）: %s", sid)

    workflow_id = str(schedule.get("workflow_id") or "").strip()
    input_text = str(schedule.get("instruction") or "").strip()

    from app.workflows import runner as workflow_runner
    from app.workflows.store import workflow_store

    def _fail(reason: str) -> Optional[RunRecord]:
        run = schedule_store.start_run(schedule_id=sid, trigger=trigger)
        if run is not None:
            try:
                settled = schedule_store.patch_run(
                    sid, run["run_id"], status=sm.RUN_FAILED, error=reason,
                )
                if settled is not None:
                    run = settled
            except Exception as e:  # noqa: BLE001
                logger.warning("定时工作流失败结算失败: %s %s", run["run_id"], e)
            logger.warning("定时工作流未派发: %s（%s）", sid, reason)
        return run

    if not workflow_id:
        return _fail("这个定时任务还没有绑定工作流")
    if not workflow_store.enabled:
        return _fail("工作流功能已关闭，本次触发未执行")
    workflow = workflow_store.get(workflow_id)
    if workflow is None:
        return _fail("绑定的工作流不存在或已被删除，请重新选择")

    wf_run, reason = workflow_runner.start(workflow, input_text, trigger=trigger)
    if wf_run is None:
        return _fail(reason or "工作流未能启动")

    wf_run_id = str(wf_run.get("run_id") or "")
    run = schedule_store.start_run(
        schedule_id=sid, workflow_run_id=wf_run_id, trigger=trigger,
    )
    if run is None:
        logger.warning(
            "工作流定时运行登记失败（工作流照跑，只是这次没有留痕）: %s wf_run=%s",
            sid, wf_run_id,
        )
        return None
    schedule_store.touch_on_dispatch(sid, run=run)

    t = asyncio.create_task(
        _finalize_workflow_async(sid, run["run_id"], workflow_id, wf_run_id, schedule),
        name=f"xjms-schedule-workflow-finalize-{run['run_id']}",
    )
    _finalizers.add(t)
    t.add_done_callback(_finalizers.discard)
    return run


async def _finalize_workflow_async(
    sid: str, run_id: str, workflow_id: str, wf_run_id: str, schedule: ScheduleRecord,
) -> None:
    """轮询工作流运行到终态，再结算定时任务这边的运行记录。"""
    from app.workflows.store import workflow_store

    deadline = _now() + _RUN_DEADLINE
    record: Optional[dict[str, Any]] = None
    while True:
        await asyncio.sleep(_POLL_SECONDS)
        record = workflow_store.get_run(workflow_id, wf_run_id)
        if record is None:
            _safe_patch_run(
                sid, run_id, status=sm.RUN_FAILED,
                error="找不到对应的工作流运行记录，无法确认执行结果",
            )
            return
        if sm.run_is_terminal(str(record.get("status") or "")):
            break
        if _now() > deadline:
            _safe_patch_run(
                sid, run_id, status=sm.RUN_FAILED,
                error=f"超过 {_RUN_DEADLINE} 仍未结束，按失败结算",
            )
            return
    _settle_workflow(sid, run_id, schedule, record)


def _settle_workflow(
    sid: str, run_id: str, schedule: ScheduleRecord, wf_run: dict[str, Any],
) -> None:
    status = str(wf_run.get("status") or "")
    output = str(wf_run.get("output") or "")
    error = str(wf_run.get("error") or "")

    if status == "succeeded":
        run_status = sm.RUN_SUCCEEDED
    elif status == "cancelled":
        run_status = sm.RUN_CANCELLED
        error = error or "这次运行已取消"
    else:
        run_status = sm.RUN_FAILED
        error = error or "工作流没有正常结束"

    try:
        schedule_store.patch_run(
            sid, run_id,
            status=run_status,
            result=sm.truncate_summary(output),
            error=error[:500],
        )
    except sm.IllegalRunTransition as e:
        logger.info("工作流运行已结算过，跳过: %s %s", run_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("工作流运行结算失败: %s %s", run_id, e)


#: finalizer 的强引用集合 —— asyncio 只持弱引用（不变量 28 的同一个坑）。
_finalizers: set[asyncio.Task] = set()


def _resolve_agent(capability: str) -> str:
    """能力名 → 启用的专员标识；空/认不出都退兜底（registry 自己保证）。"""
    try:
        from app.agents.registry import get_registry

        return get_registry().resolve_capability(capability)
    except Exception as e:  # noqa: BLE001
        logger.warning("专员注册表不可用（定时任务退默认）: %s", e)
        return capability or ""


def _resolve_workdir(workdir_id: str) -> str:
    """绑定的工作区 → 绝对路径。目录没了退沙箱默认（与 resolve_thread_root 同一取舍）。"""
    if not workdir_id:
        return ""
    try:
        from app.workdir.store import get_workdir

        entry = get_workdir(workdir_id)
        if entry is None:
            return ""
        path = str(entry.get("path") or "")
        from pathlib import Path

        return path if path and Path(path).is_dir() else ""
    except Exception as e:  # noqa: BLE001
        logger.warning("工作区解析失败（定时任务退沙箱默认）: %s %s", workdir_id, e)
        return ""


async def _finalize_async(sid: str, run_id: str, task_id: str, thread_id: str,
                          schedule: ScheduleRecord) -> None:
    """等子任务到终态，结算运行记录 + 落一轮进会话历史。"""
    from app.tasks.store import task_store

    deadline = _now() + _RUN_DEADLINE
    record: Optional[dict[str, Any]] = None
    while True:
        await asyncio.sleep(_POLL_SECONDS)
        record = task_store.get(task_id)
        if record is None:
            # 任务记录消失（被清理/存储降级）—— 明确失败，别转圈
            _safe_patch_run(sid, run_id, status=sm.RUN_FAILED, error="找不到对应的后台任务记录，无法确认执行结果")
            return
        if sm_task_is_terminal(str(record.get("status") or "")):
            break
        if _now() > deadline:
            _safe_patch_run(sid, run_id, status=sm.RUN_FAILED,
                            error=f"超过 {_RUN_DEADLINE} 仍未结束，按失败结算")
            return
    _settle(sid, run_id, thread_id, schedule, record)


def sm_task_is_terminal(status: str) -> bool:
    from app.tasks.models import is_terminal

    return is_terminal(status)


def _settle(sid: str, run_id: str, thread_id: str, schedule: ScheduleRecord,
            task_record: dict[str, Any]) -> None:
    """把子任务的终态翻译成运行记录的终态。任何一步失败只告警 ——
    结算失败不该让 finalizer 抛异常石沉大海（它没人 await）。"""
    status = str(task_record.get("status") or "")
    result = str(task_record.get("result") or "")
    error = str(task_record.get("error") or "")
    files = [f for f in (task_record.get("files") or []) if isinstance(f, dict)]
    usage = task_record.get("usage") if isinstance(task_record.get("usage"), dict) else None

    if status == "succeeded":
        run_status = sm.RUN_SUCCEEDED
    elif status == "cancelled":
        run_status = sm.RUN_CANCELLED
    else:
        run_status = sm.RUN_FAILED
        error = error or "后台任务未正常结束"

    try:
        schedule_store.patch_run(
            sid, run_id, status=run_status,
            result=sm.truncate_summary(result),
            error=error[:500],
            files=files or None,
            usage=usage,
        )
    except sm.IllegalRunTransition as e:
        logger.info("运行已结算过，跳过: %s %s", run_id, e)
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("运行结算失败: %s %s", run_id, e)
        return

    _persist_turn(thread_id, schedule, result=result, error=error, status=run_status,
                  files=files, usage=usage)


def _persist_turn(thread_id: str, schedule: ScheduleRecord, *, result: str, error: str,
                  status: str, files: list[dict[str, str]], usage: Optional[dict[str, int]]) -> None:
    """落一轮进会话历史（产物下载、历史回看都挂在这条 thread 上）。

    ★ 事件流传空：进度明细已经在子任务自己的 jsonl 里，
    详情页按 task_id 现查（`GET /api/tasks/{id}` 给 50 步）——
    复制一份进轮次只会造出第二真相，两边漂移时没人说得清哪份是真的。
    """
    try:
        from app.conversations.models import STATUS_ABORTED, STATUS_ERROR, STATUS_OK
        from app.conversations.store import conversation_store

        if status == sm.RUN_SUCCEEDED:
            turn_status, turn_error = STATUS_OK, ""
        elif status == sm.RUN_CANCELLED:
            turn_status, turn_error = STATUS_ABORTED, error
        else:
            turn_status, turn_error = STATUS_ERROR, error
        conversation_store.append_turn(
            thread_id,
            user_input=str(schedule.get("instruction") or ""),
            answer=result,
            events=[],
            status=turn_status,
            error=turn_error,
            usage=usage,
            files=files or None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("定时运行落会话轮次失败（运行记录已结算）: %s", e)


def _safe_patch_run(sid: str, run_id: str, **fields: Any) -> None:
    try:
        schedule_store.patch_run(sid, run_id, **fields)
    except Exception as e:  # noqa: BLE001
        logger.warning("运行补丁失败: run=%s %s", run_id, e)


def sweep_on_startup() -> dict[str, int]:
    """启动对账：结算上次进程留下的未收尾运行。

    与子任务的 `sweep_orphans` 同一个理由 —— 不做自动重放：
    定时运行可能已经写了文件、发了邮件，重跑比标失败危险得多。
    能对上账的（子任务其实跑完了、只是 finalizer 没活到结算）如实结算，
    对不上的标 orphaned，界面上说清「服务重启打断」。
    """
    stats = {"settled": 0, "orphaned": 0}
    for sid, run in schedule_store.active_sweep_candidates():
        run_id = str(run.get("run_id") or "")
        wf_run_id = str(run.get("workflow_run_id") or "")
        if wf_run_id:
            schedule = schedule_store.get(sid)
            wf_run = None
            if schedule is not None:
                try:
                    from app.workflows.store import workflow_store

                    wf_run = workflow_store.get_run(
                        str(schedule.get("workflow_id") or ""), wf_run_id,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("启动对账读取工作流运行失败: run=%s %s", run_id, e)
            if (
                schedule is not None
                and wf_run is not None
                and sm.run_is_terminal(str(wf_run.get("status") or ""))
            ):
                _settle_workflow(sid, run_id, schedule, wf_run)
                stats["settled"] += 1
            else:
                _safe_patch_run(
                    sid, run_id, status=sm.RUN_ORPHANED,
                    error="服务重启时这次运行还没结束，已按中断结算",
                )
                stats["orphaned"] += 1
            continue

        task_id = str(run.get("task_id") or "")
        finished = False
        if task_id:
            try:
                from app.tasks.store import task_store

                record = task_store.get(task_id)
                if record is not None and sm_task_is_terminal(str(record.get("status") or "")):
                    schedule = schedule_store.get(sid)
                    if schedule is not None:
                        _settle(sid, run_id, str(run.get("thread_id") or ""), schedule, record)
                        finished = True
                        stats["settled"] += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("启动对账读取子任务失败: run=%s %s", run_id, e)
        if not finished:
            _safe_patch_run(sid, run_id, status=sm.RUN_ORPHANED,
                            error="服务重启时这次运行还没结束，已按中断结算")
            stats["orphaned"] += 1
    if stats["settled"] or stats["orphaned"]:
        logger.info("定时任务启动对账：结算 %s / 标中断 %s", stats["settled"], stats["orphaned"])
    return stats


async def run_now(schedule: ScheduleRecord) -> Optional[RunRecord]:
    """界面上的「立即运行一次」。与到点触发走**同一个** dispatch ——
    两条路径的差别只在 trigger 标记，复制逻辑必漂移。"""
    return await dispatch(schedule, trigger=sm.TRIGGER_MANUAL)
