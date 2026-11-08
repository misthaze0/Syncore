"""Persist dispatch intent and run membership before a task can execute."""
from __future__ import annotations

import json
import uuid

from app.conversations.models import now_iso
from app.workspace.db import workspace_db


def dispatch_card(payload: dict, card_id: str, registered) -> str:
    from app.tasks.executor import executor
    task_id = uuid.uuid4().hex[:16]
    run_id = payload["run_id"]
    with workspace_db.transaction() as conn:
        conn.execute("INSERT INTO dispatches(task_id,run_id,card_id,employee_id,payload,state,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,'pending',?,?)",
                     (task_id, run_id, card_id, payload["employee_id"], json.dumps(payload), now_iso(), now_iso()))
        row = conn.execute("SELECT ledger,pending FROM runs WHERE id=?", (run_id,)).fetchone()
        ledger, pending = json.loads(row["ledger"]), json.loads(row["pending"])
        ledger[task_id] = {"employee_id": payload["employee_id"], "status": "pending"}
        if task_id not in pending:
            pending.append(task_id)
        conn.execute("UPDATE runs SET ledger=?,pending=?,updated_at=? WHERE id=?",
                     (json.dumps(ledger), json.dumps(pending), now_iso(), run_id))
        conn.execute("UPDATE run_tasks SET task_id=?,updated_at=? WHERE id=? AND owner_employee_id=?",
                     (task_id, now_iso(), card_id, payload["employee_id"]))
    try:
        record = executor.start(**payload, task_id=task_id, on_registered=registered)
        if not record:
            raise RuntimeError("执行器未接受任务")
    except Exception:
        # A rejected intent must never be replayed after the card has been released.
        from app.workspace.run_store import run_store
        workspace_db.execute("UPDATE dispatches SET state='failed',updated_at=? WHERE task_id=?", (now_iso(), task_id))
        run_store.record_task_result(run_id, task_id, employee_id=payload["employee_id"], status="failed")
        raise
    workspace_db.execute("UPDATE dispatches SET state='dispatched',updated_at=? WHERE task_id=?", (now_iso(), task_id))
    return task_id
