"""
后台子任务接口（Supervisor 架构，见 ADR 0019）

进度查询的**两条通道**之一（另一条是主对话 SSE 里的 `subtask_progress`）：

    实时那一屏   →  SSE 事件（用户在发消息，顺带推给他）
    用户空闲时   →  这个接口（2s 轮询）
    关页面/刷新 →  回来后重新拉一次

★ 为什么不给「进度」也开一条 SSE 长连接
ADR 0003 选了 SSE-over-HTTP，而 `lib/sse.ts` 又明确不用 `EventSource`
（要 POST + AbortController）。为一条 99% 时间空闲的通道手写第二个
SSE 解析器，而且它要承受与 `chat.py:11-16` 完全相同的
Nginx 60s 空闲超时 —— 这是最差的连接形态。

★ 路径为什么也要校验
`task_id` 直接进文件路径（`data/tasks/<task_id>.jsonl`）与 URL 路径段，
和 `thread_id` 是同一类攻击面。校验复用**同一个**正则
（`is_valid_task_id` → `is_valid_thread_id`），
不变量 47 的处理方式照抄会话接口：非法与不存在**统一 404**，
免得把「哪些路径存在」变成可探测的信息。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from app.config import settings
from app.schemas.tasks import (
    ProgressEntryOut,
    TaskCancelResponse,
    TaskDeleteResponse,
    TaskListResponse,
    TaskOut,
    TaskResumeResponse,
    TaskStatsResponse,
)
from app.tasks.bus import task_bus
from app.tasks.executor import executor
from app.tasks.models import (
    RESUME_MODE_APPROVAL,
    RESUME_MODE_CHECKPOINT,
    SOURCE_BOARD,
    SOURCE_EMPLOYEE_PRIVATE,
    SOURCE_SUPERVISOR,
    STATUS_INTERRUPTED,
    is_active,
    is_valid_task_id,
)
from app.tasks.store import task_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])

#: 快照最多带多少个任务（与列表接口的上限一致）
_STREAM_SNAPSHOT_LIMIT = 200

#: 列表接口每个任务带多少步进度。
#: ★ 与详情接口的 50 步刻意不同：列表是「概览」，用户在这里看的是
#: 「哪个任务在跑、跑到哪了」，不是逐条读过程。50 步 × 50 个任务
#: 就是 2500 条进度，光序列化就够呛 —— 而用户根本不会在列表里读完它们。
#: 想细看某一条就点开详情（那个接口给 50 步，且带 `total_steps`）。
_LIST_PROGRESS_LIMIT = 20


# ★★ 所有端点都要带 `response_model_exclude_none=True`
#
# Pydantic v2 里 `model_config = {"exclude_none": True}` 对 FastAPI 的
# `response_model` **不生效**（只影响 `model_dump()`，而 FastAPI 走
# 自己的序列化路径）。必须**显式**给路由装饰器传这个参数 ——
# 我实测确认过：漏掉它时响应里是一堆 `"error": null`、`"usage": null`。
#
# 为什么这件事值得较真：`error: null` 看起来像「错误信息是空的」，
# 而真相是「它根本没失败过」；`usage: null` 看起来像「用量是 0」。
# 两者都会让前端做出错误判断 —— 而「填 0 / 填 null」恰好是
# 本项目反复强调要避开的「看起来像真数据」（见 `sse.md` 的约定）。

#: 非法与不存在**用同一句** 404 detail —— 见模块注释
_NOT_FOUND = "任务不存在"

#: `scope` → 允许出现在这一屏的 `TaskRecord.source` 集合。
#: ★ 口径来自施工图 §4.3：「后台任务」这一屏是**主对话**派给子智能体的活，
#:   团队任务是「项目」那一屏的事。混在一起会让人把「员工在群里干活」
#:   读成「我有个后台任务没人管」，而这两个判断会导向完全不同的处置。
#: 空串是改造前的旧记录（那时还没有 source 这一维），它们的真实身份就是主对话子智能体，
#: 所以留在 `chat` 里 —— 把它们过滤掉等于让老任务在界面上凭空消失。
_SCOPE_SOURCES: dict[str, Optional[frozenset[str]]] = {
    "chat": frozenset({"", SOURCE_SUPERVISOR}),
    "workspace": frozenset({SOURCE_BOARD, SOURCE_EMPLOYEE_PRIVATE}),
    "all": None,
}


def _load_or_404(task_id: str) -> dict[str, Any]:
    """取一个任务；非法 id 与不存在都返回 404（不变量 47）"""
    if not is_valid_task_id(task_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    record = task_store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return record


def _to_out(
    record: dict[str, Any],
    *,
    progress: Optional[list[dict[str, Any]]] = None,
    total_steps: Optional[int] = None,
) -> TaskOut:
    """存储层记录 → 接口层形状（`instruction` 刻意不带出去）"""
    data: dict[str, Any] = {
        "task_id": str(record.get("task_id") or ""),
        "thread_id": str(record.get("thread_id") or ""),
        "agent": str(record.get("agent") or ""),
        "title": str(record.get("title") or ""),
        "status": str(record.get("status") or ""),
        "progress": [ProgressEntryOut(**e) for e in (progress or [])],
    }
    for key in (
        "parent_turn_id",
        "depends_on",
        "started_at",
        "finished_at",
        "error",
        "result",
        "usage",
        "files",
        "source",
        # ★ 续跑评估结果只在 `interrupted` 上写；续跑后 executor 会清空，
        # 所以「假值不带」在这里恰好等价于「只在中断时出现」。
        "resume_mode",
        "resume_note",
        # ★ 提问卡片同理：只在 `waiting_input` 上写，回答后 executor 清空。
        "pending_question",
    ):
        value = record.get(key)
        if value:  # ★ 假值一律不带（0 / "" / [] / {} 都不带）
            data[key] = value
    if total_steps is not None:
        data["total_steps"] = total_steps
    return TaskOut(**data)


@router.get("", response_model=TaskListResponse, response_model_exclude_none=True)
async def list_tasks(
    thread_id: str = Query("", description="按会话过滤，留空返回全部"),
    status: str = Query("", description="按状态过滤，逗号分隔"),
    scope: str = Query("chat", description="来源口径：chat=主对话子智能体 | workspace=团队任务 | all"),
    limit: int = Query(50, ge=1, le=200, description="单页条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
) -> TaskListResponse:
    """
    列出后台子任务（按最后活跃时间倒序）。

    ★ 存储不可用时返回 `storage="unavailable"` 而不是空列表
    空列表会被前端渲染成「没有任务」，而真相是「查不了」——
    谎报「没有任务」是这个项目最要防的静默失效形态。

    ★ 默认 `scope=chat`：工作空间派发的任务不在这一屏出现（施工图 §4.3）
    它们的归属是「项目」里那个数字员工的私聊工作台，不是主对话的后台任务。
    传 `scope=workspace` 可以单独看它们，`all` 是不筛（运维用）。
    认不出来的 `scope` 值**退回 `chat`** 而不是 400：这一屏的默认口径必须是安全的那一侧
    ——把团队任务当成后台任务是可见的错，把后台任务凭空消失是不可见的错。

    ★ 列表也带 progress（2026-10 补）
    早期这里只给任务行不给进度，前端纯轮询（没连 SSE）时
    `task.progress` 恒空 → 「展开看进度」永远展不出东西。
    而前端每 2s 的整体替换又会把 SSE 攒到的进度覆盖掉 ——
    表现是「进度闪一下就没了」。
    现在给每个任务带**最近 `_LIST_PROGRESS_LIMIT` 步**（不是全量，见常量注释）。
    """
    sources = _SCOPE_SOURCES.get(scope, _SCOPE_SOURCES["chat"])
    effective = scope if scope in _SCOPE_SOURCES else "chat"
    if not task_store.enabled:
        return TaskListResponse(
            storage="unavailable",
            total=0,
            limit=limit,
            offset=offset,
            scope=effective,
            tasks=[],
            storage_error="后台任务功能已关闭",
        )
    try:
        records, total = task_store.list(
            thread_id=thread_id,
            status=status,
            limit=limit,
            offset=offset,
            sources=sources,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("读取后台任务失败（按不可查询返回）: %s", e)
        return TaskListResponse(
            storage="unavailable",
            total=0,
            limit=limit,
            offset=offset,
            scope=effective,
            tasks=[],
            storage_error=str(e)[:200],
        )

    out: list[TaskOut] = []
    for r in records:
        # ★ 逐条读进度时不能让它把整个列表带崩 ——
        # 某条任务的 jsonl 损坏时，其余任务仍要能列出来（读出异常按空进度算）。
        try:
            view = task_store.progress(str(r.get("task_id") or ""), limit=_LIST_PROGRESS_LIMIT)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取任务进度失败（按空进度返回）: %s", e)
            view = {}
        out.append(
            _to_out(
                r,
                progress=list(view.get("entries") or []),
                total_steps=view.get("total_steps"),
            )
        )

    return TaskListResponse(
        storage="ok",
        total=total,
        limit=limit,
        offset=offset,
        scope=effective,
        tasks=out,
    )


@router.get("/stats", response_model=TaskStatsResponse, response_model_exclude_none=True)
async def task_stats() -> TaskStatsResponse:
    """
    任务统计 + **审批闸门状态**（运维用）。

    ★ 闸门状态必须可查
    闸门卡死是这个系统里唯一「界面完全看不出来」的故障：
    没有报错、任务状态是 `waiting_approval`、审批接口返回 `matched=false`，
    用户只看到「点了没反应」。不变量 23 的精神是
    「元数据说错比没有元数据更糟」—— 元数据缺失同样让人无从下手。
    """
    try:
        raw = task_store.stats()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取任务统计失败: %s", e)
        return TaskStatsResponse(
            enabled=bool(getattr(settings, "SUBTASK_ENABLED", True)),
            total=0,
            by_status={},
            storage="unavailable",
        )
    return TaskStatsResponse(
        enabled=bool(raw.get("enabled", True)),
        total=int(raw.get("total", 0)),
        by_status=dict(raw.get("by_status") or {}),
        gate=dict(raw.get("gate") or {}),
    )


@router.get("/stream")
async def stream_tasks(
    thread_id: str = Query(
        ..., min_length=1, max_length=64, description="订阅哪个会话的后台任务事件"
    ),
) -> EventSourceResponse:
    """
    某个会话的后台任务事件流（**服务端推送**，Supervisor 架构的实时通道）。

    ★ 它存在的理由（见 ADR 0019 与 `tasks/bus.py` 的模块注释）
    子任务跨轮次存活，而主对话 SSE（`/api/chat`）一轮就结束 —— 两者生命周期
    不匹配。所以进度**单开一条长连接**：这条由前端按会话持有，与对话流各走各的。

    ★ 契约：**先快照，后增量**（SSE ≠ 真相源）
    - 连接建立时先推一条 `snapshot`（任务表当前状态）—— 这是**真相源**；
    - 之后推 `subtask` / `subtask_progress` / `subtask_done` 增量。
    前端若发现 `seq` 断档，调 `GET /api/tasks/{id}` 补拉即可，本端点**不做补发**。
    这样任何时刻断开再连都能靠快照回到正确状态，不依赖「事件一条都不能丢」。

    ★ 顺序保证：**先落库，后发布**（`executor.emit` 就是这次序）。
    中间崩溃只会丢 live 事件，库里仍在 —— 重连的快照即恢复。

    ★ 断连清理：由 `EventSourceResponse` 负责
    它在 `http.disconnect` 时取消这个生成器，触发 payload 生成器的 `finally`
    退订。所以**不需要**在这里手写 `request.is_disconnected()` 轮询。
    """
    async def gen():
        async for payload in _task_event_stream(thread_id):
            yield {"data": json.dumps(payload, ensure_ascii=False)}

    return EventSourceResponse(gen(), ping=15)


async def _task_event_stream(thread_id: str) -> AsyncGenerator[dict[str, Any], None]:
    """
    产出进度流的**载荷**（先快照后增量）。

    ★ 为什么把它从端点里抽出来
    `TestClient.stream()` 对**无限** SSE 流的 `close()` 会挂住
    （测试进程等一个永不结束的 app task）。抽成独立函数后，单测可以直接
    `async for` 消费它、拿到需要的帧就 `break`，完全绕开 TestClient 的流式限制。
    生产路径不变（端点只是把它包成 `{"data": …}` 帧）。
    """
    # 先订阅，再取快照，避免读取快照与订阅之间漏掉完成事件。
    q = await task_bus.subscribe(thread_id)
    try:
        snapshot_tasks: list[dict[str, Any]] = []
        if is_valid_task_id(thread_id):
            try:
                records, _ = task_store.list(thread_id=thread_id, limit=_STREAM_SNAPSHOT_LIMIT)
                for rec in records:
                    view = task_store.progress(str(rec.get("task_id") or ""))
                    snapshot_tasks.append(_to_out(rec, progress=list(view.get("entries") or []),
                        total_steps=view.get("total_steps")).model_dump(exclude_none=True))
            except Exception as e:  # noqa: BLE001
                logger.warning("进度流快照读取失败（继续订阅增量）: %s", e)
        yield {"type": "snapshot", "thread_id": thread_id, "tasks": snapshot_tasks}
        # 重连补回已落盘的汇报。稳定 turn_id 令重复快照与实时终版幂等。
        if is_valid_task_id(thread_id):
            from app.conversations import conversation_store
            loaded = conversation_store.get_conversation(thread_id)
            for turn in (loaded[1] if loaded else [])[-30:]:
                marker = next((e for e in turn.events if e.get("type") == "task_report"), None)
                if marker:
                    yield {"type": "assistant_turn", "turn_id": turn.turn_id, "text": turn.answer,
                           "task_count": marker.get("count", 0), "task_status": marker.get("status", "succeeded"),
                           "files": turn.files or []}
        while True:
            yield await q.get()
    finally:
        # ★ 必须退订 —— 漏了会泄漏队列，且后续 publish 白推（不变量 18 的反面）
        await task_bus.unsubscribe(thread_id, q)


@router.get("/{task_id}", response_model=TaskOut, response_model_exclude_none=True)
async def get_task(
    task_id: str,
    limit: int = Query(50, ge=1, le=200, description="返回最近多少步进度"),
) -> TaskOut:
    """
    单个任务的完整进度与结果。

    ★ 进度被截断时响应里带 `total_steps`
    悄悄截断与填 0 是同一类谎报（与 `done.files` 可选字段同一约定）：
    用户以为就这 5 步，实际还有 20 步没显示。
    """
    record = _load_or_404(task_id)
    view = task_store.progress(task_id, limit=limit)
    return _to_out(
        record,
        progress=list(view.get("entries") or []),
        total_steps=view.get("total_steps"),
    )


@router.post("/{task_id}/cancel", response_model=TaskCancelResponse, response_model_exclude_none=True)
async def cancel_task(task_id: str) -> TaskCancelResponse:
    """
    取消一个**未结束**的任务。

    ★ 已经是终态时返回 `ok=false`，**不是 404**
    任务确实存在，只是已经结束了。
    报成「不存在」会让界面显示一个误导性的错误
    （用户会以为是自己记错了 id）。

    ★ 顺序必须是「先落状态、再断协程」
    `task_store.cancel()` 只改索引记录，协程照跑照烧 token —— 那是本轮改造前
    「已取消」骗人的根因。`executor.abort()` 才是真取消点。
    反过来先 abort：协程自己会先把状态写成 `cancelled`，于是这里的
    `cancel()` 返回 False，接口回给用户「任务已经结束，无法取消」。
    """
    if not is_valid_task_id(task_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    if task_store.cancel(task_id):
        # abort 返回 False 不是错误（任务可能已经自然结束 / 重启后只剩一条
        # pending 记录没有协程），所以**不影响** ok。
        aborted = executor.abort(task_id)
        logger.info("取消任务: task=%s 协程已掐=%s（未掐时可能仍在收尾）", task_id, aborted)
        return TaskCancelResponse(ok=True, task_id=task_id)

    # 区分「不存在」与「已结束」：前者仍然 404
    record = task_store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return TaskCancelResponse(
        ok=False,
        task_id=task_id,
        reason=f"任务已经结束（{record.get('status')}），无法取消",
    )


@router.post(
    "/{task_id}/resume",
    response_model=TaskResumeResponse,
    response_model_exclude_none=True,
)
async def resume_task(
    task_id: str,
    acknowledge: bool = Query(
        False, description="已核对过最后一步的副作用，确认继续"
    ),
) -> TaskResumeResponse:
    """
    让一个**中断**的任务从断点继续（复用原 task_id 与原进度流）。

    ★ 为什么不是「一键续跑所有中断任务」
    续跑可能重放最后一次工具调用（写文件、发消息）。是否安全取决于
    「那一步到底有没有生效」—— 这件事只有用户能核实（去目标位置看一眼）。
    `recovery.py` 已经把风险分好档：`checkpoint` / `approval` 直接续，
    `reconcile` 必须先核对。**不确定的时候问，不猜。**

    ★ `reconcile` 不带 `acknowledge=true` 时返回 `ok=false` + `needs_confirm=true`
    这不是错误，而是接口拒绝「静默重放」：把风险原样交回给用户。
    界面据此弹一次确认，用户看过 `reason` 之后再带 `acknowledge=true` 调一次。
    与取消接口同一条约定：`ok=false` = 「这次没做」，不是「任务不存在」。
    """
    record = _load_or_404(task_id)
    status = str(record.get("status") or "")
    if status != STATUS_INTERRUPTED:
        return TaskResumeResponse(
            ok=False,
            task_id=task_id,
            reason=f"任务当前状态是 {status or '未知'}，不需要继续",
        )

    # ★ 只有评估为「干净断点 / 只差一次确认」才允许直接续
    # 其余（`reconcile`，以及评估失败时留下的空串）一律先让用户核对 ——
    # 空串不代表安全，只代表「没评估出来」，把没评估当成安全就是静默重放。
    if str(record.get("resume_mode") or "") not in (
        RESUME_MODE_CHECKPOINT,
        RESUME_MODE_APPROVAL,
    ) and not acknowledge:
        return TaskResumeResponse(
            ok=False,
            task_id=task_id,
            needs_confirm=True,
            reason=str(
                record.get("resume_note")
                or "最后一步可能已经执行过，继续会重做这一步"
            ),
        )

    if not executor.resume(task_id):
        return TaskResumeResponse(
            ok=False,
            task_id=task_id,
            reason="任务状态已变化或服务正在关闭，未能继续",
        )
    logger.info(
        "续跑任务: task=%s mode=%s", task_id, record.get("resume_mode") or "-"
    )
    return TaskResumeResponse(ok=True, task_id=task_id)


@router.delete("/{task_id}", response_model=TaskDeleteResponse, response_model_exclude_none=True)
async def delete_task(task_id: str) -> TaskDeleteResponse:
    """
    删除一个**已结束**的任务（索引记录 + 事件文件，一并清掉子图上下文）。

    ★ 进行中的任务删不得，返回 `ok=false` 而不是 404
    删掉它的 jsonl 而子 Agent 还在往里写，会产生孤儿文件（有事件、索引里没有），
    下次索引重建时它会凭空冒出来（`store.clear` 的注释里推演过全过程）。
    进行中的先走取消接口。这与 `store.delete_by_thread` 的 `keep_active` 口径一致。

    ★ 子图上下文一并清理
    每个子任务在 Checkpointer 里还有一份 `subtask_<task_id>` 状态，删了记录
    留着它，续跑入口仍找得到一份「删了却还记得」的上下文。清理失败只告警：
    记录已删是用户要的结果，孤儿上下文会被淘汰机制兜住。
    """
    record = _load_or_404(task_id)
    status = str(record.get("status") or "")
    if is_active(status):
        return TaskDeleteResponse(
            ok=False,
            task_id=task_id,
            reason=f"任务还在进行中（{status}），先取消再删除",
        )

    if not task_store.delete(task_id):
        return TaskDeleteResponse(ok=False, task_id=task_id, reason="任务刚好被系统清理了")

    try:
        from app.api.conversations import _drop_task_contexts

        await _drop_task_contexts([task_id])
    except Exception as e:  # noqa: BLE001
        logger.warning("清理任务 %s 的子图上下文失败（记录已删除）: %s", task_id, e)

    logger.info("删除任务: task=%s status=%s", task_id, status)
    return TaskDeleteResponse(ok=True, task_id=task_id)


@router.api_route(
    "/{task_id}",
    methods=["GET"],
    include_in_schema=False,
)
async def _task_id_reached(task_id: str) -> None:
    """
    ★ 兜底：连「路由都匹配不上」的 404 也统一文案

    为什么需要它：`/api/tasks/..%2F..%2Fetc` 这类输入**根本进不到**
    `/{task_id}` 处理器 —— Starlette 在 URL 解码阶段就发现它不是合法路径，
    直接返回它自己的 404（`{"detail": "Not Found"}`）。

    于是出现了响应体不一致：
        非法 id（路由匹配得上） → `{"detail": "任务不存在"}`
        路径穿越（路由匹配不上） → `{"detail": "Not Found"}`

    不变量 47 的要求是「不区分『不存在』与『非法』」，
    而**响应体不同就等于泄露了信息**：攻击者能靠文案差异
    判断「这个路径是格式问题」还是「这个任务真的存在」。

    Starlette 会对**包含斜杠的路径**先做解码，斜杠被解码成 `/` 之后
    就变成了多段路径 —— 任何 router 都匹配不上。
    所以只能靠**服务器级的兜底异常处理器**统一。
    这个处理器装在 `app.main` 里（见那里的 `_install_not_found_normalizer`），
    本函数只是让「恰好匹配到 `/tasks/{task_id}` 但方法不对」的情况也有统一文案。
    """
    raise HTTPException(status_code=404, detail=_NOT_FOUND)


__all__ = ["router"]
