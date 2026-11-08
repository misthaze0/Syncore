"""
工作空间接口 —— 工作空间 / 群组 / 员工 / 员工间消息。

★ 命名空间与 id 校验

workspace / group / employee 的 id 直接进 URL 路径段（`/api/workspace/...`），
与 `thread_id` 是同一类攻击面，所以复用 `is_valid_thread_id` 正则。
非法与不存在**统一 404**（不变量 47：不区分两者，免得「哪些 id 存在」可探测）。

★ 可见性隔离的接口侧保证（需求 3、4）

- 员工私聊走 `POST /employees/{id}/chat`（原始 token/tool_call 流），
  正文落 `conversation_store`，**只对用户可见**。
- 群组消息走 `GET /groups/{id}/messages` + `GET /groups/{id}/stream`，
  只返回 orchestrator 显式生成的成品文本，**不暴露员工内部思考**。
两者在存储、通道、页面三个层面物理分离，见 `orchestrator.py` 的说明。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from app.conversations.store import is_valid_thread_id
from app.workspace import emp_thread_id, is_employee_thread_id, workspace_store
from app.workspace.knowledge_scope import drop_workspace as drop_knowledge_boundary
from app.workspace.knowledge_scope import set_workspace as set_knowledge_boundary
from app.workspace.knowledge_scope import status as knowledge_scope_status
from app.workspace.run_store import run_store
from app.workspace.models import (
    EmployeeChatRequest,
    EmployeeCreate,
    EmployeeUpdate,
    GroupMessageRequest,
    CardReviewRequest,
    ScopedMemoryRequest,
    KnowledgeScopeUpdate,
    MemberAdd,
    OrchestrateRequest,
    WorkspaceCreate,
    WorkspaceUpdate,
    clamp_employee_payload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspace", tags=["workspace"])

#: 非法与不存在用同一句 404（不变量 47）
_NOT_FOUND = "资源不存在"


@router.post("/groups/{group_id}/messages")
async def send_group_message(group_id: str, payload: GroupMessageRequest) -> dict[str, Any]:
    _load_group_or_404(group_id)
    from app.workspace.messaging import messenger
    try:
        result = await messenger.send_user(group_id, **payload.model_dump(exclude={"content"}), content=payload.content)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"ok": True, **result}


@router.get("/groups/{group_id}/deliveries")
async def group_deliveries(group_id: str) -> dict[str, Any]:
    _load_group_or_404(group_id)
    from app.workspace.db import workspace_db
    rows = workspace_db.query_all("SELECT d.*,m.content,m.run_id FROM deliveries d JOIN messages m ON m.id=d.message_id "
                                  "WHERE m.group_id=? ORDER BY m.rowid DESC LIMIT 100", (group_id,))
    return {"deliveries": rows}


@router.get("/runs/{run_id}/artifacts")
async def run_artifacts(run_id: str) -> dict[str, Any]:
    if not run_store.get(run_id):
        raise HTTPException(404, detail=_NOT_FOUND)
    from app.workspace.artifact_store import artifact_store
    return {"artifacts": artifact_store.list(run_id)}


@router.get("/artifacts/{artifact_id}/download")
async def download_workspace_artifact(artifact_id: str):
    from app.workspace.db import workspace_db
    from app.workspace.artifact_store import artifact_store
    import hashlib
    artifact = workspace_db.query_one("SELECT * FROM artifacts WHERE id=? AND carrier='file'", (artifact_id,))
    if not artifact:
        raise HTTPException(404, detail=_NOT_FOUND)
    try:
        path = artifact_store.file_path(artifact)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("成果文件已变化，无法下载这个版本")
    except (ValueError, OSError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return FileResponse(path, filename=artifact["name"])


@router.post("/runs/{run_id}/resume")
async def resume_workspace_run(run_id: str) -> dict[str, Any]:
    from app.workspace.recovery import resume_run
    try:
        return {"ok": True, **resume_run(run_id)}
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.post("/cards/{card_id}/review")
async def review_card(card_id: str, payload: CardReviewRequest) -> dict[str, Any]:
    from app.workspace.board import board
    from app.workspace.artifact_store import artifact_store
    from app.workspace import events
    card = board.get(card_id)
    if not card:
        raise HTTPException(404, detail=_NOT_FOUND)
    if card["status"] != "review":
        raise HTTPException(409, detail="这张任务卡当前不处于待验收状态")
    run = run_store.get(card["run_id"])
    if payload.accepted:
        from app.tasks.store import task_store
        attempt = task_store.get(card["task_id"]) or {}
        expected = [(board.get(dep) or {}).get("task_id") for dep in card["depends_on"]]
        if set(expected) != set(attempt.get("depends_on") or []):
            raise HTTPException(409, detail="上游成果已更新，请使用最新成果重新执行后验收")
        if not artifact_store.validate(card["run_id"], card_id):
            raise HTTPException(409, detail="成果缺失或文件已变化，请重新交付后验收")
        updated = board.advance_card(card_id, "done")
        if run["status"] == "review" and all(c["status"] == "done" for c in board.list_cards(run["id"])):
            run_store.advance(run["id"], "completed")
    else:
        active = run_store.active_run_for_group(run["group_id"])
        if active and active["id"] != run["id"]:
            raise HTTPException(409, detail="群内有新的协作正在执行，请结束后再退回重做")
        if run["status"] not in ("review", "partial", "running"):
            raise HTTPException(409, detail="这次协作当前不能退回重做")
        updated = board.advance_card(card_id, "todo", blocked_reason=payload.reason or "用户验收未通过")
        board.invalidate_dependents(run["id"], card_id)
        if run["status"] != "running":
            run_store.advance(run["id"], "running")
        from app.workspace.recovery import restore
        from app.workspace.orchestrator import orchestrator
        context = restore(run["id"])
        context.update(preparing=False, finalized=False)
        # Rework uses a fresh attempt and new immutable artifacts.
        orchestrator.maybe_continue(run["id"])
    workspace_store.append_message(run["group_id"], sender_kind="user", kind="decision", run_id=run["id"],
        content=f"{'已验收' if payload.accepted else '退回重做'}：{card['title']}。{payload.reason}")
    return {"ok": True, "card": events.card_summary(updated)}


@router.get("/employees/{employee_id}/memories")
async def employee_memories(employee_id: str) -> dict[str, Any]:
    _load_employee_or_404(employee_id)
    from app.workspace.scoped_memory import scoped_memory
    return {"memories": scoped_memory.list(employee_id)}


@router.put("/employees/{employee_id}/memories")
async def set_employee_memory(employee_id: str, payload: ScopedMemoryRequest) -> dict[str, Any]:
    employee = _load_employee_or_404(employee_id)
    from app.workspace.scoped_memory import scoped_memory
    owner = employee["workspace_id"] if payload.scope == "project" else employee_id
    try:
        scoped_memory.put(payload.scope, owner, payload.key, payload.value, payload.evidence, "user")
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/employees/{employee_id}/memories")
async def delete_employee_memory(employee_id: str, key: str = Query(..., min_length=1, max_length=100), scope: str = "employee") -> dict[str, Any]:
    employee = _load_employee_or_404(employee_id)
    if scope not in ("project", "employee"):
        raise HTTPException(400, detail="非法记忆范围")
    from app.workspace.scoped_memory import scoped_memory
    owner = employee["workspace_id"] if scope == "project" else employee_id
    return {"ok": scoped_memory.delete(scope, owner, key)}


def _load_ws_or_404(ws_id: str) -> dict[str, Any]:
    if not is_valid_thread_id(ws_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    item = workspace_store.get_workspace(ws_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return item


def _load_group_or_404(group_id: str) -> dict[str, Any]:
    if not is_valid_thread_id(group_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    item = workspace_store.get_group(group_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return item


def _load_employee_or_404(employee_id: str) -> dict[str, Any]:
    if not is_valid_thread_id(employee_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    item = workspace_store.get_employee(employee_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return item


# ---------- 工作空间 ----------

@router.get("/workspaces")
async def list_workspaces(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    items, total = workspace_store.list_workspaces(limit=limit, offset=offset)
    return {"total": total, "count": len(items), "workspaces": items}


@router.post("/workspaces")
async def create_workspace(payload: WorkspaceCreate) -> dict[str, Any]:
    """创建工作空间（自动创建同名群组，见需求 2）。"""
    item = workspace_store.create_workspace(name=payload.name, description=payload.description)
    if item is None:
        raise HTTPException(status_code=500, detail="工作空间创建失败")
    return {"ok": True, "workspace": item}


@router.get("/workspaces/{ws_id}")
async def get_workspace(ws_id: str) -> dict[str, Any]:
    return {"workspace": _load_ws_or_404(ws_id)}


@router.patch("/workspaces/{ws_id}")
async def update_workspace(ws_id: str, payload: WorkspaceUpdate) -> dict[str, Any]:
    _load_ws_or_404(ws_id)
    fields = payload.model_dump(exclude_none=True)
    item = workspace_store.update_workspace(ws_id, **fields)
    return {"ok": True, "workspace": item}


@router.delete("/workspaces/{ws_id}")
async def delete_workspace(ws_id: str) -> dict[str, Any]:
    _load_ws_or_404(ws_id)
    for group in workspace_store.list_groups(ws_id):
        active = run_store.active_run_for_group(group["id"])
        if active:
            run_store.request_cancel(active["id"])
    deleted = workspace_store.delete_workspace(ws_id)
    # 项目没了，它对资料范围的声明也必须跟着没 —— 否则那张表会积累无主前缀，
    # 而这些前缀仍会把命中的文档判成「别人的项目」，让在职员工反而检索不到。
    # 边界是旁路能力，清理失败不能让删除操作报错（SQLite 级联已经做完了）。
    try:
        drop_knowledge_boundary(ws_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("清理项目知识边界失败（%s）: %s", ws_id, e)
    return {"ok": True, "deleted": deleted}


# ---------- 知识边界（ADR 0022）----------

@router.get("/knowledge-scope")
async def get_knowledge_scope() -> dict[str, Any]:
    """
    导出当前生效的项目边界。

    每条附 `workspace_name`（界面要能看懂「这条前缀归谁」而不是只有一串 id），
    以及 `workspace_exists` —— 项目已删但声明还在时它是 False，
    这是用户需要知道的坏状态（那些前缀正在把资料判给一个不存在的项目）。
    """
    st = knowledge_scope_status()
    names: dict[str, str] = {}
    items, _ = workspace_store.list_workspaces(limit=1000, offset=0)
    for ws in items:
        names[str(ws.get("id"))] = str(ws.get("name") or "")

    boundaries = [
        {
            "workspace_id": ws_id,
            "workspace_name": names.get(ws_id, ""),
            "workspace_exists": ws_id in names,
            "prefixes": prefixes,
        }
        for ws_id, prefixes in st["boundaries"].items()
    ]
    return {
        "enabled": any(v for v in st["boundaries"].values()),
        "workspaces_declared": st["workspaces_declared"],
        "path": st["path"],
        "exists": st["exists"],
        "boundaries": boundaries,
    }


@router.put("/workspaces/{ws_id}/knowledge-scope")
async def put_knowledge_scope(ws_id: str, payload: KnowledgeScopeUpdate) -> dict[str, Any]:
    """整体替换一个项目的边界。**校验失败返回 400**，不静默丢掉任何一条。"""
    _load_ws_or_404(ws_id)
    try:
        table = set_knowledge_boundary(ws_id, payload.prefixes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "prefixes": table.get(ws_id, []), "boundaries": table}


@router.delete("/workspaces/{ws_id}/knowledge-scope")
async def del_knowledge_scope(ws_id: str) -> dict[str, Any]:
    """撤掉一个项目的全部声明（等价于「这个项目不声明范围」）。"""
    _load_ws_or_404(ws_id)
    try:
        table = drop_knowledge_boundary(ws_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "boundaries": table}


# ---------- 群组 ----------

@router.get("/groups")
async def list_groups(workspace_id: str = Query("", description="按工作空间过滤，留空返回全部")) -> dict[str, Any]:
    groups = workspace_store.list_groups(workspace_id=workspace_id)
    return {"total": len(groups), "groups": groups}


@router.get("/groups/{group_id}")
async def get_group(group_id: str) -> dict[str, Any]:
    return {"group": _load_group_or_404(group_id)}


@router.get("/groups/{group_id}/messages")
async def list_group_messages(
    group_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    after_seq: int = Query(0, ge=0, description="只要 rowid 大于它的消息（断线补拉游标）"),
) -> dict[str, Any]:
    """
    群组时间线分页。

    ★ `after_seq` 是给「SSE 断过一会儿又活了」准备的
    `messages.id` 是随机 hex、不可比较，所以本项目能用的顺序键只有 SQLite 的
    `rowid`（在这里以 `seq` 字段露出来）。客户端带着见过的最大 seq 来补拉，
    比重新拉全量便宜，也比靠 SSE 重连后的快照更准。
    """
    _load_group_or_404(group_id)
    items, total = workspace_store.list_messages(
        group_id, limit=limit, offset=offset, after_seq=after_seq
    )
    return {"total": total, "count": len(items), "messages": items}


@router.get("/groups/{group_id}/stream")
async def stream_group_messages(group_id: str) -> EventSourceResponse:
    """
    群组消息 SSE（先快照后增量，仿 `/api/tasks/stream`）。

    ★ 这里只推**群组消息**（report/negotiation/conclusion/system 成品文本），
    绝不经过员工私聊的 token/reasoning/tool_call 流 —— 可见性隔离的通道侧保证。
    """
    _load_group_or_404(group_id)

    async def gen() -> AsyncGenerator[dict[str, Any], None]:
        from app.workspace.bus import group_bus
        from app.workspace.orchestrator import stream_group_events

        async for payload in stream_group_events(group_id, group_bus):
            yield {"data": json.dumps(payload, ensure_ascii=False)}

    return EventSourceResponse(gen(), ping=15)


@router.post("/groups/{group_id}/orchestrate")
async def orchestrate_group(group_id: str, payload: OrchestrateRequest) -> dict[str, Any]:
    """
    触发一次编排协作：主持人拆解 → 派发给成员后台并行执行 → 自动汇报 → 汇总结论。

    立即返回 run_id，进度经 `GET /groups/{id}/stream` 推送。

    ★ 同一个群已经在跑第二次 → **409**，不是排队也不是静默覆盖

    约束在数据库的 partial unique 索引上（`uq_runs_active`），所以两个标签页、
    两次 curl 都挡得住 —— 前端 disable 按钮只是体验，不是保证。
    409 的 body 带上正在跑的 run_id，用户才看得懂「在跑的是哪一次」。
    """
    _load_group_or_404(group_id)
    from app.workspace.orchestrator import orchestrator
    from app.workspace.run_store import RunAlreadyActive

    try:
        run_id = await orchestrator.start(
            group_id,
            payload.instruction,
            web=payload.web,
            graph=payload.graph,
            model=payload.model or "",
        )
    except RunAlreadyActive as e:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "这个群已有一次协作在跑",
                "active_run_id": e.run_id,
                "run": run_store.get(e.run_id),
            },
        ) from e
    return {"ok": True, "group_id": group_id, "run_id": run_id}


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """
    查一次协作的状态（run 卡片的数据源）。

    非法与不存在**统一 404**（与会话/任务接口同一口径：
    别让「哪些 id 存在」变成可探测的信息）。
    """
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return {"run": run}


@router.get("/groups/{group_id}/runs")
async def list_group_runs(group_id: str, limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    _load_group_or_404(group_id)
    runs = run_store.list_runs(group_id, limit=limit)
    active = next((r for r in runs if not r["terminal"]), None)
    return {"total": len(runs), "runs": runs, "active_run": active}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    """
    取消一次协作：run 落 `cancelled`，并**真的掐掉**它名下还在跑的成员任务。

    只改 run 状态 = 又在骗人（阶段 0 的教训，见 `executor.abort`）。
    """
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    updated = run_store.request_cancel(run_id)
    if updated is None:
        return {"ok": False, "run_id": run_id, "reason": f"这次协作已经结束（{run['status']}）"}
    return {"ok": True, "run": updated}


@router.post("/groups/{group_id}/members")
async def add_group_member(group_id: str, payload: MemberAdd) -> dict[str, Any]:
    _load_group_or_404(group_id)
    _load_employee_or_404(payload.employee_id)
    ok = workspace_store.add_member(group_id, payload.employee_id, payload.role)
    return {"ok": ok}


@router.delete("/groups/{group_id}/members/{employee_id}")
async def remove_group_member(group_id: str, employee_id: str) -> dict[str, Any]:
    _load_group_or_404(group_id)
    ok = workspace_store.remove_member(group_id, employee_id)
    return {"ok": ok}


# ---------- 员工 ----------

@router.get("/employees")
async def list_employees(workspace_id: str = Query("", description="按工作空间过滤")) -> dict[str, Any]:
    employees = workspace_store.list_employees(workspace_id=workspace_id)
    return {"total": len(employees), "employees": employees}


@router.post("/employees")
async def create_employee(payload: EmployeeCreate) -> dict[str, Any]:
    _load_ws_or_404(payload.workspace_id)
    data = clamp_employee_payload(payload.model_dump(exclude={"workspace_id"}))
    _validate_employee_extensions(data)
    item = workspace_store.create_employee(workspace_id=payload.workspace_id, **data)
    if item is None:
        raise HTTPException(status_code=500, detail="员工创建失败")
    return {"ok": True, "employee": item}


@router.get("/employees/{employee_id}")
async def get_employee(employee_id: str) -> dict[str, Any]:
    return {"employee": _load_employee_or_404(employee_id)}


@router.patch("/employees/{employee_id}")
async def update_employee(employee_id: str, payload: EmployeeUpdate) -> dict[str, Any]:
    _load_employee_or_404(employee_id)
    data = clamp_employee_payload(payload.model_dump(exclude_none=True))
    _validate_employee_extensions(data)
    item = workspace_store.update_employee(employee_id, **data)
    # 配置变了，失效运行时缓存（persona / 工具白名单）
    from app.workspace.employee_runtime import invalidate_employee

    invalidate_employee(employee_id)
    return {"ok": True, "employee": item}


@router.delete("/employees/{employee_id}")
async def delete_employee(employee_id: str) -> dict[str, Any]:
    _load_employee_or_404(employee_id)
    from app.workspace.employee_runtime import invalidate_employee

    invalidate_employee(employee_id)
    deleted = workspace_store.delete_employee(employee_id)
    return {"ok": True, "deleted": deleted}


@router.get("/employees/{employee_id}/thread")
async def get_employee_thread(employee_id: str) -> dict[str, Any]:
    """返回员工私聊的 thread_id，并确保映射已登记。"""
    _load_employee_or_404(employee_id)
    thread_id = emp_thread_id(employee_id)
    workspace_store.link_thread(employee_id, thread_id, kind="private")
    return {"thread_id": thread_id}


@router.get("/employees/{employee_id}/works")
async def list_employee_works(
    employee_id: str,
    limit: int = Query(20, ge=1, le=100, description="单页条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
    progress_limit: int = Query(50, ge=1, le=200, description="每条带最近多少步进度"),
) -> dict[str, Any]:
    """
    这名数字员工**为团队干过的活**（施工图 §4.3 的执行层那一屏）。

    ★ 为什么按 `employee_id` 查、不按 `thread_id`
    他跑群里的任务时，线程归属是**群组**（`thread_id == group_id`），
    而"他的工作记录"要落在他自己的私聊工作台里。按线程查会查出「他什么都没干过」，
    而那不是一句实话，只是一个查错了键的界面。

    ★ 这一屏看全文，群聊只看成品短句
    进度条目、工具调用与返回、`result` 全文、当时接到的原始要求（`instruction`）
    都在这里 —— 这是「数字员工的工作内容只有在自己的私聊里面才能看见」那条口径的
    正面一半。反面是群聊：那里只有一条交付摘要（§4.4）。

    ★ 存储不可用时说「查不了」，不说「他没干过活」
    与 `/api/tasks` 同一纪律：空列表会被渲染成「没有记录」，而真相可能是
    「任务存储没开 / 读挂了」。
    """
    from app.tasks.store import task_store
    from app.agents.approval_broker import approval_broker

    _load_employee_or_404(employee_id)
    approvals = [item for item in approval_broker.list_pending()
                 if item.get("employee_id") == employee_id and not item.get("staged")]
    if not task_store.enabled:
        return {
            "storage": "unavailable",
            "total": 0,
            "limit": limit,
            "offset": offset,
            "works": [],
            "storage_error": "后台任务功能已关闭",
            "approvals": approvals,
        }
    try:
        records, total = task_store.list(
            employee_id=employee_id, limit=limit, offset=offset
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("读取员工工作记录失败（按不可查询返回）: emp=%s %s", employee_id, e)
        return {
            "storage": "unavailable",
            "total": 0,
            "limit": limit,
            "offset": offset,
            "works": [],
            "storage_error": str(e)[:200],
            "approvals": approvals,
        }

    works: list[dict[str, Any]] = []
    for rec in records:
        tid = str(rec.get("task_id") or "")
        # 单条读进度失败不许带崩整屏：那一步按空进度算，其余照给。
        try:
            view = task_store.progress(tid, limit=progress_limit)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取任务进度失败（按空进度返回）: task=%s %s", tid, e)
            view = {}
        item = {
            "task_id": tid,
            "title": str(rec.get("title") or ""),
            "status": str(rec.get("status") or ""),
            "source": str(rec.get("source") or ""),
            "thread_id": str(rec.get("thread_id") or ""),
            "group_id": str(rec.get("group_id") or ""),
            "run_id": str(rec.get("run_id") or ""),
            "board_card_id": str(rec.get("board_card_id") or ""),
            "created_at": str(rec.get("created_at") or ""),
            "started_at": str(rec.get("started_at") or ""),
            "finished_at": str(rec.get("finished_at") or ""),
            "error": str(rec.get("error") or ""),
            "instruction": str(rec.get("instruction") or ""),
            "result": str(rec.get("result") or ""),
            "files": rec.get("files") or [],
            "usage": rec.get("usage") or {},
            # 提问题同理（见 `/api/tasks` 的同名字段）：这一屏是员工私聊里
            # 唯一能看到他「停在等回答」的地方，不带载荷就只能显示状态、答不了。
            "pending_question": rec.get("pending_question"),
            "total_steps": view.get("total_steps") or 0,
            "progress": list(view.get("entries") or []),
        }
        works.append({k: v for k, v in item.items() if v})

    return {
        "storage": "ok",
        "total": total,
        "limit": limit,
        "offset": offset,
        "works": works,
        "approvals": approvals,
    }


@router.post("/employees/{employee_id}/chat")
async def chat_employee(employee_id: str, req: EmployeeChatRequest) -> EventSourceResponse:
    """
    员工私聊 SSE —— 数字员工在**独立聊天界面**里解决问题，过程仅用户可见。

    ★ 这里流式返回的 token / tool_call / reasoning 是「员工内部思考 + 单独对话」，
    只进这一条通道 + `conversation_store`（`emp_<id>` 线程），
    **不进**群组消息表 —— 与群组界面物理隔离（需求 3、4）。
    """
    _load_employee_or_404(employee_id)
    thread_id = req.thread_id or emp_thread_id(employee_id)
    if not is_employee_thread_id(thread_id):
        raise HTTPException(status_code=400, detail="员工私聊线程标识无效")

    async def gen() -> AsyncGenerator[dict[str, Any], None]:
        from app.workspace.employee_stream import get_employee_event_stream

        async for event in get_employee_event_stream(
            employee_id=employee_id,
            user_input=req.message,
            thread_id=thread_id,
            web=req.web,
            graph=req.graph,
            model=req.model,
            thinking=req.thinking,
        ):
            yield {"data": json.dumps(event, ensure_ascii=False)}

    return EventSourceResponse(gen(), ping=15, headers={"X-Accel-Buffering": "no"})


# ---------- 员工可分配的已安装扩展 ----------

def _validate_employee_extensions(data: dict[str, Any]) -> None:
    if "tools_allowed" not in data:
        return
    from app.workspace.capabilities import validate_capabilities

    try:
        data["tools_allowed"] = validate_capabilities(data["tools_allowed"])
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

@router.get("/tools")
async def list_workspace_tools() -> dict[str, Any]:
    from app.workspace.capabilities import installed_capabilities

    tools = sorted(installed_capabilities().values(), key=lambda item: (item.kind, item.display_name))
    return {
        "total": len(tools),
        "tools": [item.public() for item in tools],
    }


__all__ = ["router"]
