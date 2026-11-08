"""Rebuild coordination from persisted attempts; interrupted effects are never replayed blindly."""
from __future__ import annotations

import json
import time

from app.tasks.store import task_store
from app.workspace.board import board
from app.workspace.db import workspace_db
from app.workspace.run_store import run_store, ACTIVE_STATUSES
from app.workspace.store import workspace_store


def restore(run_id: str) -> dict:
    from app.workspace.orchestrator import orchestrator
    run = run_store.get(run_id)
    if not run:
        raise ValueError("协作不存在")
    if run_id in orchestrator._runs:
        return orchestrator._runs[run_id]
    options = workspace_db.query_one("SELECT payload FROM run_options WHERE run_id=?", (run_id,))
    options = json.loads(options["payload"]) if options else {}
    members = [m for m in workspace_store.list_members(run["group_id"]) if m.get("enabled", True)]
    if options.get("target_employee_id"):
        members = [m for m in members if m["id"] == options["target_employee_id"]]
    context = {"group_id": run["group_id"], "instruction": run["instruction"], "done_results": {},
               "conversational": bool(options.get("conversational")),
               "finalized": False, "preparing": True, "members": members,
               "public_context": orchestrator._group_context(run["group_id"])}
    orchestrator._runs[run_id] = context
    context["dispatch"] = orchestrator._make_dispatcher(run_id, run["group_id"], members,
        web=options.get("web", True), graph=options.get("graph", True), model=options.get("model", ""))
    for tid in run["ledger"]:
        task = task_store.get(tid)
        if task and task.get("status") == "succeeded":
            context["done_results"][tid] = str(task.get("result") or "")
    return context


async def reconcile_workspaces() -> int:
    from app.tasks.executor import executor
    from app.tasks.models import is_terminal
    from app.workspace.artifact_store import artifact_store
    from app.workspace.inbox import inbox
    from app.workspace.messaging import messenger
    from app.workspace.orchestrator import orchestrator

    rows = workspace_db.query_all("SELECT id FROM runs WHERE status IN (" + ",".join("?" for _ in ACTIVE_STATUSES) + ")",
                                  tuple(ACTIVE_STATUSES))
    # A crash may have left a claimed card before the executor had persisted its attempt.
    for row in rows:
        rid = row["id"]
        # Unknown usage from the old process remains charged, but cannot keep a reservation forever.
        workspace_db.execute("UPDATE run_budgets SET tokens=tokens+reserved,reserved=0 WHERE run_id=?", (rid,))
        context = restore(rid)
        run = run_store.get(rid)
        if not workspace_db.query_one("SELECT id FROM messages WHERE run_id=? AND sender_kind='user'", (rid,)):
            request = workspace_db.query_one("SELECT client_id FROM message_requests WHERE run_id=?", (rid,))
            workspace_store.append_message(run["group_id"], kind="goal", sender_kind="user", run_id=rid,
                content=run["instruction"], meta={"client_id": request["client_id"]} if request else {})
        for card in board.list_cards(rid):
            if card["status"] == "in_progress" and not card["task_id"]:
                board.advance_card(card["id"], "todo", blocked_reason="重启前已认领但尚未登记派发，重新开放认领")
        attempts = workspace_db.query_all("SELECT * FROM dispatches WHERE run_id=? AND state<>'failed'", (rid,))
        for attempt in attempts:
            tid = attempt["task_id"]
            orchestrator.track_task(rid, tid)
            record = task_store.get(tid)
            if not record and attempt["state"] == "pending":
                executor.start(**json.loads(attempt["payload"]), task_id=tid,
                               on_registered=lambda record, rid=rid: orchestrator.track_task(rid, record["task_id"]))
            elif not record:
                run_store.record_task_result(rid, tid, employee_id=attempt["employee_id"], status="failed")
                card = board.get(attempt["card_id"])
                if card and card["status"] in ("todo", "in_progress"):
                    board.advance_card(card["id"], "blocked", blocked_reason="执行记录缺失，请核对结果后重新派发")
            workspace_db.execute("UPDATE dispatches SET state='dispatched' WHERE task_id=?", (tid,))
        interrupted = False
        for tid in list((run_store.get(rid) or run)["ledger"]):
            record = task_store.get(tid)
            if not record:
                run_store.record_task_result(rid, tid, employee_id="", status="failed")
                continue
            if is_terminal(str(record.get("status"))):
                artifact_store.register_task(record)
                board.sync_from_task(record)
                messenger.task_done(record)
                orchestrator._settle_run(tid, record.get("employee_id", ""), record["status"], record.get("result", ""))
            elif record.get("status") == "interrupted":
                interrupted = True
                orchestrator.track_task(rid, tid)
            else:
                orchestrator.track_task(rid, tid)
        # Release delivery leases only when their task can no longer process them.
        for message in inbox.pending(run_id=rid):
            messenger.arm_deadline(message)
            record = task_store.get(message["delivery_task_id"]) if message["delivery_task_id"] else None
            if message["delivery_state"] == "processing" and not record:
                workspace_db.execute("UPDATE deliveries SET state='pending',task_id='' WHERE message_id=?", (message["id"],))
        context["preparing"] = False
        current = run_store.get(rid)
        if current["status"] == "planned" and board.list_cards(rid):
            run_store.advance(rid, "running")
            current = run_store.get(rid)
        if interrupted:
            if current["status"] == "running":
                run_store.advance(rid, "input_required", error="执行环境已恢复，部分任务需从检查点继续")
            workspace_store.append_message(run["group_id"], kind="system", run_id=rid,
                content="协作记录已恢复。中断的任务保留了检查点，请点击继续；可能已执行的操作需要先核对。")
        elif not board.list_cards(rid) and not current["pending"]:
            options = workspace_db.query_one("SELECT payload FROM run_options WHERE run_id=?", (rid,))
            options = json.loads(options["payload"]) if options else {}
            if current["status"] == "planned":
                context["preparing"] = True
                orchestrator._schedule_pipeline(rid, group_id=run["group_id"], instruction=run["instruction"],
                                               members=context["members"], web=options.get("web", True),
                                               graph=options.get("graph", True), model=options.get("model", ""))
            else:
                orchestrator.maybe_continue(rid)
        else:
            messenger.wake_run(rid)
            orchestrator.maybe_continue(rid)
    return len(rows)


def resume_run(run_id: str) -> dict:
    from app.tasks.executor import executor
    from app.workspace.orchestrator import orchestrator
    run = run_store.get(run_id)
    if not run or run["terminal"]:
        raise ValueError("当前协作无法继续")
    records = [task_store.get(tid) for tid in run["pending"]]
    interrupted = [r for r in records if r and r.get("status") == "interrupted"]
    if any(r.get("resume_mode") == "reconcile" for r in interrupted):
        raise ValueError("部分任务最后一步可能已经执行，请先在员工工作台核对后继续")
    if not interrupted:
        raise ValueError("当前没有可从检查点继续的任务")
    if any(r.get("resume_mode") not in ("checkpoint", "approval") for r in interrupted):
        raise ValueError("部分任务没有可恢复的检查点")
    restore(run_id)["preparing"] = False
    if run["status"] != "running":
        run_store.advance(run_id, "running")
    workspace_db.execute("UPDATE run_budgets SET started=?,error='' WHERE run_id=?", (time.time(), run_id))
    count = 0
    for record in interrupted:
        orchestrator.track_task(run_id, record["task_id"])
        count += int(executor.resume(record["task_id"]))
    return {"resumed": count}
