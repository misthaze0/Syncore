"""
任务板 —— 一次 run 名下的卡片：状态层，不是执行层。

★ 为什么必须是一张新表，而不是给 `TaskRecord` 加几个字段（施工图 §1.1 / §2.4）

`tasks/store.py::patch` 是「读整条 → 改 → 追加一行 → 整写 index.json」。
两条纪律在这份存储里**表达不出来**：

1. **认领互斥（护栏 2）**。两个协程同时 claim 只能由「谁最后写 index.json」决定，
   那是 LWW 不是互斥。卡片必须落在 SQLite 上，靠一条带条件的 UPDATE 去抢。
2. **看板高频写**。每次 patch 都重写整份索引，看板一旦频繁动，
   「查任务列表」就变成 IO 竞争者。

★ 三条不可绕过的规矩

1. **`claim()` 是唯一能给卡片写上 owner 的地方**（建卡 ≠ 指派，ADR 0020）。
   `advance_card()` 只有一种情况下碰 owner：把卡片推回 `todo` 时把它**清空**。
   协调者、界面都不许指名某人。
2. 卡片状态只有 `claim()` 和 `advance_card()` 能写，两者都要过迁移白名单，
   且都是 `WHERE id=? AND status=<from>` 的乐观 CAS。库里的列**没有 CHECK**，
   白名单是唯一的闸，绕过它就等于让「已完成的卡重新转起来」成为可能。
3. `project_status()` 是 `TaskRecord.status` → 卡片状态的**唯一**翻译处。
   界面里将来一定有人写「`task.status == 'running'` 就显示 in_progress」，
   而那张卡可能已经是 `blocked`（用户叫停了，协程还没退出）。

★ 两份真相怎么缝

`run_tasks`（看板 / 状态层）与 `TaskRecord`（JSONL / 执行真相）各自成立，
靠 `TaskRecord.board_card_id` 这一对键缝合，缝合后的写入收敛到 `sync_from_task()`。
代价是「一处状态两处落」，换来回护栏 2 的可表达性 —— 这是施工图里明写的取舍，不是遗漏。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.config import settings
from app.conversations.models import now_iso
from app.conversations.store import is_valid_thread_id
from app.tasks.models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_ORPHANED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_WAITING_APPROVAL,
)
from app.workspace import events
from app.workspace.db import WorkspaceDB, workspace_db

logger = logging.getLogger(__name__)

# ---------- 看板词表（≠ 任务词表，见 §2.8） ----------

CARD_TODO = "todo"
CARD_IN_PROGRESS = "in_progress"
CARD_BLOCKED = "blocked"
CARD_REVIEW = "review"
CARD_DONE = "done"
CARD_CANCELLED = "cancelled"

CARD_STATUSES: tuple[str, ...] = (
    CARD_TODO,
    CARD_IN_PROGRESS,
    CARD_BLOCKED,
    CARD_REVIEW,
    CARD_DONE,
    CARD_CANCELLED,
)

#: 终态。状态机意义上的「不可迁出之外」与 `terminal` 标记只看它。
TERMINAL_CARD_STATUSES = frozenset({CARD_DONE, CARD_CANCELLED})

#: 「这批的活已经交了」—— 批次推进与依赖判定看它，**不是**看上面的终态。
#:
#: ★ 为什么必须把 `review` 算进来（2026-10-16 真实跑出来的死锁）
#: 员工跑完一张卡的合法落点是 `review`（不是 `done`，阶段 3 之前「完成必须有工件」
#: 还没有实施者），而 `review → done` 要等用户验收。若批次推进只认终态，那么
#: **任何两张卡的计划都永远走不到第二张**：批 0 全部停在 review → `current_batch`
#: 永远返回 0 → 批 1 never 被摊出去，而 run 已经在成员任务收口时结束了。
#: 实测症状：任务板上留一张「待认领」，群聊里协调者已经写了综合结论，run 显示「已完成」。
#: 依赖方要的是**产物**（已经交了），不是用户的签字，所以这里用 settled 而不是 terminal。
SETTLED_CARD_STATUSES = frozenset({CARD_DONE, CARD_REVIEW})

#: run 落终态时要弃掉判死的态。
#:
#: ★ 为什么 `blocked` **不在**这一档（`test_bleeding_stops.py` 钉着它）
#: 停滞护栏与拆解失败那条路，正是把没人认领的卡写成 `blocked` + 一句「为什么」
#: （「没有成员认领」/「模型没有拆出任何卡片」）。那是留给用户的**证据**。
#: 收口若把它一起抹成 `cancelled`，写进去的会是「本次协作已结束」，
#: 于是那句唯一能指导用户下一步的话被覆盖掉 —— 界面从「卡住，因为没人接」
#: 变成「已取消」，用户什么也学不到。
#: `todo` / `in_progress` 没有这个问题：它们身上没有任何诊断信息，只是两个还在转的谎。
ABANDONED_CARD_STATUSES = frozenset({CARD_TODO, CARD_IN_PROGRESS})

#: 在飞的卡 —— 认领那条 SQL 的负载子查询数的是它，`inflight_count()` 也必须数它，
#: 两处口径不一致的话「限流」和「界面显示的任务数」会对不上。
INFLIGHT_CARD_STATUSES = frozenset({CARD_IN_PROGRESS})

#: 合法迁移白名单。终态一律空集（不可迁出）。
CARD_TRANSITIONS: dict[str, frozenset[str]] = {
    CARD_TODO: frozenset({CARD_IN_PROGRESS, CARD_BLOCKED, CARD_CANCELLED}),
    # ★ `in_progress → todo` 是「释放」：抢到卡但**没派出去**（员工被停用、
    # 执行器拒收、进程刚好在这儿崩）时，卡片必须回到待认领。
    # 少了这条路，那张卡会永远停在 in_progress 且 `task_id` 为空 ——
    # 界面上是一个转到天荒地老的 spinner，而背后没有任何东西在跑，
    # 正是本次改造要根除的那个症状（见 ADR 0020 的「永久 spinner」）。
    # `advance_card()` 在 `to == todo` 时把 owner 与 task_id 一起归零，
    # 所以释放不会留下一张「有主人但没人做」的死卡。
    CARD_IN_PROGRESS: frozenset(
        {CARD_REVIEW, CARD_DONE, CARD_BLOCKED, CARD_CANCELLED, CARD_TODO}
    ),
    # blocked 是「需要人来处置」的态：解了锁就推回待认领（`advance_card` 会把
    # owner 一起归零，见那里的注释），协调者判定依赖恢复也可以直接推回在飞，
    # 或者干脆判死；不许它自己「变成已完成」。
    CARD_BLOCKED: frozenset(
        {CARD_TODO, CARD_IN_PROGRESS, CARD_REVIEW, CARD_CANCELLED}
    ),
    # 验收不过要打回重做（review→in_progress），所以 review 不是准终态。
    CARD_REVIEW: frozenset({CARD_DONE, CARD_TODO, CARD_IN_PROGRESS, CARD_BLOCKED, CARD_CANCELLED}),
    CARD_DONE: frozenset({CARD_TODO}),  # Explicit upstream rework invalidates prior acceptance.
    CARD_CANCELLED: frozenset(),
}

#: 界面上的说法。`review` 与 `done` 的区别必须能被读出来 —— 阶段 3 之前
#: 「完成必须有工件」这条不变量还没有实现者，把 succeeded 说成 done 是撒谎。
CARD_STATUS_LABELS = {
    CARD_TODO: "待认领",
    CARD_IN_PROGRESS: "进行中",
    CARD_BLOCKED: "卡住了",
    CARD_REVIEW: "待验收",
    CARD_DONE: "已完成",
    CARD_CANCELLED: "已取消",
}

CARD_PRIORITIES = ("high", "normal", "low")

#: 建卡的三条合法来源 + 拆解失败那张（都靠 `created_by` 区分，见 §5.4）
CREATED_BY_COORDINATOR = "coordinator"
CREATED_BY_EMPLOYEE = "employee"
CREATED_BY_USER = "user"
CREATED_BY_SYSTEM = "system"
CARD_CREATORS = (
    CREATED_BY_COORDINATOR,
    CREATED_BY_EMPLOYEE,
    CREATED_BY_USER,
    CREATED_BY_SYSTEM,
)

CARD_TITLE_CHARS = 60
BLOCKED_REASON_CHARS = 200


class IllegalCardTransition(RuntimeError):
    """非法卡片迁移 —— 抛出来让 bug 落在调用它的那一行，而不是在界面上变成一个又转起来的进度条。"""


class CardDependencyError(ValueError):
    """
    依赖不成立（自依赖 / 同批互等 / 依赖不存在）。

    必须在**建卡时**拒掉：静默丢掉一条越界依赖 = 让这张卡跑在它的前提之前，
    那正是「分解漂移」里最贵的一种 —— 产出看着像真的，但基于的是旧事实。
    """


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def new_card_id() -> str:
    """
    卡片 id 的唯一产出处。

    ★ 为什么暴露给协调者：拆解轮要在**建卡之前**就知道每张卡的 id，
    否则表达不出「c2 依赖 c1」这种批内依赖图（id 若由 `create` 现场生成，
    依赖字段就只能引用一个还没存在的值，或者反过来靠标题猜——那是个歧义源）。
    拿 id 的权力不等于写 owner 的权力，这条界限是清楚的。
    """
    return _new_id()


def _json_field(row: Mapping[str, Any], key: str, default: Any) -> Any:
    """JSON 列的宽容读取：坏 JSON 退回默认值而不是抛（磁盘是可以被手工改坏的）"""
    raw = row.get(key)
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


# ---------- 唯一投影：TaskRecord.status → 卡片状态 ----------

#: 任务侧状态 → 看板侧状态。**别处不得自行推断**（§2.8）。
_TASK_TO_CARD: dict[str, str] = {
    # 已认领但还没跑起来 / 在跑 / 在等人审批 —— 看板上都是「这张卡有人在飞」。
    STATUS_PENDING: CARD_IN_PROGRESS,
    STATUS_RUNNING: CARD_IN_PROGRESS,
    STATUS_WAITING_APPROVAL: CARD_IN_PROGRESS,
    # ★ 不是 done：工件库属阶段 3，「完成必须有工件」这条不变量还没有实现者。
    #   执行成功 = 产出待验收，这一步区分开了才骗不了人。
    STATUS_SUCCEEDED: CARD_REVIEW,
    STATUS_FAILED: CARD_BLOCKED,
    STATUS_CANCELLED: CARD_CANCELLED,
    # 进程重启留下的孤儿：是「中断」不是「失败」，但看板上都需要人处置。
    STATUS_ORPHANED: CARD_BLOCKED,
}


def project_status(task: Mapping[str, Any]) -> str:
    """
    `TaskRecord` 的状态翻译成看板状态。

    未知 / 缺失的任务状态退回 `todo` —— 诚实的默认值是「这张卡还没人认领」，
    而不是替一个我们不认识的状态编出一个看起来像进度的值。
    """
    return _TASK_TO_CARD.get(str(task.get("status") or ""), CARD_TODO)


def project_reason(task: Mapping[str, Any]) -> str:
    """卡片进入 `blocked` 时该写的那句理由（与投影同处产出，免得两边各编一套）。"""
    status = str(task.get("status") or "")
    if status == STATUS_ORPHANED:
        return "进程重启，执行中断（不是失败，也没有重放）"
    if status == STATUS_FAILED:
        return str(task.get("error") or "").strip()[:BLOCKED_REASON_CHARS] or "执行失败"
    return ""


class Board:
    """`run_tasks` 的读写。卡片状态与 owner 的**唯一**写入点。"""

    def __init__(self, db: Optional[WorkspaceDB] = None) -> None:
        self._db = db or workspace_db

    @property
    def enabled(self) -> bool:
        return self._db.enabled

    # ---------- 建与查 ----------

    def create(
        self,
        run_id: str,
        *,
        title: str,
        brief: Any = None,
        batch: int = 0,
        depends_on: Sequence[str] = (),
        acceptance: str = "",
        priority: str = "normal",
        deadline: str = "",
        handoff_depth: int = 0,
        created_by: str = CREATED_BY_COORDINATOR,
        card_id: str = "",
    ) -> dict[str, Any]:
        """
        落一张卡，返回成形后的行。

        ★ 没有 `owner_employee_id` 参数 —— 这是刻意的（建卡 ≠ 指派）。
        任何想让某个人做这张卡的意图，都得走 `claim()` 那条 CAS，
        或者由本人在认领轮里自己说他要。

        Raises:
            ValueError: run 不存在 / 标题为空 —— 都是规划层的 bug，不该落成一条卡片。
            CardDependencyError: 见 `create_cards()`。
        """
        title = str(title or "").strip()[:CARD_TITLE_CHARS]
        if not title:
            raise ValueError("卡片没有标题 —— 看板上会出现一张读不出来的卡")
        if not is_valid_thread_id(run_id) or self._run_exists(run_id) is False:
            raise ValueError(f"卡片必须挂在存在的 run 下: run={run_id!r}")
        cid = (card_id or _new_id()).strip()
        if not is_valid_thread_id(cid):
            raise ValueError(f"非法卡片 id: {cid!r}")
        try:
            self._db.execute(
                "INSERT INTO run_tasks(id, run_id, title, brief, status, priority, batch, "
                "depends_on, acceptance, artifact_ids, handoff_depth, deadline, "
                "blocked_reason, created_by, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
                (
                    cid,
                    run_id,
                    title,
                    json.dumps(_normalize_brief(brief), ensure_ascii=False),
                    CARD_TODO,
                    priority if priority in CARD_PRIORITIES else "normal",
                    max(0, int(batch or 0)),
                    json.dumps(_clean_ids(depends_on), ensure_ascii=False),
                    str(acceptance or "")[:BLOCKED_REASON_CHARS * 4],
                    "[]",
                    max(0, int(handoff_depth or 0)),
                    str(deadline or "")[:40],
                    created_by if created_by in CARD_CREATORS else CREATED_BY_COORDINATOR,
                    now_iso(),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError as e:
            # FK 违约（run 被并发删了）/ 主键重复：都不该被吞成「建卡成功但表里没有」。
            raise ValueError(f"卡片写入被数据库拒绝: run={run_id} title={title!r} ({e})") from e
        row = self.get(cid)
        if row is None:  # pragma: no cover - 写成功却读不到，只可能是库坏了
            raise ValueError(f"卡片写入后读不到: {cid}")
        events.card_created(row, db=self._db)
        return row

    def create_cards(self, run_id: str, cards: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """
        一次建一批卡（协调者的拆解结果本来就是「一批」）。

        ★ 为什么要这个入口，而不是让调用方循环 `create()`

        「批内不得互相 `depends_on`」（§5.3）只有在看**整批**时才判得出来：
        循环建卡时前一张刚落地、后一张依赖它，逐条看每一步都合法，
        实际却造出「同批互等」的死结 —— 批次推进要求本批全到终态，
        而互等的两张卡谁也等不到对方终态。所以这条闸放在建卡时，不留给运行时。

        Raises:
            CardDependencyError: 自依赖 / 同批依赖 / 依赖不存在 / 依赖指向更晚的批次。
        """
        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in cards:
            card = dict(raw)
            cid = card.pop("card_id", "") or card.pop("id", "") or _new_id()
            if cid in seen:
                raise CardDependencyError(f"同一次建卡里 id 重复: {cid}")
            seen.add(cid)
            card["card_id"] = cid
            prepared.append(card)

        batch_of = {c["id"]: int(c["batch"]) for c in self.list_cards(run_id)}
        batch_of.update({c["card_id"]: int(c.get("batch") or 0) for c in prepared})
        for card in prepared:
            own = int(card.get("batch") or 0)
            for dep in _clean_ids(card.get("depends_on") or ()):
                if dep == card["card_id"]:
                    raise CardDependencyError(f"卡片 {card['card_id']} 依赖自己")
                if dep not in batch_of:
                    raise CardDependencyError(
                        f"卡片 {card['card_id']} 依赖不存在的 {dep} —— "
                        "丢掉这条依赖会让它跑在前提之前"
                    )
                if batch_of[dep] >= own:
                    raise CardDependencyError(
                        f"卡片 {card['card_id']}(批次 {own}) 依赖 {dep}(批次 {batch_of[dep]})："
                        "同批必须并行、依赖只能指向更早的批次（§5.3）"
                    )
        return [self.create(run_id, **card) for card in prepared]

    def get(self, card_id: str) -> Optional[dict[str, Any]]:
        if not is_valid_thread_id(card_id):
            return None
        row = self._db.query_one("SELECT * FROM run_tasks WHERE id = ?", (card_id,))
        return self._shape(row) if row is not None else None

    def list_cards(
        self,
        run_id: str,
        *,
        status: str = "",
        batch: Optional[int] = None,
        owner_employee_id: str = "",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM run_tasks WHERE run_id = ?"
        params: list[Any] = [run_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if batch is not None:
            sql += " AND batch = ?"
            params.append(int(batch))
        if owner_employee_id:
            sql += " AND owner_employee_id = ?"
            params.append(owner_employee_id)
        # ★ `rowid` 而不是 `created_at`：`create_cards()` 一轮写完一批卡，
        # 相邻两行常落在同一毫秒里（now_iso 只到毫秒），此时 created_at 相等，
        # 而下一级的 `id` 是随机 hex —— 排序结果就成了掷骰子，看板会把协调者
        # 排出来的计划顺序打乱。rowid 是本项目唯一可用的插入序游标（与 messages.seq 同）。
        sql += " ORDER BY batch ASC, rowid ASC"
        rows = self._db.query_all(sql, tuple(params))
        return [self._shape(r) for r in rows]

    def _shape(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        out["brief"] = _json_field(out, "brief", {})
        out["depends_on"] = _json_field(out, "depends_on", [])
        out["artifact_ids"] = _json_field(out, "artifact_ids", [])
        out["status_label"] = CARD_STATUS_LABELS.get(str(out.get("status")), str(out.get("status")))
        out["terminal"] = str(out.get("status")) in TERMINAL_CARD_STATUSES
        return out

    def _run_exists(self, run_id: str) -> Optional[bool]:
        """None = 库不可用；否则 run 行在不在。"""
        if not self._db.enabled:
            return None
        return self._db.query_one("SELECT id FROM runs WHERE id = ?", (run_id,)) is not None

    # ---------- 认领：护栏 2 的唯一实施点 ----------

    def claim(
        self,
        card_id: str,
        *,
        employee_id: str,
        task_id: str,
        batch: Optional[int] = None,
        max_inflight: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """
        抢一张卡：一次条件写（施工图 §5.2）。成功返回新行，`None` = 没抢到。

        ★ 为什么负载上限必须在**同一条**语句里

        「先查在飞数、再抢卡」在认领轮里等于没查：那几个协程是并发跑的，
        查的时候都有空位，写的时候一个人挤了两个。SQLite 单写者语义只保证
        一条语句内原子，所以 `§5.2` 那条子查询是必须的，不是洁癖。

        抢不到**不是错误**：卡片不消失（v1「抢不到就把卡删了重来」的病根），
        调用方换下一条或 `decline`。要区分「被别人抢了」还是「自己负载满了」，
        读 `get(card_id)["owner_employee_id"]` 与 `inflight_count()` —— 这两个数
        本来就在界面上要显示，不必在这里额外发明一套返回码。

        Args:
            batch: 认领轮摊这张卡时它所在的批次。给了就写进 WHERE，
                于是「摊卡 → 抢卡」之间发生了改批也不会抢错（本阶段无人改批，
                但这道闸不花钱）。不给则取卡当前批次。
        """
        if batch is None:
            card = self.get(card_id)
            if card is None:
                return None
            batch = int(card["batch"])
        cap = (
            settings.BOARD_MAX_INFLIGHT_PER_EMPLOYEE
            if max_inflight is None
            else int(max_inflight)
        )
        if cap <= 0:
            return None
        inflight = sorted(INFLIGHT_CARD_STATUSES)
        holders = ",".join("?" * len(inflight))
        changed = self._db.execute(
            "UPDATE run_tasks "
            "   SET owner_employee_id = ?, task_id = ?, status = ?, updated_at = ? "
            " WHERE id = ? AND owner_employee_id = '' AND status = ? AND batch = ? "
            "   AND (SELECT COUNT(*) FROM run_tasks "
            f"         WHERE owner_employee_id = ? AND status IN ({holders})) < ?",
            (
                employee_id,
                task_id,
                CARD_IN_PROGRESS,
                now_iso(),
                card_id,
                CARD_TODO,
                int(batch),
                employee_id,
                # ★ 负载子查询与 `inflight_count()` 用同一个谓词、同一个常量来源：
                #   口径分叉会让「限流拦住的」和「界面显示的」是两个数。
                *inflight,
                cap,
            ),
        )
        if changed == 0:
            logger.info("认领未成功（被抢 / 负载满 / 批次不符）: card=%s emp=%s", card_id, employee_id)
            return None
        row = self.get(card_id)
        if row is not None:
            events.card_claimed(row, db=self._db)
        return row

    def attach_task(
        self, card_id: str, *, employee_id: str, task_id: str
    ) -> Optional[dict[str, Any]]:
        """
        把派发出来的 `task_id` 挂回卡片上。**不改变状态**。

        ★ 为什么 `claim()` 里不带 task_id 一次写完

        认领轮的正确次序是「先抢卡，再派活」（§16.1）：反过来先派活再抢卡，
        抢输的那个员工已经白跑了一次模型（几十秒和一堆 token），
        而抢赢的那张卡上还留着**两个** task_id —— 界面分不清该看哪条执行真相。
        代价是中间存在一个极短的窗口：卡片已 `in_progress` 但 `task_id` 还是空。
        这个窗口是可见的（`task_id=''` 就写在行上），重启对账时据此能判出
        「认领了但没派出去」，比让抢输的人白跑要便宜。

        谓词带 `owner_employee_id = ?` 与 `task_id = ''`：
        别人抢不走、也不许把已经挂上的执行真相换掉（那正是两份真相错位的开始）。
        """
        if not task_id:
            return None
        changed = self._db.execute(
            "UPDATE run_tasks SET task_id = ?, updated_at = ? "
            " WHERE id = ? AND owner_employee_id = ? AND (task_id = '' OR task_id = ?)",
            (task_id, now_iso(), card_id, employee_id, task_id),
        )
        if changed == 0:
            logger.info("挂载 task 未成功（卡不存在 / 不是他的 / 已挂过）: card=%s task=%s", card_id, task_id)
            return None
        row = self.get(card_id)
        if row is not None:
            # ★ 状态没动，但卡片变了 —— 广播的是**整份快照**，
            # `previous_status == status` 就是「没有迁移，只是链接变了」的记号。
            # 不推这一发的症状：看板上这张卡显示「进行中」，
            # 却直到任务结束才第一次带上 task_id —— 用户点不进工作台看它在干什么。
            events.card_status(row, str(row["status"]), db=self._db)
        return row

    # ---------- 交接深度（护栏 5） ----------

    def handoff_depth(self, card_id: str) -> int:
        row = self.get(card_id)
        return int((row or {}).get("handoff_depth") or 0)

    def handoff(self, card_id: str) -> Optional[dict[str, Any]]:
        """
        记一次「这张卡被建议转手」：`handoff_depth += 1`，**不写 owner、不改状态**。

        ★ 为什么它动不了 owner
        「我觉得这该给小李」是同事的一句话，不是一次指派。写上 owner 就把
        ADR 0020 的「建卡 ≠ 指派」从后门放回来了 —— 而这张卡照旧要小李自己在
        认领轮里抢（CAS 那条闸一个都不为它放宽）。

        谓词带 `status = todo AND owner_employee_id = ''`：卡被人接走之后就不再是
        「开放待认领」，这时再给它累加深度，护栏 5 会去限一道根本没在转手的卡。
        """
        changed = self._db.execute(
            "UPDATE run_tasks SET handoff_depth = handoff_depth + 1, updated_at = ? "
            " WHERE id = ? AND status = ? AND owner_employee_id = ''",
            (now_iso(), card_id, CARD_TODO),
        )
        if changed == 0:
            logger.info("转手未记录（卡不存在 / 已被认领 / 已收口）: card=%s", card_id)
            return None
        return self.get(card_id)

    # ---------- 状态：唯一写入点 ----------

    def advance_card(
        self,
        card_id: str,
        to: str,
        *,
        blocked_reason: str = "",
        task_id: str = "",
    ) -> Optional[dict[str, Any]]:
        """
        把卡片推进到 `to`。返回 None 表示**没写成**（卡不存在 / 被别人抢先移走）。

        ★ `blocked_reason` 每次迁移都写（默认空串）：状态与理由不许各留一份 ——
        离开 `blocked` 时把旧理由留在库里，下一轮界面就会把「上次为什么卡」
        当成「这次为什么卡」。

        ★ 推回 `todo` 时 owner 与 task_id 一起归零，这是唯一允许本方法碰 owner 的情况
        （写上 owner 仍然只有 `claim()` 能做到）。

        Raises:
            IllegalCardTransition: 当前态到目标态不在白名单里。
        """
        if to not in CARD_STATUSES:
            raise IllegalCardTransition(f"未知的卡片状态: {to!r}")
        row = self.get(card_id)
        if row is None:
            logger.warning("卡片不存在，无法推进: card=%s -> %s", card_id, to)
            return None
        current = str(row["status"])
        if to not in CARD_TRANSITIONS.get(current, frozenset()):
            raise IllegalCardTransition(
                f"卡片 {card_id}: {current} → {to} 不是合法迁移"
                f"（{current} 只允许 → {sorted(CARD_TRANSITIONS.get(current) or [])}）"
            )
        sets = ["status = ?", "blocked_reason = ?", "updated_at = ?"]
        params: list[Any] = [to, blocked_reason[:BLOCKED_REASON_CHARS], now_iso()]
        # ★ 追加顺序必须与 `sets` 一致：位置列的参数错一位不会报错，
        #   只会把 task_id 写进 updated_at、把时间写进 task_id（本项目踩过）。
        if task_id:
            sets.append("task_id = ?")
            params.append(task_id)
        if to == CARD_TODO:
            # ★ 回到待认领必须把 owner 一起归零（写成字面量，不参与上面的位置对齐）。
            # 留着 owner 的 `todo` 是一张**死卡**：`ready_cards()` 不摊它（要求 owner 为空），
            # `claim()` 也抢不动（同样要求 owner 为空）—— 于是它挡住整批却谁也不能做，
            # 最后由停滞检测把这个 run 判死。
            sets.append("owner_employee_id = ''")
            if not task_id:
                sets.append("task_id = ''")
        params += [card_id, current]
        changed = self._db.execute(
            f"UPDATE run_tasks SET {', '.join(sets)} WHERE id = ? AND status = ?",
            tuple(params),
        )
        if changed == 0:
            logger.warning(
                "卡片状态被抢先改走（并发迁移）: card=%s 期望从 %s 到 %s", card_id, current, to
            )
            return None
        row = self.get(card_id)
        if row is not None:
            # 带 `previous_status`：前端据此判断这是不是一次真迁移（`todo → in_progress`
            # 值得高亮一下，而重复的 `in_progress → in_progress` 不该有动画）。
            events.card_status(row, current, db=self._db)
        return row

    # ---------- run 收口 ----------

    def close_unfinished(self, run_id: str, *, reason: str) -> int:
        """
        run 落终态时把**还挂着转圈的卡片**弃掉判死，返回真正改写的张数。

        ★ 为什么 `review` 与 `blocked` 都不动
        `review` 上已经有产物了，把它抹成「已取消」等于毁掉用户唯一能验收的东西；
        `blocked` 上带着「为什么停在这」那句诊断（理由见 `ABANDONED_CARD_STATUSES`）。
        于是这里只动 `todo / in_progress` —— 两个态在 `CARD_TRANSITIONS` 里都允许
        → `cancelled`，所以这段不会撞 `IllegalCardTransition`。

        ★ 逐张走 `advance_card()`
        它是卡片状态的唯一写入点，也就是一张卡改对了界面就必然知道。
        自己拼 SQL 省掉两次函数调用，代价是看板上留一片转圈而群聊已经散场。
        """
        closed = 0
        for card in self.list_cards(run_id):
            if str(card["status"]) not in ABANDONED_CARD_STATUSES:
                continue
            if self.advance_card(str(card["id"]), CARD_CANCELLED, blocked_reason=reason):
                closed += 1
        if closed:
            logger.info("run 收口：弃掉未完成卡片 %s 张 (run=%s)", closed, run_id)
        return closed

    # ---------- 批次推进（§5.3） ----------

    def batches(self, run_id: str) -> list[dict[str, Any]]:
        """每批的总数与**未交付**数（口径 = `SETTLED_CARD_STATUSES`，不是终态）。"""
        settled = sorted(SETTLED_CARD_STATUSES)
        rows = self._db.query_all(
            "SELECT batch, COUNT(*) AS total, "
            "SUM(CASE WHEN status IN ({}) THEN 0 ELSE 1 END) AS unfinished "
            "FROM run_tasks WHERE run_id = ? GROUP BY batch ORDER BY batch ASC".format(
                ",".join("?" * len(settled))
            ),
            (*settled, run_id),
        )
        return [
            {"batch": int(r["batch"]), "total": int(r["total"]), "unfinished": int(r["unfinished"] or 0)}
            for r in rows
        ]

    def current_batch(self, run_id: str) -> Optional[int]:
        """
        「当前批次」= 编号最小、且其中还有**未交付**卡片的批次；全部交付返回 None。

        ★ 这就是「批间串行」的唯一实现：批次 1 有没有得做，取决于批次 0 是否清空，
        而不是取决于谁的依赖刚好写完。停滞（护栏 3）正是卡在这里时才会发生 ——
        一张没人认领的 `todo` 就足以永久挡住后面所有批次。
        「清空」按 `SETTLED_CARD_STATUSES` 数（含 `review`），理由写在那条常量上。
        """
        for entry in self.batches(run_id):
            if entry["unfinished"] > 0:
                return int(entry["batch"])
        return None

    def ready_cards(self, run_id: str) -> list[dict[str, Any]]:
        """
        当前批次里可以摊出去认领的卡：`todo`、且依赖都已交付。

        依赖这层检查在批次顺序下**本该是多余的**（更早批次既然清空，依赖必然交付），
        但 §5.3 明写「未满足依赖的卡不可认领」，而本阶段之后会有人改批 ——
        这条判据留在唯一的摊卡入口里，比留给调用方自觉可靠。
        """
        batch = self.current_batch(run_id)
        if batch is None:
            return []
        cards = self.list_cards(run_id)
        from app.workspace.artifact_store import ArtifactStore
        artifacts = ArtifactStore(self._db)
        settled = {c["id"] for c in cards if str(c["status"]) in SETTLED_CARD_STATUSES
                   and artifacts.validate(run_id, c["id"])}
        return [
            c
            for c in cards
            if int(c["batch"]) == batch
            and str(c["status"]) == CARD_TODO
            and not str(c["owner_employee_id"])
            and all(str(d) in settled for d in c["depends_on"])
        ]

    def inflight_count(self, employee_id: str, *, run_id: str = "") -> int:
        """
        这名员工现在有几张卡在飞 —— 与认领 SQL 里的子查询同一个谓词。

        带 `run_id` 是**只窄化读取**（界面按 run 显示用），认领限流走的是全局负载：
        一个人同时做三个项目的四件事，比一个项目里的四件事更该被拦住。
        """
        placeholders = ",".join("?" * len(INFLIGHT_CARD_STATUSES))
        sql = (
            "SELECT COUNT(*) FROM run_tasks "
            f"WHERE owner_employee_id = ? AND status IN ({placeholders})"
        )
        params: list[Any] = [employee_id, *sorted(INFLIGHT_CARD_STATUSES)]
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        count = self._db.query_scalar(sql, tuple(params))
        return int(count or 0)

    # ---------- 缝合：执行真相 → 看板 ----------

    def sync_from_task(self, task: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """
        按 `project_status()` 把一条 `TaskRecord` 的进展落到它那张卡上。

        任务终态钩子（`executor`）是唯一调用方；**幂等**：状态已经是投影结果时
        直接返回当前卡片，这样重试、迟到的第二次广播、对账扫描都不会把卡推歪。

        ★ 两道拒写

        - 卡片已终态 → 不回退。迟到的任务汇报不许把 `done` 的卡拉回在飞。
        - `task.employee_id` 与卡片 owner 不一致 → 不写。那说明缝合键被改坏了
          （或者有人绕开 `claim()` 写了 owner），此时照投影写下去会让
          甲的执行结果改掉乙的卡片 —— 界面会显示一个没做过这件事的人「已完成」。
        """
        card_id = str(task.get("board_card_id") or "")
        if not card_id:
            return None

        row = self.get(card_id)
        if row is None:
            logger.warning("任务挂在看不见的卡片上: task=%s card=%s", task.get("task_id"), card_id)
            return None
        owner = str(row["owner_employee_id"] or "")
        if str(row.get("task_id") or "") != str(task.get("task_id") or ""):
            return None  # A superseded attempt cannot change a newly dispatched card.
        employee_id = str(task.get("employee_id") or "")
        if owner and employee_id and owner != employee_id:
            logger.warning(
                "拒绝跨员工写卡片: card=%s owner=%s 但汇报来自 %s", card_id, owner, employee_id
            )
            return None
        target = project_status(task)
        if str(row["status"]) == target:
            return row
        if bool(row["terminal"]):
            logger.info("卡片已终态，忽略后续汇报: card=%s %s -> %s", card_id, row["status"], target)
            return None
        try:
            return self.advance_card(
                card_id,
                target,
                blocked_reason=project_reason(task),
                task_id=str(task.get("task_id") or ""),
            )
        except IllegalCardTransition as e:
            logger.warning("卡片迁移被拒: %s", e)
            return None

    def invalidate_dependents(self, run_id: str, source_id: str) -> list[str]:
        """A user changing an upstream delivery also invalidates outputs derived from it."""
        from app.tasks.executor import executor
        from app.tasks.store import task_store
        cards = self.list_cards(run_id)
        changed = {source_id}
        invalidated = []
        for _ in range(len(cards)):
            newly_changed = []
            for card in cards:
                if card["id"] in changed or not changed.intersection(card["depends_on"]):
                    continue
                newly_changed.append(card["id"])
                if card["status"] in (CARD_REVIEW, CARD_DONE, CARD_IN_PROGRESS, CARD_BLOCKED):
                    if card["status"] == CARD_IN_PROGRESS and card["task_id"]:
                        task_store.cancel(card["task_id"])
                        executor.abort(card["task_id"])
                        from app.workspace.run_store import run_store
                        run_store.record_task_result(run_id, card["task_id"], employee_id=card["owner_employee_id"], status="cancelled")
                    self.advance_card(card["id"], CARD_TODO, blocked_reason="上游成果退回重做，需要使用新版成果重新执行")
                    invalidated.append(card["id"])
            if not newly_changed:
                break
            changed.update(newly_changed)
        return invalidated


def _normalize_brief(brief: Any) -> dict[str, Any]:
    """brief 是结构不是散文（§5.1）：给成字符串就包一层 goal，别让看板显示一个裸串。"""
    if isinstance(brief, dict):
        return brief
    text = str(brief or "").strip()
    return {"goal": text} if text else {}


def _clean_ids(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for raw in values or ():
        value = str(raw or "").strip()
        if value and value not in out:
            out.append(value)
    return out


#: 进程级单例
board = Board()


__all__ = [
    "ABANDONED_CARD_STATUSES",
    "BLOCKED_REASON_CHARS",
    "CARD_BLOCKED",
    "CARD_CANCELLED",
    "CARD_CREATORS",
    "CARD_DONE",
    "CARD_IN_PROGRESS",
    "CARD_PRIORITIES",
    "CARD_REVIEW",
    "CARD_STATUSES",
    "CARD_STATUS_LABELS",
    "CARD_TODO",
    "CARD_TRANSITIONS",
    "CardDependencyError",
    "CREATED_BY_COORDINATOR",
    "CREATED_BY_EMPLOYEE",
    "CREATED_BY_SYSTEM",
    "CREATED_BY_USER",
    "IllegalCardTransition",
    "INFLIGHT_CARD_STATUSES",
    "SETTLED_CARD_STATUSES",
    "TERMINAL_CARD_STATUSES",
    "Board",
    "board",
    "new_card_id",
    "project_reason",
    "project_status",
]
