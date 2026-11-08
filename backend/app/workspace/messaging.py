"""Event-driven employee mailboxes. Workers exist only while there is work to deliver."""
from __future__ import annotations

import asyncio
import logging
import json
import re
from datetime import datetime, timezone

from app.config import settings
from app.conversations.models import now_iso
from app.workspace.db import workspace_db
from app.workspace.inbox import inbox
from app.workspace.store import workspace_store

logger = logging.getLogger(__name__)


class Messenger:
    def __init__(self):
        self.workers: dict[str, asyncio.Task] = {}
        self.dirty: set[str] = set()
        self.deadlines: dict[str, asyncio.TimerHandle] = {}
        self.stopping = False

    def arm_deadline(self, message: dict) -> None:
        if not message.get("recipient_employee_id") or message.get("kind") != "request":
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        key = f"{message['id']}:{message['recipient_employee_id']}"
        old = self.deadlines.pop(key, None)
        if old:
            old.cancel()
        delay = settings.WORKSPACE_DELIVERY_TIMEOUT_SECONDS
        if message.get("deadline"):
            delay = max(0, (datetime.fromisoformat(message["deadline"]) - datetime.now(timezone.utc)).total_seconds())
        def expired():
            self.deadlines.pop(key, None)
            self.wake(message["recipient_employee_id"])
        self.deadlines[key] = loop.call_later(delay, expired)

    async def shutdown(self) -> None:
        self.stopping = True
        for timer in self.deadlines.values():
            timer.cancel()
        self.deadlines.clear()
        self.dirty.clear()
        workers = list(self.workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.workers.clear()

    def wake(self, employee_id: str) -> None:
        if self.stopping or not employee_id:
            return
        if employee_id in self.workers:
            self.dirty.add(employee_id)
            return
        try:
            task = asyncio.get_running_loop().create_task(self._drain(employee_id))
        except RuntimeError:
            return
        self.workers[employee_id] = task
        task.add_done_callback(lambda t: self._finished(employee_id, t))

    def _finished(self, employee_id: str, task: asyncio.Task) -> None:
        self.workers.pop(employee_id, None)
        if self.stopping:
            return
        if not task.cancelled() and task.exception():
            logger.error("员工消息投递失败: %s", task.exception())
        if employee_id in self.dirty:
            self.dirty.discard(employee_id)
            self.wake(employee_id)
        from app.workspace.orchestrator import orchestrator
        for run_id in list(orchestrator._runs):
            orchestrator.maybe_continue(run_id)

    def wake_run(self, run_id: str) -> None:
        for message in inbox.pending(run_id=run_id):
            self.wake(str(message["recipient_employee_id"]))

    def busy_task(self, employee_id: str) -> dict | None:
        from app.tasks.store import task_store
        from app.tasks.models import is_terminal
        rows, _ = task_store.list(employee_id=employee_id, limit=100)
        return next((r for r in rows if not is_terminal(str(r.get("status"))) and
                     r.get("status") not in ("interrupted", "orphaned")), None)

    async def _drain(self, employee_id: str) -> None:
        from app.tasks.executor import executor
        from app.workspace.orchestrator import orchestrator
        from app.workspace.models import normalize_base_agent
        from app.workspace.run_store import run_store

        employee = workspace_store.get_employee(employee_id)
        for message in inbox.pending(employee_id):
            if not employee or not employee.get("enabled", True):
                inbox.resolve(message["id"], employee_id, "failed", "员工不存在或已停用")
                continue
            if datetime.fromisoformat(message["deadline"]) <= datetime.now(timezone.utc):
                inbox.resolve(message["id"], employee_id, "failed", "消息处理超时")
                workspace_store.append_message(message["group_id"], employee_id=employee_id, kind="blocker",
                                               content="这条请求已超时，请重新确认是否仍需处理。", reply_to=message["id"])
                continue
            if message["attempts"] >= settings.WORKSPACE_DELIVERY_MAX_ATTEMPTS:
                inbox.resolve(message["id"], employee_id, "failed", "已达到重试上限")
                continue
            run = run_store.get(str(message.get("run_id") or ""))
            if not run or run["terminal"]:
                inbox.resolve(message["id"], employee_id, "cancelled", "所属协作已结束")
                continue
            if self.busy_task(employee_id):
                return  # The active planner reads this mailbox at its next safe boundary.
            options = workspace_db.query_one("SELECT payload FROM run_options WHERE run_id=?", (run["id"],))
            options = json.loads(options["payload"]) if options else {}
            from app.permissions.policy import effective_mode
            def registered(record):
                inbox.db.execute("UPDATE deliveries SET state='processing',task_id=?,attempts=attempts+1,updated_at=? "
                                 "WHERE message_id=? AND employee_id=?",
                                 (record["task_id"], now_iso(), message["id"], employee_id))
                orchestrator.track_task(run["id"], record["task_id"])

            record = executor.start(thread_id=message["group_id"], agent=normalize_base_agent(employee.get("base_agent")),
                           title="处理群内请求", instruction=message["content"], employee_id=employee_id,
                           group_id=message["group_id"], run_id=run["id"], source="employee_message",
                           model=employee.get("model") or options.get("model") or "", on_registered=registered,
                           web=bool(options.get("web", True)), graph=bool(options.get("graph", True)),
                           permission_mode=str(options.get("permission_mode") or effective_mode()))
            if not record:
                inbox.db.execute("UPDATE deliveries SET attempts=attempts+1,last_error='执行器暂不可用' WHERE message_id=?",
                                 (message["id"],))
                self.dirty.add(employee_id)
            return

    def task_done(self, task: dict) -> None:
        eid, tid = str(task.get("employee_id") or ""), str(task.get("task_id") or "")
        if not eid:
            return
        for message in inbox.pending(eid):
            if message["delivery_task_id"] != tid:
                continue
            if task.get("status") == "succeeded":
                inbox.resolve(message["id"], eid)
            elif task.get("status") in ("failed", "cancelled", "orphaned"):
                inbox.resolve(message["id"], eid, "failed", str(task.get("error") or "任务未完成"))
        self.wake(eid)

    async def send_user(self, group_id: str, content: str, *, recipient_employee_id: str = "",
                        reply_to: str = "", intent: str = "auto", client_id: str = "") -> dict:
        from app.workspace.orchestrator import orchestrator
        from app.workspace.run_store import run_store

        content = content.strip()
        if not content:
            raise ValueError("消息不能为空")
        members = [m for m in workspace_store.list_members(group_id) if m.get("enabled", True)]
        if not members:
            raise ValueError("请先添加可用的数字员工")
        if client_id:
            prior = workspace_db.query_one("SELECT * FROM message_requests WHERE group_id=? AND client_id=?", (group_id, client_id))
            if prior:
                return {"message_id": prior["message_id"], "run_id": prior["run_id"]}
        if reply_to:
            parent = workspace_store.get_message(reply_to)
            if not parent or parent["group_id"] != group_id:
                raise ValueError("回复的消息不属于当前群")
            recipient_employee_id = recipient_employee_id or str(parent.get("employee_id") or "")
        if not recipient_employee_id:
            mentioned = [m for m in members if re.search(r"@" + re.escape(m["name"]) + r"(?=\s|[，,：:]|$)", content)]
            if len(mentioned) == 1:
                recipient_employee_id = mentioned[0]["id"]
        if recipient_employee_id and not any(m["id"] == recipient_employee_id for m in members):
            raise ValueError("收件人不是当前群的可用成员")
        progress_query = not recipient_employee_id and bool(re.fullmatch(r"[现在目前请问\s]*(进度|做到哪了|做得怎么样|还要多久)[？?。!！\s]*", content))
        run = run_store.active_run_for_group(group_id)
        if intent == "task" and run:
            raise ValueError("当前协作仍在执行；可以发送补充消息，或结束后新建任务")
        if run and run["status"] == "concluding" and not progress_query:
            context = orchestrator._runs.get(run["id"])
            if context:
                context["conclusion_version"] = int(context.get("conclusion_version", 0)) + 1
                context["finalized"] = False
                run_store.advance(run["id"], "running")
                conclusion = orchestrator._finalizers.get(run["id"])
                if conclusion:
                    conclusion.cancel()
        if not run:
            conversational = intent == "message" or (intent == "auto" and bool(re.fullmatch(r"(你好|您好|嗨|在吗|谢谢|辛苦了|好的|收到|hello|hi)[！!。.?？\s]*", content, re.IGNORECASE)))
            target = recipient_employee_id or (members[0]["id"] if conversational else "")
            run_id = await orchestrator.start(group_id, content, target_employee_id=target, client_id=client_id, conversational=conversational)
            goal = workspace_db.query_one("SELECT id FROM messages WHERE group_id=? AND run_id=? AND sender_kind='user' ORDER BY rowid DESC LIMIT 1", (group_id, run_id))
            mid = goal["id"] if goal else ""
        else:
            run_id = run["id"]
            # Plain progress questions are answered from persisted facts without waking every model.
            if progress_query:
                msg = workspace_store.append_message(group_id, kind="decision", sender_kind="user", content=content, run_id=run_id,
                                                     meta={"client_id": client_id} if client_id else {})
                mid = msg["id"]
                cards = orchestrator.cards_for_run(run_id)
                text = "；".join(f"{c['title']}：{c['status_label']}" for c in cards) or run["status_label"]
                workspace_store.append_message(group_id, kind="summary", content=text, run_id=run_id, reply_to=mid)
            else:
                targets = [m for m in members if m["id"] == recipient_employee_id] if recipient_employee_id else members
                # User amendments are addressed to all affected employees, not interpreted as parallel new goals.
                msg = workspace_store.append_message(group_id, kind="request", sender_kind="user", content=content,
                                                     run_id=run_id, recipient_employee_id=recipient_employee_id, reply_to=reply_to,
                                                     recipient_employee_ids=[member["id"] for member in targets],
                                                     meta={"client_id": client_id} if client_id else {})
                mid = msg["id"]
                for member in targets:
                    self.wake(member["id"])
        if client_id:
            workspace_db.execute("INSERT OR IGNORE INTO message_requests(group_id,client_id,message_id,run_id) VALUES(?,?,?,?)",
                                 (group_id, client_id, mid, run_id))
        orchestrator.maybe_continue(run_id)
        return {"message_id": mid, "run_id": run_id}


messenger = Messenger()
