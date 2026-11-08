"""Shared, persisted budgets for planning, employee execution and reporting."""
from __future__ import annotations

import time
import asyncio
from contextvars import ContextVar
from functools import wraps
from typing import Any

from app.config import settings
from app.workspace.db import WorkspaceDB, workspace_db

current_run: ContextVar[str] = ContextVar("workspace_run", default="")


def run_scoped(function):
    @wraps(function)
    async def wrapped(self, run_id, *args, **kwargs):
        token = current_run.set(run_id)
        try:
            return await function(self, run_id, *args, **kwargs)
        finally:
            current_run.reset(token)
    return wrapped


class BudgetExceeded(RuntimeError):
    pass


class RunBudget:
    def __init__(self, db: WorkspaceDB = workspace_db):
        self.db = db

    def ensure(self, run_id: str) -> None:
        if not run_id:
            return
        self.db.execute(
            "INSERT OR IGNORE INTO run_budgets(run_id,max_calls,max_tokens,max_seconds,max_retries,started) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, settings.WORKSPACE_MAX_LLM_CALLS, settings.WORKSPACE_MAX_TOKENS,
             settings.WORKSPACE_MAX_SECONDS, settings.WORKSPACE_MAX_RETRIES, time.time()),
        )
        self.db.execute("UPDATE runs SET token_budget=(SELECT max_tokens FROM run_budgets WHERE run_id=?), "
                        "wall_limit=(SELECT max_seconds FROM run_budgets WHERE run_id=?) WHERE id=?",
                        (run_id, run_id, run_id))

    def snapshot(self, run_id: str) -> dict[str, Any]:
        return self.db.query_one("SELECT * FROM run_budgets WHERE run_id=?", (run_id,)) or {}

    def notify(self, run_id: str) -> None:
        from app.workspace.run_store import RunStore
        from app.workspace import events
        run = RunStore(self.db).get(run_id)
        if run:
            events.run_status(run)

    def reserve(self, estimate: int, *, run_id: str = "", retry: bool = False) -> tuple[str, int]:
        run_id = run_id or current_run.get()
        if not run_id:
            return "", 0
        self.ensure(run_id)
        estimate = max(1, estimate)
        error = ""
        with self.db.transaction() as conn:
            row = dict(conn.execute("SELECT * FROM run_budgets WHERE run_id=?", (run_id,)).fetchone())
            if time.time() - row["started"] >= row["max_seconds"]:
                error = "协作已达到运行时长上限"
            elif row["calls"] >= row["max_calls"]:
                error = "协作已达到模型调用次数上限"
            elif row["tokens"] + row["reserved"] + estimate > row["max_tokens"]:
                error = "协作剩余 token 预算不足"
            elif retry and row["retries"] >= row["max_retries"]:
                error = "协作已达到重试次数上限"
            if error:
                conn.execute("UPDATE run_budgets SET error=? WHERE run_id=?", (error, run_id))
            else:
                conn.execute("UPDATE run_budgets SET calls=calls+1,reserved=reserved+?,retries=retries+? "
                             "WHERE run_id=?", (estimate, int(retry), run_id))
        if error:
            self.notify(run_id)
            raise BudgetExceeded(error)
        self.notify(run_id)
        return run_id, estimate

    def finish(self, ticket: tuple[str, int], usage: Any = None) -> None:
        run_id, reserved = ticket
        if not run_id:
            return
        usage = usage or {}
        tokens = int(usage.get("total_tokens") or
                     (int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0) +
                      int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)) or reserved)
        self.db.execute("UPDATE run_budgets SET reserved=MAX(0,reserved-?),tokens=tokens+? WHERE run_id=?",
                        (reserved, tokens, run_id))
        self.db.execute("UPDATE runs SET token_used=(SELECT tokens FROM run_budgets WHERE run_id=?) WHERE id=?", (run_id, run_id))
        self.notify(run_id)

    def remaining_seconds(self, run_id: str) -> float:
        row = self.snapshot(run_id)
        return max(0.01, row["max_seconds"] - (time.time() - row["started"])) if row else 60.0


run_budget = RunBudget()


async def budgeted_invoke(llm, messages, *, timeout: float = 120):
    ticket = run_budget.reserve(len(str(messages).encode("utf-8")) // 2 + 2048)
    response = None
    try:
        deadline = min(timeout, run_budget.remaining_seconds(ticket[0])) if ticket[0] else timeout
        response = await asyncio.wait_for(llm.ainvoke(messages), deadline)
        return response
    finally:
        run_budget.finish(ticket, getattr(response, "usage_metadata", None))


async def budgeted_stream(llm, messages, *, run_id: str = "", retry: bool = False):
    """Account failed/partial streams too; release reservations even on cancellation."""
    ticket = run_budget.reserve(len(str(messages).encode("utf-8")) // 2 + 4096, run_id=run_id, retry=retry)
    total = None
    try:
        async with asyncio.timeout(run_budget.remaining_seconds(ticket[0]) if ticket[0] else None):
            async for chunk in llm.astream(messages):
                total = chunk if total is None else total + chunk
                yield chunk
    finally:
        run_budget.finish(ticket, getattr(total, "usage_metadata", None))
