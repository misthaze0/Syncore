"""
后台子任务执行器 —— 让子任务活得比主对话那一轮更久（见 ADR 0019）。

★ 它解决的唯一问题：生命周期

Router 时代图的执行绑死在 SSE 连接上 ——
`graph.py` 在生成器里 `create_task(_drive_graph(...))`，
在 `finally` 里 `runner.cancel()`。于是：

    客户端断连 → 生成器被关闭 → 图被取消 → 子 Agent 停跑

Supervisor 时代这**完全不可接受**：
用户派了「查资料并写成文档发给张三」之后关掉页面，
任务必须继续跑完 —— 否则「后台任务」这个词就是假的。

所以执行器有一条铁律：
**`start()` 调用后立即返回，任务的生命周期与任何请求/连接无关。**

★ 三个必须做对的细节

1. **持强引用**。`asyncio` 只对 task 持**弱**引用 ——
   不存强引用的话，任务可能在跑完之前被 GC 回收，
   **不报错、不留痕**，用户只看到任务永远不动。
   （不变量 28 已经踩过一次这个坑，照抄 `graph.py` 的 `_title_tasks` 模式。）

2. **先落盘再执行**（不变量 57）。`mark_running` 落盘之后才真正驱动子图。
   顺序反过来的症状：进程被杀时任务永远停在 `pending`，
   而没有任何东西在等它 —— 用户看到的是一个转不停的 spinner。

3. **审批走闸门**。子图之间是并行的，而前端的 `pendingApproval` 是单个对象，
   不排队会「后一个静默覆盖前一个」（见 `approval_gate.py`）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any, Callable, Dict, List, Optional

from app.agents.approval_flow import backfill_on_event
from app.agents.approval_gate import approval_gate
from app.agents.subgraph import (
    SUBGRAPH_RECURSION_LIMIT,
    build_subgraph,
    initial_subtask_state,
    subtask_thread_id,
)
from app.config import settings
from app.tasks.bus import task_bus
from app.tasks.models import (
    SOURCE_SUPERVISOR,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_WAITING_APPROVAL,
    STATUS_WAITING_INPUT,
    IllegalTransition,
    TaskRecord,
    is_terminal,
)
from app.tasks.store import task_store

logger = logging.getLogger(__name__)

#: ★ 正在跑的子任务的强引用表：`task_id → asyncio.Task`。
#:
#: asyncio 只持弱引用 —— 不存这一份的话任务会被 GC 悄悄回收，
#: 且**没有任何错误**（不变量 28 记了这个坑）。
#: 照抄 `graph.py::_title_tasks` 的模式：登记 + `add_done_callback` 摘除。
#:
#: ★ 为什么是 dict 而不是 set（本轮「真取消」的前提）
#: `POST /api/tasks/{id}/cancel` 过去只改索引里的状态，协程照跑、照烧 token，
#: 之后它的终态写入撞 `IllegalTransition` 被吞 —— 用户看到「已取消」，
#: 而任务其实还在跑。要取消就得能**按 id 找到那个协程**，set 找不到。
#: 排队中（还没拿到信号量）的任务也在表里，`cancel()` 同样能掐掉。
_running: dict[str, asyncio.Task] = {}


def _track(task_id: str, t: asyncio.Task) -> None:
    """登记强引用，并在结束时摘除。

    ★ 摘除时比对身份：`redispatch` 会用同一个 `task_id` 覆盖表项，
      旧协程的 done 回调如果无条件 `pop`，就会把**新**协程从表里抹掉 ——
      于是 `abort()` 找不到它，取消又变回静默失效。
    """
    _running[task_id] = t

    def _discard(done: asyncio.Task, tid: str = task_id) -> None:
        if _running.get(tid) is done:
            _running.pop(tid, None)

    t.add_done_callback(_discard)


#: 并发闸门（进程内信号量）。超出则**排队**，不报错（不变量 14）。
#:
#: ★ 按事件循环缓存，而不是全局单例
#: `asyncio.Semaphore` 在 Python 3.10+ 绑定到创建它的那个 loop
#: （内部用 `get_running_loop` 拿），跨 loop 复用会抛
#: 「attached to a different loop」。测试里每个用例都是新 loop，
#: 全局单例会直接崩 —— 而生产环境也会在「同一进程跑多个 loop」时踩到
#: （比如 uvicorn 的 reload 场景）。
_slots: dict[Any, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    """
    取（或按当前配置创建）并发信号量。

    ⚠️ 配置在**创建那一刻**被读进信号量，之后改 `SUBTASK_MAX_CONCURRENCY`
    不会影响已存在的那个 loop 的信号量。这是有意的：
    运行期改配置应该走重启（它是部署级参数），
    而「改了配置不生效却以为生效了」是这个项目反复强调要避免的静默失效。
    """
    loop = asyncio.get_running_loop()
    sem = _slots.get(loop)
    if sem is None:
        limit = max(1, int(getattr(settings, "SUBTASK_MAX_CONCURRENCY", 3)))
        sem = asyncio.Semaphore(limit)
        _slots[loop] = sem
    return sem


class SubtaskExecutor:
    """
    后台子任务执行器。

    ★ 单进程假设
    与 `approval_broker` / `approval_gate` 同源：并发信号量在内存里，
    多副本部署会让「最多 3 个同时跑」变成「每副本 3 个」。
    真要横向扩展，得把这个信号量换成 Redis 分布式锁。
    """

    def __init__(self) -> None:
        self._shutdown = False

    @property
    def shutting_down(self) -> bool:
        """服务是否正在关停（关停期间取消任务应落 `interrupted` 而不是 `cancelled`）"""
        return self._shutdown

    # ---------- 派发 ----------

    def start(
        self,
        *,
        thread_id: str,
        agent: str,
        title: str,
        instruction: str,
        parent_turn_id: str = "",
        web: bool = True,
        graph: bool = True,
        model: str = "",
        employee_id: str = "",
        group_id: str = "",
        run_id: str = "",
        board_card_id: str = "",
        source: str = "",
        permission_mode: str = "",
        workdir_root: str = "",
        depends_on: Optional[List[str]] = None,
        requires_file: bool = False,
        task_id: str = "",
        on_registered: Optional[Callable[[TaskRecord], None]] = None,
    ) -> Optional[TaskRecord]:
        """
        登记并派发一个子任务，**立即返回**（不 await）。

        返回 `TaskRecord`（含 `task_id`），供调用方发 `subtask` SSE 事件。
        返回 None 表示降级（`SUBTASK_ENABLED=False` 或 id 非法）——
        ★ 调用方**必须**能在 None 时继续跑完这一轮：
        降级不是失败，只是「没有进度可查」。

        ★ `employee_id` / `group_id`（工作空间模块）
        两者同时非空时，子图注入该员工的 persona / 工具白名单，完成后自动向群组汇报。

        ★ `run_id` / `board_card_id` / `source`（任务板缝合，施工图 §2.6）
        看板的卡片与这里的执行记录是两份真相，这三键是它们之间唯一的连接。
        少传 `board_card_id` 的症状不是报错，而是**卡片永远停在 in_progress** ——
        任务已经跑完了，而没有任何人知道该把哪个对账单划掉。

        ★ `permission_mode`（Phase 5）
        派发那一轮的权限档，落盘后由子图冻结使用。空串 = 没记，执行时按
        当前全局默认档跑。调用方**能拿到就传**：`dispatch_node` 拿得到，
        它不传的后果是「用户设了只读，后台子任务照样改文件」——
        只读模式管不住的那条路，正是没人盯着看的那条。
        """
        if self._shutdown:
            logger.info("执行器已关闭，不再派发新任务")
            return None

        for dependency in depends_on or []:
            upstream = task_store.get(dependency)
            if not upstream or upstream.get("thread_id") != thread_id or upstream.get("parent_turn_id", "") != parent_turn_id:
                raise ValueError("前置任务必须属于同一次请求")
        if task_id and task_store.get(task_id):
            existing = task_store.get(task_id)
            if existing.get("thread_id") != thread_id or existing.get("run_id", "") != run_id or existing.get("employee_id", "") != employee_id:
                raise ValueError("任务标识已被另一条执行记录占用")
            if on_registered:
                on_registered(existing)
            return existing
        record = task_store.create(
            task_id=task_id,
            thread_id=thread_id,
            agent=agent,
            title=title,
            instruction=instruction,
            parent_turn_id=parent_turn_id,
            web_enabled=web,
            graph_enabled=graph,
            model=model,
            employee_id=employee_id,
            group_id=group_id,
            run_id=run_id,
            board_card_id=board_card_id,
            source=source,
            permission_mode=permission_mode,
            workdir_root=workdir_root,
            depends_on=depends_on,
            requires_file=requires_file,
        )
        if record is None:
            logger.warning("任务登记失败（降级：子任务不派发）")
            return None

        task_id = str(record["task_id"])
        if on_registered:
            on_registered(record)
        coro = self._run(
            task_id=task_id,
            thread_id=thread_id,
            agent=agent,
            instruction=instruction,
            web=web,
            graph=graph,
            model=model,
            employee_id=employee_id,
            group_id=group_id,
            run_id=run_id,
            permission_mode=permission_mode,
            workdir_root=workdir_root,
        )
        t = asyncio.create_task(coro, name=f"xjms-subtask-{task_id}")
        # ★ 强引用：漏了这一行任务会被 GC 静默回收
        _track(task_id, t)
        return record

    def redispatch(self, task_id: str) -> bool:
        """
        重新派发一个 **pending** 任务（复用原 task_id，不新建记录）。

        用于 `sweep_orphans` 挑出来的 pending 任务 —— 服务重启后它们没有
        对应的 asyncio task（随旧进程一起没了），需要重新 create_task。
        ★ 复用原 task_id 而不是 `start()` 新建：新建会留下一个悬空的旧记录，
        界面上会出现两个同名的任务。

        运行时选项（联网/图路/模型）从落盘的记录里恢复 ——
        这要求 `start()` 在登记时把它们写进 TaskRecord（见 store.create）。
        旧记录（改造前落盘的）缺省时回退默认，已在 record_from_dict 兜住。
        """
        record = task_store.get(task_id)
        if record is None:
            return False
        if str(record.get("status")) != STATUS_PENDING:
            return False

        coro = self._run(
            task_id=task_id,
            thread_id=str(record.get("thread_id") or ""),
            agent=str(record.get("agent") or ""),
            instruction=str(record.get("instruction") or ""),
            web=bool(record.get("web_enabled", True)),
            graph=bool(record.get("graph_enabled", True)),
            model=str(record.get("model") or ""),
            employee_id=str(record.get("employee_id") or ""),
            group_id=str(record.get("group_id") or ""),
            # ★ 重启复用必须把 run_id 一起恢复：审批键与留痕都靠它定位，
            #   少这一行的症状是重启后那次协作的危险操作不再显示是谁请求的
            run_id=str(record.get("run_id") or ""),
            # 权限档同理按**派发时**的那一份跑：用户在任务飞出去之后把全局
            # 从 manual 改成 auto，不该让一个已经在跑的任务突然拿到自动批准。
            permission_mode=str(record.get("permission_mode") or ""),
            workdir_root=str(record.get("workdir_root") or ""),
        )
        t = asyncio.create_task(coro, name=f"xjms-subtask-{task_id}")
        _track(task_id, t)
        return True

    def resume(self, task_id: str) -> bool:
        """
        让一个 `interrupted` 任务从 checkpoint 接着跑（复用原 task_id）。

        ★ 与 `redispatch` 的区别只在「起点」
        `redispatch` 是从头跑一个**没开始过**的 pending 任务；
        这里是从上次停下的位置继续 —— 因此**不传**初始状态（`resume=True`），
        由 checkpointer 把图恢复到断点，`ainvoke(None)` 继续。

        ★ 先落盘再执行（不变量 57）
        状态先写回 `running`，再 create_task。反过来的话，进程在这一行之后
        被杀，界面上会看到一个 `interrupted` 的任务其实正在跑。

        ★ 不自动调用
        续跑可能重放最后一步（写文件、发消息）。什么时候可以续由
        `recovery.py` 评估、由用户点按钮决定，这里只负责执行决定。
        """
        record = task_store.get(task_id)
        if record is None:
            return False
        if str(record.get("status") or "") != STATUS_INTERRUPTED:
            return False
        if self._shutdown:
            logger.info("执行器已关闭，不再恢复任务")
            return False

        try:
            task_store.patch(
                task_id,
                status=STATUS_RUNNING,
                # 恢复字段只在「中断」这一屏有意义，续跑后必须清掉 ——
                # 否则任务跑完之后详情里还挂着「继续前请核对」的旧提示。
                resume_mode=None,
                resume_note=None,
            )
        except IllegalTransition:
            # 并发里刚被别人改了状态（取消 / 已续跑）—— 不是错误
            return False

        coro = self._run(
            task_id=task_id,
            thread_id=str(record.get("thread_id") or ""),
            agent=str(record.get("agent") or ""),
            instruction=str(record.get("instruction") or ""),
            web=bool(record.get("web_enabled", True)),
            graph=bool(record.get("graph_enabled", True)),
            model=str(record.get("model") or ""),
            employee_id=str(record.get("employee_id") or ""),
            group_id=str(record.get("group_id") or ""),
            run_id=str(record.get("run_id") or ""),
            permission_mode=str(record.get("permission_mode") or ""),
            workdir_root=str(record.get("workdir_root") or ""),
            resume=True,
        )
        t = asyncio.create_task(coro, name=f"xjms-subtask-{task_id}")
        _track(task_id, t)
        return True

    # ---------- 取消 ----------

    def abort(self, task_id: str) -> bool:
        """
        掐掉这个 task_id 对应的协程（如果它还在跑或在排队）。

        ★ 这是「取消」唯一有意义的地方
        `task_store.cancel()` 只改索引里的状态字段；协程不归它管，
        于是过去取消 = **界面显示已取消，模型继续被调用、token 继续烧**。
        状态与真相必须同时改，所以接口层两个都要调：先 `cancel()` 落状态
        （返回 ok=True 给用户），再 `abort()` 断协程。

        返回是否真找到了在跑的协程。False **不是错误**：任务可能已经自然结束、
        可能在本进程重启后只剩一条 pending 记录（`sweep_orphans` 的场景）。
        ⚠️ 调用方不许因为 False 把状态回滚 —— 用户的取消意图已经落在记录上了。
        """
        t = _running.get(task_id)
        if t is None or t.done():
            return False
        t.cancel()
        return True

    # ---------- 单个任务的执行 ----------

    async def _run(
        self,
        *,
        task_id: str,
        thread_id: str,
        agent: str,
        instruction: str,
        web: bool,
        graph: bool,
        model: str,
        employee_id: str = "",
        group_id: str = "",
        run_id: str = "",
        permission_mode: str = "",
        workdir_root: str = "",
        resume: bool = False,
    ) -> None:
        """
        跑完一个子任务：排队 → 落盘 running → 驱动子图 → 落盘终态。

        ★ 异常处理：任何失败都要落到一个**明确**的终态
        不能让任务停在 `running` —— 那样界面上就是一个永远转圈的进度条，
        而用户完全不知道出了什么事。

        `resume=True` 表示从 checkpoint 接着跑（见 `resume()`），
        此时不传初始状态，图自己恢复到断点。
        """
        from app.workspace.budget import current_run, run_budget
        budget_context = current_run.set(run_id)
        if run_id:
            run_budget.ensure(run_id)
        sem = _semaphore()
        acquired = False
        from app.observability import begin_trace, get_current_recorder, set_current_recorder
        parent_recorder = get_current_recorder()
        recorder = None
        try:
            if settings.OBSERVABILITY_ENABLED:
                recorder = begin_trace(instruction, thread_id)
                recorder.add_metadata(source="background_task", task_id=task_id,
                                      parent_trace_id=parent_recorder.trace_id if parent_recorder else "")
        except Exception:  # noqa: BLE001 - 追踪故障不阻塞任务
            logger.debug("后台任务追踪初始化失败", exc_info=True)
        try:
            if not resume:
                instruction = await self._dependency_input(task_id, instruction)
            await sem.acquire()
            acquired = True

            # ---------- 不变量 57：先落盘，再执行 ----------
            # 落盘失败也不该跳过执行：状态不可查是「看不见」，
            # 不执行是「什么都没发生」—— 后者严重得多。
            try:
                started = task_store.mark_running(task_id)
                if run_id and not started:
                    raise RuntimeError("团队任务无法登记执行状态，已停止执行")
            except Exception as e:  # noqa: BLE001
                if run_id:
                    raise
                logger.warning("任务状态落盘失败（仍继续执行）: task=%s %s", task_id, e)

            drive = self._drive(
                task_id=task_id,
                thread_id=thread_id,
                agent=agent,
                instruction=instruction,
                web=web,
                graph=graph,
                model=model,
                employee_id=employee_id,
                run_id=run_id,
                permission_mode=permission_mode,
                workdir_root=workdir_root,
                resume=resume,
            )
            if run_id:
                await asyncio.wait_for(drive, run_budget.remaining_seconds(run_id))
            else:
                await drive
        except asyncio.CancelledError:
            # 取消是「有人不要它了」（关服务 / 显式 cancel），不是失败
            #
            # ★ 只在记录还没落终态时写它。接口层的顺序是「先 `store.cancel()`
            #   标终态、再 `abort()` 断协程」，到这里记录往往已经是 cancelled，
            #   再 patch 会撞 IllegalTransition。那是**预期**结果，但把它塞进
            #   `except Exception: pass` 会连带吞掉真正的写盘故障 —— 所以显式判断。
            #
            # ★ 关服务 ≠ 取消
            # 用户点「取消」是不要这个任务了；关服务只是执行环境没了。
            # 后者落 `interrupted`，下次启动就能评估「还能不能接着跑」——
            # 落成 cancelled 等于把一段本可以续的工作判了死刑。
            # 排队中的任务保持 pending：它没有任何副作用，启动时会自动重派。
            try:
                record = task_store.get(task_id)
                status = str((record or {}).get("status") or "")
                if record is not None and not is_terminal(status) and status != STATUS_PENDING:
                    if self._shutdown:
                        task_store.patch(
                            task_id,
                            status=STATUS_INTERRUPTED,
                            resume_note="服务关闭时任务中断，重开后可继续",
                        )
                    else:
                        task_store.patch(task_id, status="cancelled")
            except Exception as e:  # noqa: BLE001
                logger.warning("取消后状态落盘失败: task=%s %s", task_id, e)
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("子任务执行失败: task=%s %s", task_id, e, exc_info=True)
            try:
                task_store.mark_failed(task_id, error=str(e)[:500])
            except Exception:  # noqa: BLE001
                pass
        finally:
            if acquired:
                sem.release()
            # ★ 广播终态（若已到达）—— 前端据此把卡片从「进行中」翻成最终状态，
            # 不必等下一次轮询。放在 finally：正常收尾 / 失败 / 取消三条路都会经过。
            _broadcast_terminal(task_id, thread_id)
            # ★ 工作空间自动汇报 hook（需求 3、4 的「进度汇报」）：
            # 员工任务到终态后，由他自己打一次模型产出一条群聊交付摘要。
            # 这里必须 await：fire-and-forget 会让 run 的汇总结论先于成员发言到达时间线。
            try:
                await self._maybe_report_to_group(task_id, employee_id, group_id)
            finally:
                current_run.reset(budget_context)
            try:
                if recorder:
                    record = task_store.get(task_id) or {}
                    status = str(record.get("status") or "")
                    recorder.finish(status="ok" if status == STATUS_SUCCEEDED else "cancelled" if status == "cancelled" else "error",
                                    error=str(record.get("error") or "") or None)
            except Exception:  # noqa: BLE001
                logger.debug("后台追踪落盘失败", exc_info=True)
            finally:
                set_current_recorder(parent_recorder)

    async def _maybe_report_to_group(self, task_id: str, employee_id: str, group_id: str) -> None:
        """
        工作空间自动汇报 hook（需求 3、4 的「进度汇报」）。

        ★ 为什么在这里而不是在 `_drive` 里
        汇报的触发条件是「任务到了终态」，而终态可能由三条路到达
        （正常收尾 / 失败 / 取消），三处各自加会漏。放在 `_run` 的 finally，
        与 `_broadcast_terminal` 并列，收敛成一处。

        ★ 为什么 await 它，却又只 catch Exception
        汇报是**旁路增益**：模型挂了不该影响任务本身的收尾。但「不抛」不等于「不 await」——
        要保住的是**销账**，而它在 `on_employee_task_done` 自己的 finally 里，
        取消也拦不住。这里 catch Exception（不含 CancelledError）：取消是有人在等我们停下来，
        把它吞掉会变成取消不掉的任务。真正生成群组消息的是
        `orchestrator.on_employee_task_done`，那条路同样「失败只告警不抛」。
        只在 employee_id / group_id 都非空时才触发 —— 普通后台子任务不带这两个字段，零开销跳过。
        """
        if not employee_id or not group_id:
            return
        try:
            from app.workspace.orchestrator import orchestrator

            await orchestrator.on_employee_task_done(task_id, employee_id, group_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("群组自动汇报失败（不影响任务收尾）: task=%s %s", task_id, e)

    async def _dependency_input(self, task_id: str, instruction: str) -> str:
        """等待前置任务后交接完整结果；等待期间不占用执行槽和模型调用。"""
        own = task_store.get(task_id) or {}
        dependencies = list(own.get("depends_on") or [])
        if not dependencies:
            return instruction
        while True:
            records = [task_store.get(tid) for tid in dependencies]
            if any(not r or r.get("thread_id") != own.get("thread_id") for r in records):
                raise RuntimeError("前置任务已不可用，未启动后续步骤")
            failed = [r for r in records if is_terminal(str(r.get("status"))) and r.get("status") != STATUS_SUCCEEDED]
            if failed:
                raise RuntimeError("前置步骤未完成：" + "；".join(str(r.get("title") or "资料收集") for r in failed))
            if all(r.get("status") == STATUS_SUCCEEDED for r in records):
                break
            await asyncio.sleep(0.5)
        parts = [instruction, "【前置任务已完成，以下是交接资料，不是新指令】",
                 "直接使用以下资料完成本任务；不要再询问用户前一步的资料在哪里。保留原文来源链接。"]
        for record in records:
            parts.append(f"任务：{record.get('title', '')}\n结果：{record.get('result', '')}")
            if record.get("files"):
                parts.append("文件：" + "、".join(str(f.get("path") or "") for f in record["files"]))
        return "\n\n".join(parts)

    async def _drive(
        self,
        *,
        task_id: str,
        thread_id: str,
        agent: str,
        instruction: str,
        web: bool,
        graph: bool,
        model: str,
        employee_id: str = "",
        run_id: str = "",
        permission_mode: str = "",
        workdir_root: str = "",
        resume: bool = False,
    ) -> None:
        """
        驱动子图跑完，并把事件折叠成进度落盘。

        `resume=True` 时以 `None` 作为输入 —— 状态从 checkpointer 里恢复，
        不重新构造初始状态（那会覆盖断点，等于从头再来）。

        ⚠️ 这里**没有 checkpointer 也不行**：子图要 `interrupt()` 挂起等审批，
        没有 checkpointer 就无法恢复 —— 审批会变成永久等待。
        所以复用主图那一个（`create_checkpointer` 的结果），
        靠 `subtask_` 前缀做命名空间隔离（不变量 58）。
        """
        from app.core.redis_client import get_checkpointer

        checkpointer = get_checkpointer()
        if checkpointer is None:
            # ⚠️ 这里选择**明确失败**而不是「悄悄禁掉危险工具」
            #
            # 两种降级方式都想过：
            #   a) 禁用 danger 工具，让任务跑完安全的那部分
            #   b) 直接失败，说清原因
            #
            # 选 (b)，因为 (a) 有个静默失效：用户要求「把结果发给张三」，
            # 子 Agent 悄悄跳过了发送，界面上显示「任务完成」——
            # 而结果根本没发出去，用户要等到张三没收到邮件才发现。
            # 一个说清原因的失败，比一个悄悄做了半件事的「成功」诚实。
            #
            # 正常情况下这里几乎不会走到：`create_checkpointer` 一定会
            # 降级成 InMemorySaver（不变量 14），拿到的是一个能用的对象。
            # 真正为 None 只可能是图没初始化 —— 那时整个 Supervisor 也不可用。
            raise RuntimeError("会话状态存储未就绪，后台任务无法安全运行（危险操作需要审批机制）")

        subgraph = build_subgraph(checkpointer=checkpointer)
        # ★ 续跑不构造初始状态：checkpointer 里的那份才是断点。
        # 传 None 给 `ainvoke` 的语义是「从这里接着走」。
        state: Optional[dict[str, Any]] = None
        if not resume:
            state = initial_subtask_state(
                instruction=instruction,
                agent=agent,
                task_id=task_id,
                thread_id=thread_id,
                web=web,
                graph=graph,
                model=model,
                employee_id=employee_id,
                # ★ 权限档在**任务开跑时**定下来，之后不再跟着界面变
                # （理由见 `initial_subtask_state` 的字段注释）。
                # 空串 = 派发时没记（旧记录）→ 按当前全局默认档跑。
                permission_mode=permission_mode,
                workdir_root=workdir_root,
                run_id=run_id,
            )

        # 事件 → 进度的折叠只有一处（`tasks/store.py::_entry_from_event`），
        # 所以「实时看到的进度」与「轮询查到的进度」必然一致。
        #
        # ★ 这里同时回填**审批留痕的执行结果**
        # 预授权自动放行的 danger 工具执行完之后，要往留痕里写「执行成功/失败」
        # （否则审批面板上那条记录永远停在「已批准」，看不出执行结果）。
        # 主图那条路在 `graph.py::record_event` 里做同样的事 ——
        # 那是**两个**驱动层，所以两处都要做。
        # 回填逻辑本身在 `approval_flow`（不变量 36：写入点唯一）。
        #
        # ---------- 叙述段累积（★ 状态是**闭包**里的，随任务生灭）----------
        # 子 Agent 的 `token` 是逐字增量：一个回答几千条。
        # 每条都落盘会让 jsonl 瞬间爆掉、前端被几千条一模一样的事件刷屏。
        # 所以这里攒成一段，在**段结束**时落一条 `narrative`。
        #
        # ★ 为什么「段结束」=「收到任意非 token 事件」
        # 子图的事件序列天然是这样：
        #     token… token… → tool_call → tool_result → token… → done
        # 工具调用/结果/终态一出现，就说明上一段话已经说完了。
        # 不需要再引入「段落结束」事件（那会多一种契约）。
        #
        # ★ 为什么累积在调用方而不是 store 里
        # store 的 `_entry_from_event` 是**纯函数** —— 正是这一点保证了
        # 「实时流」与「轮询」给出同一份事实。让它变有状态就破坏了这个保证。
        # 而这里天然有状态（`emit` 闭包），也知道段边界。
        narrative_parts: list[str] = []

        def _flush_narrative() -> None:
            """把攒到的正文落成一条进度并广播。空段什么都不做。"""
            if not narrative_parts:
                return
            text = "".join(narrative_parts)
            narrative_parts.clear()
            entry = task_store.append_narrative(task_id, text)
            if entry is not None:
                task_bus.publish(
                    thread_id,
                    {"type": "subtask_progress", "task_id": task_id, **entry},
                )

        tool_spans: dict[str, Any] = {}

        def emit(event: dict[str, Any]) -> None:
            # ★ 顺序：先落库（真相源）→ 再广播（加速通道）。
            # 反过来的话，中间崩溃会推出「库里没有、前端却见过」的事件 ——
            # 重连/刷新时它会凭空消失（SPEC 的「先落库后 publish」）。
            if event.get("type") == "token":
                # 逐字增量：只攒着，不落盘也不广播（见上面的论证）
                narrative_parts.append(str(event.get("content") or ""))
                return
            # 子图寿命长于主请求，工具过程记在各自的后台 trace 中。
            from app.observability import get_current_recorder
            recorder = get_current_recorder()
            try:
                if recorder:
                    call_id = str(event.get("call_id") or "")
                    if event.get("type") == "tool_call":
                        tool_spans[call_id] = recorder.start_span(f"tool:{event.get('tool_name') or 'unknown'}",
                            kind="tool", input=event.get("args"), call_id=call_id, tool_name=event.get("tool_name"))
                    elif event.get("type") == "tool_result" and call_id in tool_spans:
                        recorder.end_span(tool_spans.pop(call_id),
                            output=str(event.get("result") or "")[:4000],
                            status="ok" if event.get("success") else "error",
                            error=None if event.get("success") else str(event.get("result") or "执行失败")[:1000])
            except Exception:  # noqa: BLE001
                logger.debug("后台工具追踪失败", exc_info=True)
            # 非 token 事件 = 上一段话说完了，先收口再处理它自己 ——
            # 顺序不能反：反了的话进度列表里「工具调用」会排在
            # 「解释为什么调用它」的前面，读起来是倒的。
            _flush_narrative()
            entry = task_store.append_event(task_id, event)
            # ★ 回填要带上协作与员工两维：群组里 thread_id 是共用的，
            #   只按 (thread_id, call_id) 找会把甲的执行结果写进乙的审批留痕
            backfill_on_event(event, thread_id=thread_id, run_id=run_id, employee_id=employee_id)
            if entry is not None:
                # `append_event` 返回的是折叠后的进度条目（含 seq/kind/label/status），
                # 正是前端 `subtask_progress` 需要的形状 —— 折叠只有一处
                # （`store._entry_from_event`），实时流与轮询因此必然一致。
                task_bus.publish(
                    thread_id,
                    {"type": "subtask_progress", "task_id": task_id, **entry},
                )

        config = {
            "configurable": {"thread_id": subtask_thread_id(task_id), "emit": emit},
            "recursion_limit": SUBGRAPH_RECURSION_LIMIT,
        }

        # ★ 续跑时先看一眼断点是不是挂在审批或提问上
        # 是的话**先问、再 `Command(resume=...)`**，而不是直接 `ainvoke(None)`：
        # 后者会让 tool_executor 节点从头重放一遍（钩子、判定、留痕都会再跑一次），
        # 然后才重新停下——用户会收到两次弹窗，留痕里也会多一条。
        pre_interrupt = await self._pending_interrupt(subgraph, config) if resume else None

        result = await self._run_with_suspension(
            subgraph,
            state,
            config,
            task_id=task_id,
            thread_id=thread_id,
            employee_id=employee_id,
            run_id=run_id,
            pre_interrupt=pre_interrupt,
        )

        # ---------- 收尾 ----------
        # ★ 先 flush 最后一段叙述再结算状态
        # 子图最后一句回答往往是「token… token…」直接结束的
        # （planner 在没有工具调用时只设 `final_answer`，不发 done 事件），
        # 所以闭包里还攒着一段。不 flush 的话它永远不会出现在进度里 ——
        # 而它恰恰是「这个任务最后说了什么」，最该被看到的一段。
        _flush_narrative()

        final_answer = str(result.get("final_answer") or result.get("sub_agent_result") or "")
        final_failure = str(result.get("final_failure") or "")
        if final_failure.strip():
            # 子图自己判了「没完成」：终态是失败，但带着可读的原因
            task_store.mark_failed(task_id, error=final_failure)
        elif final_answer.strip():
            files = _collect_files(result)
            # ★ 客观事实校验（收尾前的最后一道闸）：子任务的说辞不能越过
            # 记录里的客观事实 —— 声称交付文件必须有文件真的写进来，
            # 工具全败不能靠「拼一份假报告」冒充完成。
            verdict = _delivery_verdict(final_answer, files, list(result.get("tool_calls") or []))
            record = task_store.get(task_id) or {}
            if record.get("requires_file") and not files:
                verdict = "报告尚未交付：没有实际保存文件。已完成的资料仍保留在任务结果中。"
            if not verdict and record.get("requires_file") and files:
                from app.core.sandbox import resolve_in_sandbox
                for file in files:
                    try:
                        target = resolve_in_sandbox(str(file.get("path") or ""))
                        if not target.is_file() or target.stat().st_size == 0:
                            verdict = "产出文件不存在或为空，尚未完成交付。"
                    except (ValueError, OSError):
                        verdict = "产出文件暂不可读取，尚未完成交付。"
            if verdict:
                task_store.mark_failed(task_id, error=verdict)
            else:
                task_store.mark_succeeded(task_id, result=final_answer, files=files)
        else:
            # 没有正文却「成功」是最糟的收尾：界面上显示绿勾，点进去是空的
            task_store.mark_failed(task_id, error="子任务没有产出回答")

    async def _run_with_suspension(
        self,
        subgraph: Any,
        state: dict[str, Any],
        config: dict[str, Any],
        *,
        task_id: str,
        thread_id: str,
        employee_id: str = "",
        run_id: str = "",
        pre_interrupt: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        驱动子图，并在它挂起（审批或提问）时排队等待人工输入。

        两种挂起在 `_resume_for` 里按 payload 的 `kind` 分流，等待队列各自独立。

        ★ 闸门必须在 `finally` 里释放
        漏了的话对应的队列会永久停摆 —— 而且**不报错**，
        界面上只是「点了没反应」。这是本架构唯一会静默卡死的故障。

        `employee_id` / `run_id` 往下进审批/提问的键与留痕（护栏 6）：
        这里的 `thread_id` 在工作空间路径下**就是群组 id**，
        少了这两维，同组三个员工的弹窗在数据结构上无法区分。

        `pre_interrupt` 是续跑时从 checkpoint 里取到的挂起载荷：
        它意味着图**还没**走到 `interrupt()` 之后，先问清楚再 `Command(resume=...)`
        是最短的路径（见 `_drive` 的注释）。
        """
        graph_input: Any = state
        while True:
            if pre_interrupt is not None:
                payload, pre_interrupt = pre_interrupt, None
                graph_input = await self._resume_for(
                    payload,
                    task_id=task_id,
                    thread_id=thread_id,
                    employee_id=employee_id,
                    run_id=run_id,
                )
                continue

            result = await subgraph.ainvoke(graph_input, config)
            interrupts = _collect_interrupts(result)
            if not interrupts:
                return result if isinstance(result, dict) else {}

            payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
            payload = payload if isinstance(payload, dict) else {"reason": str(payload)}
            graph_input = await self._resume_for(
                payload,
                task_id=task_id,
                thread_id=thread_id,
                employee_id=employee_id,
                run_id=run_id,
            )

    async def _resume_for(
        self,
        payload: dict[str, Any],
        *,
        task_id: str,
        thread_id: str,
        employee_id: str = "",
        run_id: str = "",
    ) -> Any:
        """
        一个挂起点的分流：提问 → `Command(resume=回答)`；其余 → 审批。

        ★ 判别只看 payload 的 `kind`（与主图同一条规则）
        提问的载荷由 `build_question_payload` 构造，带 `kind="question"`；
        审批的载荷由宿主构造，没有这个键。模型无法把一次审批伪装成提问。

        ★ 两条路的等待队列必须分开
        `question_gate` 与 `approval_gate` 各自排队：用户在批一个危险操作时，
        一条不相关的提问不该排在它前面；反过来也一样。
        """
        from langgraph.types import Command

        from app.questions.models import is_question_payload

        if is_question_payload(payload):
            outcome = await self._ask_question(
                task_id=task_id,
                thread_id=thread_id,
                payload=payload,
                employee_id=employee_id,
                run_id=run_id,
            )
            return Command(resume=outcome.to_resume())

        approved = await self._ask_and_wait(
            task_id=task_id,
            thread_id=thread_id,
            payload=payload,
            employee_id=employee_id,
            run_id=run_id,
        )
        return Command(resume={"approved": approved})

    @staticmethod
    async def _pending_interrupt(subgraph: Any, config: dict[str, Any]) -> Optional[dict[str, Any]]:
        """
        从 checkpoint 快照里取「断点是不是正挂在审批或提问上」。

        取不到就返回 None —— 那只是「没有挂起的输入」，续跑按普通路径走。
        绝不能因为这里失败就放弃续跑：大多数中断根本不是在等人工。
        """
        try:
            snapshot = await subgraph.aget_state(config)
        except Exception as e:  # noqa: BLE001
            logger.debug("读取断点快照失败（按普通续跑处理）: %s", e)
            return None
        interrupts = _collect_interrupts(snapshot)
        if not interrupts:
            return None
        payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        return payload if isinstance(payload, dict) else {"reason": str(payload)}

    async def _ask_and_wait(
        self,
        *,
        task_id: str,
        thread_id: str,
        payload: dict[str, Any],
        employee_id: str = "",
        run_id: str = "",
    ) -> bool:
        """
        排队 → 落 `waiting_approval` → 弹窗 → 拿决策 → 回到 running。

        抽成独立方法是为了让「续跑时先问」与「跑着跑着停下问」走同一条路：
        两处各写一遍，总有一处会漏掉闸门释放或状态回填。
        """
        # ★ 排队等审批（不变量 40 在并行子图下的补丁）
        await approval_gate.wait_turn(task_id)
        await self._set_run_awaiting_user(run_id, True)
        try:
            task_store.mark_waiting_approval(task_id)
            # 审批的**留痕与推送**都在 `approval_flow` 里（不变量 36）——
            # 执行器不自己写留痕，那会变成「两处都写」。
            decision = await self._approve(
                task_id=task_id,
                thread_id=thread_id,
                payload=payload,
                employee_id=employee_id,
                run_id=run_id,
            )
            return bool(decision.get("approved", False))
        finally:
            # ★ 必须释放，否则审批队列永久停摆（且不报错）
            await approval_gate.release(task_id)
            await self._set_run_awaiting_user(run_id, False)
            try:
                current = task_store.get(task_id)
                if current is not None and current.get("status") == STATUS_WAITING_APPROVAL:
                    task_store.patch(task_id, status="running")
            except Exception:  # noqa: BLE001
                pass

    async def _ask_question(
        self,
        *,
        task_id: str,
        thread_id: str,
        payload: dict[str, Any],
        employee_id: str = "",
        run_id: str = "",
    ):
        """
        排队 → 落 `waiting_input` + 提问载荷 → 推卡片 → 等回答 → 回到 running。

        与 `_ask_and_wait` 是**并列**的两条路，不是同一个函数的参数分支：
        等待队列（`question_gate`）、留痕（`questions/records`）、任务状态
        （`waiting_input` vs `waiting_approval`）、恢复载荷（作答 vs 布尔）
        四样全不同。硬合成一条，必然在「超时算什么」「要不要发 state_update」
        这类判断上互相污染。
        """
        from app.questions.flow import (
            question_gate,
            read_question_answer,
            run_pending_question,
        )
        from app.questions.models import QuestionOutcome

        call_id = str(payload.get("call_id") or "unknown")
        await question_gate.wait_turn(task_id)
        await self._set_run_awaiting_user(run_id, True)
        outcome = QuestionOutcome()
        try:
            task_store.mark_waiting_input(task_id, payload)
            try:
                async for event in run_pending_question(
                    thread_id=thread_id,
                    call_id=call_id,
                    questions=list(payload.get("questions") or []),
                    run_id=run_id,
                    employee_id=employee_id,
                    requestor=self._requestor_label(employee_id),
                ):
                    answered = read_question_answer(event)
                    if answered is not None:
                        outcome = answered
                        break
                    if event.get("type") == "question":
                        # ★ 落的是 flow 产出的完整载荷，不是 interrupt 那份
                        # interrupt 里没有 question_id / timeout_seconds，而刷新后
                        # 重画的卡片回答时必须带回 question_id —— 少了它，
                        # 后端判成「这张卡已过期」，用户点了却什么都没发生。
                        task_store.patch(task_id, pending_question=event)
                    task_store.append_event(task_id, event)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"等待回答期间上下文丢失（{e}），请重试") from e
            return outcome
        finally:
            # 与审批同一条纪律：闸门漏放会让提问队列永久停摆，且不报错
            await question_gate.release(task_id)
            await self._set_run_awaiting_user(run_id, False)
            try:
                current = task_store.get(task_id)
                if current is not None and current.get("status") == STATUS_WAITING_INPUT:
                    # mark_running 顺带清掉 pending_question：卡片已经消费过了
                    task_store.mark_running(task_id)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _set_run_awaiting_user(run_id: str, waiting: bool) -> None:
        """
        审批挂起期间把 run 标成 `input_required`，决策后放回 `running`。

        ★ 为什么值得为它写一次状态迁移

        「等你补充信息」是界面上唯一能把「员工在等人」和「员工干砸了」分开的信号。
        少了它，一次危险操作的审批等待在群组里表现为：转圈、什么都不动、
        300 秒后卡片变成「卡住了」—— 用户看到的是一次失败，实际是一次没被问。

        `awaiting=False` 时**只在当前确实是 `input_required` 才往回填**：
        等待期间停滞检测可能已经把它推成 `stalling`，
        无条件写 `running` 会把别人的治理判定擦掉（两份真相各写一半是这个项目反复踩的坑）。

        ⚠️ 迁移不合法 / 库不可用时**只记日志**。这是治理层的旁路记账，
        它挂掉不该让一次审批失败（不变量 18）—— 任务该继续走就该继续走。
        """
        if not run_id:
            return
        try:
            from app.workspace.run_store import (
                STATUS_INPUT_REQUIRED,
                STATUS_RUNNING,
                run_store,
            )

            row = run_store.get(run_id)
            if row is None:
                return
            current = str(row["status"])
            if waiting:
                if current != STATUS_INPUT_REQUIRED:
                    run_store.advance(run_id, STATUS_INPUT_REQUIRED)
            elif current == STATUS_INPUT_REQUIRED:
                run_store.advance(run_id, STATUS_RUNNING)
        except Exception as e:  # noqa: BLE001
            logger.debug("run 的等待审批态标记失败（不影响任务执行）: run=%s %s", run_id, e)

    async def _approve(
        self,
        *,
        task_id: str,
        thread_id: str,
        payload: dict[str, Any],
        employee_id: str = "",
        run_id: str = "",
    ) -> dict[str, Any]:
        """
        走 `approval_flow` 拿审批结果，把事件落成进度。

        ★ `requestor` 是给人看的那句「是谁在请求」（护栏 6 的界面半边）
        群组里 thread_id 共用，三条弹窗若不写出员工名，用户其实是在盲签 ——
        他会以为自己批的是刚才那个查资料的，实际放行的是发邮件的。
        名字在这里查（`workspace_store` 函数内 import，与
        `_maybe_report_to_group` 同一手法：执行层不硬依赖工作空间层），
        查不到就退回 id —— 空字符串会让弹窗看起来像「系统在请求」，那是误导。

        ★ 为什么不能靠事件反推决策
        最初这里是「消费完事件后从进度里反推用户批没批」——
        但 `tool_result` 的 status 与审批决策是两回事
        （工具执行成功 ≠ 审批通过），
        两者混在同一个 `kind` 空间里必然出现
        「工具失败所以没批准」这种错误归因。
        所以改成走 `approval_flow.read_decision` 那个**内部通道** ——
        与主图里是同一套写法（两处各写一遍「怎么从异步生成器拿结果」迟早漂移）。

        ⚠️ 为什么这里 catch 一切
        审批期间 Redis 可能重启 → Checkpointer 降级 → 恢复会失败。
        那时必须让任务变成 `failed` 且**说清楚原因**，
        而不是让它永远停在 `waiting_approval` ——
        最坏的情况是用户点了「批准」、`/api/approval` 返回成功，
        但任务再也不往下走（见 ADR 0019 的风险五）。
        """
        from app.agents.approval_flow import read_decision, run_pending_approval

        approved = False
        call_id = str(payload.get("call_id", "unknown"))
        try:
            async for event in run_pending_approval(
                thread_id=thread_id,
                call_id=call_id,
                tool_name=str(payload.get("tool_name", "")),
                tool_args=payload.get("args") or {},
                danger_reason=str(payload.get("reason", "该操作需要人工确认")),
                danger_level=str(payload.get("danger_level", "danger")),
                decision_source=str(payload.get("decision_source", "danger_level")),
                permission_mode=str(payload.get("permission_mode", "")),
                run_id=run_id,
                employee_id=employee_id,
                requestor=self._requestor_label(employee_id),
            ):
                decision = read_decision(event)
                if decision is not None:
                    approved = decision
                    break
                task_store.append_event(task_id, event)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"审批期间上下文丢失（{e}），请重试") from e
        return {"approved": approved}

    @staticmethod
    def _requestor_label(employee_id: str) -> str:
        """
        「是谁在请求这次审批」的人话名字。查不到就退回 id，**不给空串**。

        空串会让弹窗读起来像「系统在请求」，而护栏 6 要防的恰恰是用户
        不知道自己正在替谁签字 —— 一个不完美的名字比没有名字诚实。
        """
        if not employee_id:
            return ""
        try:
            from app.workspace.store import workspace_store

            employee = workspace_store.get_employee(employee_id)
        except Exception as e:  # noqa: BLE001 - 观测层不许把主流程打挂（不变量 18）
            logger.debug("审批请求方名字查询失败（退回 id）: emp=%s %s", employee_id, e)
            return employee_id
        name = str((employee or {}).get("name") or "").strip()
        return name or employee_id

    # ---------- 关停 ----------

    async def shutdown(self, *, timeout: float = 5.0) -> int:
        """
        关停时取消所有在跑的任务。

        ★ `_shutdown` 必须**先**置真再 cancel
        `_run` 的取消分支靠它区分「用户点了取消」和「服务要关机了」：
        前者落 `cancelled`（不要了），后者落 `interrupted`（环境没了，可续）。
        顺序反了的话，每次正常重启都会把在跑的任务判成用户取消。

        返回被取消的数量。
        """
        self._shutdown = True
        pending = [t for t in _running.values() if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        return len(pending)

    async def reset(self) -> None:
        """服务启动时调用：清掉关停标记"""
        self._shutdown = False


def _collect_interrupts(result: Any) -> list[Any]:
    """
    从子图结果里提取 interrupt。

    ★★★ 三种挂载位置都要试，缺一个都是「审批永远不来」
    ① `result["__interrupt__"]` —— **dict 键**（`ainvoke` 的返回值是 dict）
    ② `result.__interrupt__` —— 属性（某些版本/包装器下）
    ③ `result.tasks[*].interrupts` —— 任务级（主图 `aget_state` 那条路径）

    ⚠️ 我最初只写了 ② 与 ③，漏了 ① ——
    而 ① 恰恰是 `ainvoke` 的实际返回形态。
    症状是「子 Agent 挂起后 `_collect_interrupts` 返回空」，
    于是执行器认为它已经跑完，直接去读一个空的 final_answer：
    **任务显示成功，结果是空的**，而那个危险操作永远停在「等审批」。

    这正是 ADR 0019 风险五描述的最坏情况 ——
    而且它不会报错，只会让界面显示一个「完成但没有结果」的任务。
    """
    # ① dict 键（ainvoke 的实际返回形态）
    if isinstance(result, dict):
        raw = result.get("__interrupt__")
        if raw:
            return list(raw) if isinstance(raw, (list, tuple)) else [raw]
    # ② 属性
    raw = getattr(result, "__interrupt__", None)
    if raw:
        return list(raw) if isinstance(raw, (list, tuple)) else [raw]
    # ③ 快照级（`aget_state` 的返回值：StateSnapshot.interrupts）
    raw = getattr(result, "interrupts", None)
    if raw:
        return list(raw) if isinstance(raw, (list, tuple)) else [raw]
    # ④ 任务级
    tasks = result.get("tasks") if isinstance(result, dict) else getattr(result, "tasks", None)
    for task in tasks or ():
        found = getattr(task, "interrupts", None)
        if found:
            return list(found)
    return []


def _collect_files(result: dict[str, Any]) -> Optional[list[dict[str, str]]]:
    """从子图结果里收集产出的文件（复用会话历史那套派生逻辑，不重写）"""
    from app.conversations.files import collect_written_files

    calls = result.get("tool_calls") or []
    if not calls:
        return None
    # 子图状态里带着派发时冻结的工作区根：产物路径要按它换算成沙箱相对形式
    # 子图保留的是已合并的调用记录，历史派生器接收的是 call/result 事件对。
    events: list[dict[str, Any]] = []
    for call in calls:
        events.append({**call, "type": "tool_call"})
        events.append({**call, "type": "tool_result"})
    return collect_written_files(events, str(result.get("workdir_root") or ""))


#: 「声称交付了文件」的模式：交付动词与文件名同句出现（动词在前），
#: 或「产出文件：xxx」这类强措辞。只认这两种 —— 不把「读取了 data.json」
#: 这种提及输入文件的句子误判成声称交付。
_FILE_CLAIM_RE = re.compile(
    r"(?:产出文件[:：]|已保存|保存至|保存为|写入了?|导出为?|生成了?文件|见附件)"
    r"[^\n。；]{0,24}?"
    r"[A-Za-z0-9_\-\u4e00-\u9fff]{2,40}\.(?:md|markdown|txt|docx?|xlsx?|pptx?|pdf|csv|json|html?)"
)
#: 文件名在前、完成态动词在后的形式（「报告.md 已生成」）。
#: 刻意不匹配「data.json 生成统计」—— 生成后面没有完成态标记。
_FILE_CLAIM_AFTER_RE = re.compile(
    r"[A-Za-z0-9_\-\u4e00-\u9fff]{2,40}\.(?:md|markdown|txt|docx?|xlsx?|pptx?|pdf|csv|json|html?)"
    r"[^\n。；]{0,12}?(?:已(?:生成|写入|保存|导出)|保存成功|生成完毕)"
)
#: 否认措辞：先于声称判断 —— 句子里有这些时，即使有文件名也不算声称。
_FILE_NEGATION_RE = re.compile(
    r"产出文件[:：]\s*(?:无|暂无|没有)|"
    r"未能?(?:产出|保存|写入|生成|导出|交付)|"
    r"没有(?:产出|保存|写入|生成|交付)|"
    r"无法(?:写入|保存|生成|导出|交付)"
)


def _claims_file_delivery(text: str) -> bool:
    """最终回答是否**声称**交付了文件（与文件是否真存在无关）"""
    if _FILE_NEGATION_RE.search(text):
        return False
    return bool(_FILE_CLAIM_RE.search(text) or _FILE_CLAIM_AFTER_RE.search(text))


def _all_tools_failed(tool_calls: List[Dict[str, Any]]) -> bool:
    """有工具调用且每一条的 success 严格为 False（None = 未执行，不算失败也不算成功）"""
    calls = [c for c in tool_calls or [] if isinstance(c, dict)]
    return bool(calls) and all(c.get("success") is False for c in calls)


def _delivery_verdict(
    final_answer: str,
    files: Optional[List[Dict[str, str]]],
    tool_calls: List[Dict[str, Any]],
) -> str:
    """
    收尾前的客观事实校验。返回空串 = 通过；非空 = 失败原因（按失败收尾）。

    ★ 为什么「说辞」不能越过「记录」
    子任务的 final_answer 是模型写的，它可能声称「产出文件：xxx.md」
    但从未调用过任何写入工具 —— 汇报生成器会原样转述这句话，
    用户点不开一个不存在的文件。两道闸分别堵两类谎：
    声称文件却没有写入记录、所有工具都失败却用未经证实的内容冒充完成。
    """
    if not (files or []) and _claims_file_delivery(final_answer):
        return "任务声称交付了文件，但没有任何文件被实际写入（写入类工具的调用记录为空）—— 该声称不可信，任务按失败收尾。"
    if _all_tools_failed(tool_calls):
        return (
            f"任务的全部 {len(tool_calls)} 次工具调用都失败了，没有拿到任何真实数据"
            "—— 不能用未经证实的内容冒充完成，任务按失败收尾。"
        )
    return ""


def _broadcast_terminal(task_id: str, thread_id: str) -> None:
    """
    任务跑到终态时广播一条 `subtask_done`（若有终态）。

    ★ 为什么要重读一次 record 而不是让调用方传状态
    终态可能由三条路到达（正常收尾 / 失败 / 取消），分散在三处；
    在 finally 里统一「读一次最终记录、是终态就广播」把三处收敛成一处 ——
    少一处分支就少一次「某条路忘了广播」（那会让前端卡片永远转圈）。

    非终态（还在跑、或异常中途退出但没落终态）时不广播 ——
    那种情况由轮询与 sweeper 兜底。
    """
    record = task_store.get(task_id)
    if record is None:
        return
    status = str(record.get("status") or "")
    if not is_terminal(status):
        return
    # ★ 只带「有值」的可选字段（不填 null / 不填 0）—— 与全项目约定一致
    event: dict[str, Any] = {"type": "subtask_done", "task_id": task_id, "status": status}
    for key in ("title", "result", "error", "usage", "files"):
        value = record.get(key)
        if value:
            event[key] = value
    task_bus.publish(thread_id, event)
    grouped = bool(record.get("parent_turn_id")) and str(record.get("source") or "") in ("", SOURCE_SUPERVISOR) and not record.get("employee_id")
    if grouped:
        from app.tasks import report
        report.schedule_report(thread_id)
    elif status == STATUS_FAILED:
        _notify_failure_in_conversation(task_id, thread_id, record)
    elif status == STATUS_SUCCEEDED:
        # 成功的一半（`app/tasks/report.py`）：安排总 Agent 向用户做完成汇报。
        # 失败通知是「写一条固定消息」，成功汇报是「让总 Agent 再生成一句」——
        # 所以这里只负责**触发**，生成与送达都在汇报模块里（旁路，失败有兜底）。
        # ★ 员工/看板任务不走这里：report._needs_report 有同一套边界判断，
        # 但在这里先拦一道，避免给每个工作空间任务都白跑一次领取。
        from app.tasks import report

        report.schedule_report(thread_id)


def _notify_failure_in_conversation(task_id: str, thread_id: str, record: TaskRecord) -> None:
    """
    任务失败后，在**主对话时间线**里追加一条助理发起的消息。

    ★ 为什么是「写进对话」而不是只让任务面板显示红叉
    后台任务可能跑几十分钟，失败时用户很可能不在任务面板页。
    失败对用户来说不能是「那个任务自己消失了」——
    得在用户一定会看的地方（主对话）留一句说得清原因的话。

    这是 C 修复的另一半：`final_failure` 把原因写进任务的 error，
    这里把它带进对话 —— 两步缺一，用户都只会看到「又失败了」。

    ★ 工作空间任务不走这里
    员工/看板任务（employee_id 非空）有自己的汇报通道（群组消息、卡片状态），
    再往用户对话里写一遍等于同一件事通知两次。
    """
    if str(record.get("employee_id") or ""):
        return
    title = str(record.get("title") or "").strip() or "未命名任务"
    error = str(record.get("error") or "").strip() or "未知原因"
    text = f"后台任务「{title}」失败了：{error}"
    try:
        from app.conversations import INITIATED_BY_ASSISTANT, conversation_store
        from app.conversations.models import STATUS_OK

        turn_id = uuid.uuid4().hex[:16]
        summary = conversation_store.append_turn(
            thread_id,
            user_input="",
            answer=text,
            events=[{"type": "token", "content": text}],
            status=STATUS_OK,
            turn_id=turn_id,
            initiated_by=INITIATED_BY_ASSISTANT,
        )
    except Exception:  # noqa: BLE001 - 落盘失败不影响任务终态本身
        logger.warning("任务失败通知落盘失败: task=%s thread=%s", task_id, thread_id)
        return
    if summary is None:
        return
    # 实时推给前端：正在看对话的用户立刻看到这条通知，不必刷新
    task_bus.publish(thread_id, {"type": "assistant_turn", "turn_id": turn_id, "text": text})
    try:
        from app.channels.wechat_worker import wechat_channel

        wechat_channel.mirror_turn(thread_id, text)
    except Exception:  # noqa: BLE001 - 微信不可用仍照常展示桌面通知
        logger.warning("任务失败通知同步微信失败: thread=%s", thread_id)


#: 进程级单例
executor = SubtaskExecutor()


__all__ = ["SubtaskExecutor", "executor"]
