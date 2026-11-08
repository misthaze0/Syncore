"""
run 的存储与状态机 —— 一次协作从「内存字典」变成「一行可寻址的记录」。

★ 为什么必须先有它（ADR 0020 / 施工图 §3）

改造前 run 只活在 `GroupOrchestrator._runs` 这个字典里。于是：

- **进程被杀就再没有人知道它存在过** —— 界面上那个「协作中」的转圈，
  重启后既不会变成失败也不会变成完成，因为压根没有记录可结算。
- 前端拿不到状态：`run_id` 只是一个 UUID，`GET /runs/{id}` 无处可查。
- 「同一个群同时只跑一次协作」只能靠前端把按钮 disable 掉 ——
  而 disable 挡不住两个标签页。

★ 三条纪律

1. **状态只能经 `advance()` 写**。别的模块要改状态就调它；
   它内部是一条 `UPDATE ... WHERE id=? AND status=<from>` 的乐观 CAS，
   并对着 `_TRANSITIONS` 白名单校验。理由与 `tasks/models.py` 的迁移表同一句：
   允许 `completed → running` 不会报错，只会让一次已结束的协作重新转起来。
2. **终态不可迁出**，`expired` 也是终态。重启对账把非终态 run 结算成
   `expired` 之后，迟到的成员汇报**不许**把它复活 ——
   那正是「任务列表里有条记录永远在动」的来源。
3. **失败只告警不抛**（与会话/任务存储同一口径）：一次 run 记账失败
   不该让用户的对话失败。但 `RunAlreadyActive` 是**业务结果**不是故障，
   它必须抛出去让接口层回 409。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from typing import Any, Optional

from app.conversations.models import now_iso
from app.conversations.store import is_valid_thread_id
from app.workspace import events
from app.workspace.board import Board
from app.workspace.db import WorkspaceDB, workspace_db

logger = logging.getLogger(__name__)

# ---------- 状态词表 ----------

STATUS_PLANNED = "planned"
STATUS_RUNNING = "running"
STATUS_INPUT_REQUIRED = "input_required"
STATUS_STALLING = "stalling"
STATUS_REPLAN = "replan"
STATUS_BUDGET_LIMITED = "budget_limited"
STATUS_CONCLUDING = "concluding"
STATUS_COMPLETED = "completed"
STATUS_REVIEW = "review"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

#: 终态：界面上的 spinner 只看它。
TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_REVIEW, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}
)

#: 非终态。双跑约束（`uq_runs_active`）的 WHERE 子句必须与它逐条一致 ——
#: 改一处不改另一处的症状是「数据库允许两个活跃 run，接口层却以为挡住了」。
ACTIVE_STATUSES = frozenset(
    {
        STATUS_PLANNED,
        STATUS_RUNNING,
        STATUS_INPUT_REQUIRED,
        STATUS_STALLING,
        STATUS_REPLAN,
        STATUS_BUDGET_LIMITED,
        STATUS_CONCLUDING,
    }
)

#: 合法迁移白名单。终态一律空集（不可迁出）。
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PLANNED: frozenset(
        {STATUS_RUNNING, STATUS_INPUT_REQUIRED, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}
    ),
    STATUS_RUNNING: frozenset(
        {
            STATUS_INPUT_REQUIRED,
            STATUS_STALLING,
            STATUS_REPLAN,
            STATUS_BUDGET_LIMITED,
            STATUS_CONCLUDING,
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_CANCELLED,
            STATUS_EXPIRED,
        }
    ),
    # 等用户补输入 / 停滞 / 改计划 / 超预算 —— 四条治理态都能回到 running，
    # 也都能被直接判死（用户取消、进程重启对账）。
    STATUS_INPUT_REQUIRED: frozenset(
        {STATUS_RUNNING, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}
    ),
    STATUS_STALLING: frozenset(
        {STATUS_RUNNING, STATUS_REPLAN, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}
    ),
    STATUS_REPLAN: frozenset(
        {STATUS_RUNNING, STATUS_INPUT_REQUIRED, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}
    ),
    STATUS_BUDGET_LIMITED: frozenset(
        {STATUS_RUNNING, STATUS_CONCLUDING, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}
    ),
    # 收尾期间收到新要求可以回到 running，版本号会使旧汇总失效。
    STATUS_CONCLUDING: frozenset({STATUS_RUNNING, STATUS_COMPLETED, STATUS_REVIEW, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}),
    **{s: frozenset() for s in TERMINAL_STATUSES},
    STATUS_REVIEW: frozenset({STATUS_COMPLETED, STATUS_PARTIAL, STATUS_RUNNING}),
    STATUS_PARTIAL: frozenset({STATUS_RUNNING}),
}

ALL_RUN_STATUSES = frozenset(_TRANSITIONS)

#: 对外说法：`input_required` / `stalling` 这类状态在界面上要能被读出来。
STATUS_LABELS = {
    STATUS_PLANNED: "准备中",
    STATUS_RUNNING: "协作中",
    STATUS_INPUT_REQUIRED: "等你补充信息",
    STATUS_STALLING: "卡住了",
    STATUS_REPLAN: "重新分工中",
    STATUS_BUDGET_LIMITED: "触到预算上限",
    STATUS_CONCLUDING: "汇总中",
    STATUS_COMPLETED: "已完成",
    STATUS_REVIEW: "成果待验收",
    STATUS_PARTIAL: "部分交付",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消",
    STATUS_EXPIRED: "已中断（服务重启）",
}


class IllegalRunTransition(RuntimeError):
    """非法状态迁移 —— 抛出来让 bug 写在调用它的这一行，而不是在界面上表现成一个又转起来的进度条。"""


class RunAlreadyActive(RuntimeError):
    """
    这个群已经有一次协作在跑。

    带上了活跃 run 的 id，接口层才能回一句有用的 409
    （「上一次还在跑，run_id=…」），而不是让用户对着「太频繁了」猜。
    """

    def __init__(self, group_id: str, run_id: str) -> None:
        super().__init__(f"group {group_id} already has an active run {run_id}")
        self.group_id = group_id
        self.run_id = run_id


def _json_field(row: dict[str, Any], key: str, default: Any) -> Any:
    """JSON 列的宽容读取：坏 JSON 退回默认值而不是抛（磁盘是可以被手工改坏的）"""
    raw = row.get(key)
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


class RunStore:
    """run 行的读写。状态的**唯一**写入点。"""

    def __init__(self, db: Optional[WorkspaceDB] = None) -> None:
        self._db = db or workspace_db
        #: 看板，接在**同一个连接**上而不是用 `board` 单例。
        #: ★ 为什么不能图省事用单例：单例对着进程级 `workspace_db`（用户真实库），
        #: 而测试注入的是 tmp_path 的新实例。run 收口一旦走单例，
        #: 「结算一个测试 run」就会把用户真库里的卡片判死。
        #: `board` 不 import `run_store`，所以这里可以模块级导入，不成环。
        self._board = Board(db=self._db)
        #: 账本（ledger/pending）是「读整份 JSON → 改 → 写回」，跨两条 SQL。
        #: 今天所有调用点都在同步代码里（事件循环内天然原子），这把锁是给
        #: 阶段 2 的认领轮留的底线 —— 那时会有跨 await 的写入者。
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self._db.enabled

    # ---------- 建与查 ----------

    def create(self, *, group_id: str, instruction: str, mode: str = "negotiated", client_id: str = "") -> str:
        """
        开一次 run，返回 run_id。

        初始状态是 `planned`：拆解还没发生。拆解成功→`running`，
        拆解失败→`input_required`（这条请求没分工，等用户处置）。

        Raises:
            RunAlreadyActive: 这个群已有一次非终态 run（由 partial unique 索引拒掉）。
        """
        if not is_valid_thread_id(group_id):
            raise ValueError(f"非法 group_id: {group_id!r}")
        run_id = uuid.uuid4().hex[:16]
        now = now_iso()
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO runs(id, group_id, instruction, mode, status, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (run_id, group_id, (instruction or "")[:4000],
                     mode if mode in ("negotiated", "direct") else "negotiated",
                     STATUS_PLANNED, now, now),
                )
                if client_id:
                    conn.execute("INSERT INTO message_requests(group_id,client_id,message_id,run_id) VALUES(?,?,'',?)",
                                 (group_id, client_id, run_id))
        except sqlite3.IntegrityError as e:
            # ★ 认列名不认索引名：SQLite 对 unique 索引报的是
            #   `UNIQUE constraint failed: runs.group_id`。
            active = self.active_run_for_group(group_id)
            if active is not None:
                raise RunAlreadyActive(group_id, str(active["id"])) from e
            raise
        # 广播用**现场拼的形状**，不回头查库：run 刚落地时所有字段都还在手上，
        # 多一次 SELECT 只为发一条事件不值得（`advance()` 才需要读回真行）。
        events.run_status(
            {
                "id": run_id,
                "group_id": group_id,
                "instruction": (instruction or "")[:4000],
                "mode": mode if mode in ("negotiated", "direct") else "negotiated",
                "status": STATUS_PLANNED,
                "status_label": STATUS_LABELS[STATUS_PLANNED],
                "terminal": False,
                "created_at": now,
                "updated_at": now,
            }
        )
        return run_id

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        if not is_valid_thread_id(run_id):
            return None
        row = self._db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._shape(row) if row is not None else None

    def _shape(self, row: dict[str, Any]) -> dict[str, Any]:
        from app.workspace.budget import RunBudget
        out = dict(row)
        out["budget"] = RunBudget(self._db).snapshot(str(row["id"]))
        out["ledger"] = _json_field(out, "ledger", {})
        out["pending"] = _json_field(out, "pending", [])
        out["status_label"] = STATUS_LABELS.get(str(out.get("status")), str(out.get("status")))
        out["terminal"] = str(out.get("status")) in TERMINAL_STATUSES
        return out

    def active_run_for_group(self, group_id: str) -> Optional[dict[str, Any]]:
        if not is_valid_thread_id(group_id):
            return None
        row = self._db.query_one(
            "SELECT * FROM runs WHERE group_id = ? AND status IN ({}) "
            "ORDER BY created_at DESC LIMIT 1".format(",".join("?" * len(ACTIVE_STATUSES))),
            (group_id, *sorted(ACTIVE_STATUSES)),
        )
        return self._shape(row) if row is not None else None

    def list_runs(self, group_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if not is_valid_thread_id(group_id):
            return []
        rows = self._db.query_all(
            "SELECT * FROM runs WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
            (group_id, max(1, limit)),
        )
        return [self._shape(r) for r in rows]

    # ---------- 状态：唯一写入点 ----------

    def advance(self, run_id: str, to: str, *, error: str = "") -> Optional[dict[str, Any]]:
        """
        把 run 推进到 `to`。成功返回新行，返回 None 表示**没写成**
        （行不存在 / 已被别人抢先移走）。

        Raises:
            IllegalRunTransition: 当前态到目标态不在白名单里。
        """
        if to not in ALL_RUN_STATUSES:
            raise IllegalRunTransition(f"未知的 run 状态: {to!r}")
        with self._lock:
            row = self.get(run_id)
            if row is None:
                logger.warning("run 不存在，无法推进: run=%s -> %s", run_id, to)
                return None
            current = str(row["status"])
            if to not in _TRANSITIONS.get(current, frozenset()):
                raise IllegalRunTransition(
                    f"run {run_id}: {current} → {to} 不是合法迁移"
                    f"（{current} 只允许 → {sorted(_TRANSITIONS.get(current) or [])}）"
                )
            params: list[Any] = [to, now_iso()]
            sets = ["status = ?", "updated_at = ?"]
            if error:
                sets.append("error = ?")
                params.append(error[:500])
            if to in TERMINAL_STATUSES:
                # 结束时间单独记，不能拿 `updated_at` 顶：账本写入（`register_tasks`
                # / `record_task_result`）同样 bump `updated_at`，用它当「什么时候
                # 结束的」会一路往后漂，界面和审计里的耗时就是假的。
                sets.append("finished_at = ?")
                params.append(now_iso())
            # ★ WHERE 里带上 from 态 = 乐观 CAS：并发两个 advance 只有一个能改到行，
            #   另一个 rowcount=0 返回 None（而不是把状态覆盖成一个既然后到的值）。
            params += [run_id, current]
            changed = self._db.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id = ? AND status = ?",
                tuple(params),
            )
            if changed == 0:
                logger.warning(
                    "run 状态被抢先改走（并发 advance）: run=%s 期望从 %s 到 %s",
                    run_id,
                    current,
                    to,
                )
                return None
            fresh = self.get(run_id)
            # ★ 卡片先于 run 的广播收口：界面读到 `completed` 的那一刻，看板上不许
            #   还有转圈的卡。反过来的话，前端会先渲染一帧「已完成 + 3 张在飞」，
            #   而那正是本次实盘验证里用户拿来问「这算什么」的那幅画。
            if to in TERMINAL_STATUSES:
                self._close_board(run_id, to, error)
            # ★ 广播放在写成功之后、且在锁内：状态改了却没广播是「界面永远停在上一态」，
            #   而两个广播先后颠倒则会让界面先看到 completed 再看到 concluding。
            if fresh is not None:
                events.run_status(fresh)
            return fresh

    def _close_board(self, run_id: str, status: str, error: str) -> None:
        """
        把这次 run 看板上还开着的卡片一起判死（`board.close_unfinished`）。

        ★ 为什么写失败不回滚 run 行
        run 已经终态是事实 —— 成员任务确实收口了。卡片没改成只是「看板还挂着活」，
        而把一次终态迁移退回去会造成更坏的症状：run 又活了，背后却没有任何东西在跑。
        所以这里只把异常记下来，缺的那一帧由用户（或阶段 4 的对账）处置。
        """
        reason = f"本次协作已结束（{STATUS_LABELS.get(status, status)}）"
        if error:
            reason = f"{reason}：{error}"
        try:
            self._board.close_unfinished(run_id, reason=reason)
        except Exception:  # noqa: BLE001
            logger.exception("run 收口时卡片判死失败: run=%s", run_id)

    # ---------- 账本 ----------

    def register_tasks(self, run_id: str, task_ids: list[str]) -> Optional[dict[str, Any]]:
        """
        记下这次 run 派出去的 task_id（`pending` = 还没到终态的那些）。

        ★ 为什么落盘而不是只留在内存
        `pending` 是重启对账的依据：没有它，sweeper 只能猜「这个 run 大概没跑完」，
        而有它才能回答「还差哪几个任务」。
        """
        with self._lock:
            row = self.get(run_id)
            if row is None:
                return None
            pending = list(row["pending"])
            ledger = dict(row["ledger"])
            for task_id in task_ids:
                if task_id not in ledger:
                    ledger[task_id] = {"employee_id": "", "status": "pending"}
                    pending.append(task_id)
            self._write_ledger(run_id, ledger, pending)
            return self.get(run_id)

    def record_task_result(
        self, run_id: str, task_id: str, *, employee_id: str, status: str, result: str = ""
    ) -> bool:
        """
        记一个任务的终态，并从 `pending` 里销账。

        Returns:
            True 表示 pending 已空 —— 调用方据此收尾。
            （**收尾动作不在这里做**：本模块只管账，「谁来汇总结论」是编排器的决定。）
        """
        with self._lock:
            row = self.get(run_id)
            if row is None:
                return False
            if bool(row["terminal"]):
                # 迟到的汇报不许把 expired / completed 的 run 拉回生命周期
                logger.info("run 已终态，忽略迟到汇报: run=%s task=%s %s", run_id, task_id, status)
                return False
            ledger = dict(row["ledger"])
            entry = dict(ledger.get(task_id) or {})
            entry.update({"employee_id": employee_id, "status": status})
            if result:
                entry["result_excerpt"] = result[:200]
            ledger[task_id] = entry
            pending = [t for t in row["pending"] if str(t) != str(task_id)]
            self._write_ledger(run_id, ledger, pending)
            return not pending

    def _write_ledger(self, run_id: str, ledger: dict[str, Any], pending: list[str]) -> None:
        self._db.execute(
            "UPDATE runs SET ledger = ?, pending = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(ledger, ensure_ascii=False),
                json.dumps(pending, ensure_ascii=False),
                now_iso(),
                run_id,
            ),
        )

    # ---------- 轮次与停滞计数（护栏 3 的落点）----------

    def note_claim_round(self, run_id: str) -> Optional[dict[str, Any]]:
        """
        记「发生过一轮认领」。它是 token 账的分母，也是界面上「团队已经问过几次」的数。

        终态的 run 不再累加 —— 迟到的轮次不许把已经结算的 run 又往前推。
        """
        with self._lock:
            row = self.get(run_id)
            if row is None or bool(row["terminal"]):
                return None
            self._db.execute(
                "UPDATE runs SET claim_rounds = claim_rounds + 1, updated_at = ? WHERE id = ?",
                (now_iso(), run_id),
            )
            return self.get(run_id)

    def bump_stall(self, run_id: str) -> int:
        """
        这一轮没产生任何新 owner → `stall_count += 1`，返回累加后的值。

        ★ 与停滞检测（护栏 3）**共用这一个计数器**，不另起第二套。
        「没人认领」和「任务反复失败、协调者反复重开」在用户眼里是同一件事：
        这件事推不动了。分成两套计数会让界面显示「进展正常」，同时后台已经空转四轮；
        合并成一个数之后，调用方拿到 `>= STALL_LIMIT` 就只有一条路可走
        （replan 或判 failed），没有「先按哪套口径」这种纠结。

        新一轮真有人接活时由调用方 `reset_stall()` 归零：计数器数的是**连续**空轮，
        不是历史累计 —— 否则一个跑了 20 轮的正常项目会在第 3 轮被判停滞。
        """
        with self._lock:
            row = self.get(run_id)
            if row is None or bool(row["terminal"]):
                return 0
            self._db.execute(
                "UPDATE runs SET stall_count = stall_count + 1, updated_at = ? WHERE id = ?",
                (now_iso(), run_id),
            )
            fresh = self.get(run_id)
            return int((fresh or {}).get("stall_count") or 0)

    def reset_stall(self, run_id: str) -> None:
        """认领有进展 → 空轮计数归零（数的是连续空轮）。"""
        with self._lock:
            self._db.execute(
                "UPDATE runs SET stall_count = 0, updated_at = ? WHERE id = ? "
                "AND stall_count <> 0",
                (now_iso(), run_id),
            )

    # ---------- 取消 ----------

    def request_cancel(self, run_id: str) -> Optional[dict[str, Any]]:
        """
        用户取消一次协作：把 run 推到 `cancelled`，并把它名下还在跑的任务真掐掉。

        ★ 与阶段 0 的 `executor.abort` 是配套的
        只改 run 状态 = 又在骗人：成员任务照跑。所以这里逐个 abort。
        `concluding`（已经在汇总）不许取消 —— 汇总那条路没有可取消的东西，
        让它跑完并留下结论更诚实。
        """
        row = self.get(run_id)
        if row is None or bool(row["terminal"]):
            return None
        # 先断协程，再落状态：反过来协程会把状态抢先写成 cancelled，
        # 而 `advance` 的 CAS 就会白扑一次（rowcount=0）。
        try:
            from app.tasks.executor import executor
            from app.tasks.store import task_store

            for task_id in row["pending"]:
                # 顺序与 `api/tasks.py::cancel_task` 一致：先落状态再断协程。
                task_store.cancel(str(task_id))
                executor.abort(str(task_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("取消 run 时掐协程失败（状态仍会落 cancelled）: run=%s %s", run_id, e)
        try:
            self._db.execute("UPDATE deliveries SET state='cancelled',updated_at=? WHERE message_id IN "
                             "(SELECT id FROM messages WHERE run_id=?) AND state IN ('pending','read','processing')",
                             (now_iso(), run_id))
            updated = self.advance(run_id, STATUS_CANCELLED, error="用户取消")
            from app.workspace.orchestrator import orchestrator
            for task in (orchestrator._pipelines.pop(run_id, None), orchestrator._finalizers.pop(run_id, None)):
                if task:
                    task.cancel()
            orchestrator._runs.pop(run_id, None)
            return updated
        except IllegalRunTransition as e:
            logger.warning("run 取消被拒: %s", e)
            return None


#: 进程级单例
run_store = RunStore()


__all__ = [
    "ACTIVE_STATUSES",
    "ALL_RUN_STATUSES",
    "IllegalRunTransition",
    "RunAlreadyActive",
    "RunStore",
    "STATUS_LABELS",
    "TERMINAL_STATUSES",
    "run_store",
    "sweep_expired_runs",
]


# ---------- 启动对账 ----------


def sweep_expired_runs(store: Optional[RunStore] = None) -> int:
    """
    把上次进程留下的非终态 run 结算成 `expired`，并在对应群组的時間线上留一句话。

    ★ 为什么必须做（施工图 §3.4 的验收项「永久 spinner 数 = 0」）
    run 的行是持久的，而驱动它的协程随旧进程一起没了。不结算的话，
    界面上会有一行「协作中」永远不动 —— 而那恰好是最让人不敢再点第二次的东西。

    不做自动 resume：成员任务可能已经产生副作用（写了文件、发了邮件），
    重放的后果比「中断，请重试」严重得多。与 `tasks/store.py::sweep_orphans`
    对 `running` 态度一致，理由也同一份。
    """
    store = store or run_store
    if not store.enabled:
        return 0
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    rows = store._db.query_all(
        f"SELECT * FROM runs WHERE status IN ({placeholders})",
        tuple(sorted(ACTIVE_STATUSES)),
    )
    count = 0
    for raw in rows:
        row = store._shape(raw)
        run_id = str(row["id"])
        try:
            store.advance(run_id, STATUS_EXPIRED, error="进程重启，协作中断")
        except IllegalRunTransition as e:  # pragma: no cover - 白名单已覆盖所有非终态
            logger.warning("run 结算失败: run=%s %s", run_id, e)
            continue
        count += 1
        _note_expired(store, row)
    return count


def _note_expired(store: RunStore, row: dict[str, Any]) -> None:
    """在群组时间线上说明「这次协作中断了」，否则用户只看到一个凭空消失的转圈。"""
    try:
        from app.workspace.store import workspace_store

        group_id = str(row.get("group_id") or "")
        if not group_id:
            return
        unfinished = len(row["pending"])
        workspace_store.append_message(
            group_id,
            employee_id="",
            kind="system",
            content=(
                "⏹️ 服务重启，这次协作中断了（不是失败，也没有重放）。\n"
                f"你当时交代的是：{str(row.get('instruction') or '')[:200]}\n"
                f"还有 {unfinished} 个任务没有到终态 —— 已经产生过的副作用不会被撤回，"
                "需要的话请重新发起。"
            ),
            meta={"run_id": str(row["id"]), "status": STATUS_EXPIRED},
        )
        # 广播由 `append_message` 在写完时发（界面上「凭空消失的转圈」就是它要防的）
    except Exception as e:  # noqa: BLE001
        logger.warning("run 中断说明写不进时间线: run=%s %s", row.get("id"), e)
