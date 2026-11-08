"""
工作空间模块的 SQLite 持久化层 —— 标准库 sqlite3 + WAL + 单例。

★ 为什么这一层是 SQLite，而项目其余地方是文件型

工作空间里的实体（工作空间 / 群组 / 成员 / 员工 / 员工间消息）之间有**真实的
外键关系与级联**：删一个工作空间，要连带删掉它的群组、成员、员工、员工间消息。
文件型要手工维护这份引用完整性（删工作空间时自己遍历、自己删、自己防漏），
而 SQLite 的 `FOREIGN KEY ... ON DELETE CASCADE` 一行就闭环了。

所以本模块**单独**走关系库，其余栈（对话历史 / 后台子任务 / 长期记忆）保持
文件型不动 —— 两者互不干扰，也没有「迁移旧数据」的包袱。

★ 为什么用标准库 sqlite3，而不是 aiosqlite

与 `conversation_store` / `task_store` 同一条纪律：写方法**同步**、失败只告警不抛。
工作空间是低频写（配置 CRUD + 群组消息追加），同步写几 KB 的代价可以接受，
换来的是「任何上下文都能安全调用」（不变量 44 的同类取舍）。

★ 连接管理

- 单连接 + `check_same_thread=False`：单进程 uvicorn，一把 `threading.RLock`
  守住所有访问，与 `ConversationStore` / `MemoryStore` 同一套做法。
- 打开时执行 PRAGMA：`journal_mode=WAL`（读写并发）、`synchronous=NORMAL`
  （WAL 下安全的性能折中）、`foreign_keys=ON`（级联删除的前提）。
- `row_factory=sqlite3.Row`：读回 dict 风格的行，字段名 snake_case。

★ schema 迁移（v2 起才有，此前**没有机制**：`SCHEMA_VERSION` 只写不读）

`_DDL` 描述「当前全量形状」，`_MIGRATIONS` 描述「从旧库怎么爬上来」。三条纪律：

1. **重入靠 `PRAGMA table_info` 探测**，不靠 `try/except ALTER` —— 后者会把「列已存在」
   与「磁盘满 / 权限错 / 库损坏」压成同一个静默成功。
2. **迁移失败必须响亮**：抛 `RuntimeError` 并记 `migration_error`。半迁移状态下继续跑，
   症状是一堆指向业务逻辑的误导错误（位置列 INSERT 与表形状不匹配就是那种）。
3. **新库不跑迁移**：`meta` 里没有版本号就是全新库，`_DDL` 已经建到位，只写版本。
   这条区分靠的是「读不到版本」而不是「猜」，所以顺序是先建表再读版本。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColumnSpec:
    """一列的完整描述：DDL 形状 + Python↔SQLite 的转换方式。

    ★ `kind` 决定三件事，而它们原先是三份各自硬编码的清单：
      DDL（这里）、INSERT/UPDATE 的取值序列化（`store.py`）、行还原时的反序列化（`store.py`）。
      收成一份元数据后，**给员工加一个字段只需要在本列表里加一行**。
    """

    name: str
    ddl: str
    #: text=原样 | int=存 0/1 取 bool | json=存 JSON 串取 dict/list
    kind: str = "text"
    #: 仅 kind=json 用：解析失败或缺列时反序列化成的形状（与 DDL 的 DEFAULT 保持一致）。
    json_default: str = "{}"


#: 员工表的唯一字段清单。顺序即 DDL 里的顺序。
#: ⚠️ 旧库经 `ALTER TABLE ADD COLUMN` 迁移后列顺序会与本列表不同（SQLite 只能追加到末尾）。
#: 这不影响正确性 —— 所有读写都按列名，不按位置；正是为了保住这一点，
#: `store.py` 原先那条 13 位置列 INSERT 才被换掉（它是 v2 迁移的头号风险）。
EMPLOYEE_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("id", "TEXT PRIMARY KEY"),
    ColumnSpec(
        "workspace_id",
        "TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE",
    ),
    # ---- persona（角色定位 + 能力设定）----
    ColumnSpec("name", "TEXT NOT NULL"),
    ColumnSpec("tagline", "TEXT NOT NULL DEFAULT ''"),
    ColumnSpec("tone", "TEXT NOT NULL DEFAULT 'professional'"),
    ColumnSpec("tone_custom", "TEXT NOT NULL DEFAULT ''"),
    ColumnSpec("instructions", "TEXT NOT NULL DEFAULT ''"),
    ColumnSpec("scope", "TEXT NOT NULL DEFAULT ''"),
    # ---- 底层能力模板 + 工具白名单 ----
    ColumnSpec("base_agent", "TEXT NOT NULL DEFAULT 'file_agent'"),
    ColumnSpec("tools_allowed", "TEXT NOT NULL DEFAULT '[]'", kind="json", json_default="[]"),
    ColumnSpec("enabled", "INTEGER NOT NULL DEFAULT 1", kind="int"),
    # ---- v2：模型名要能 GROUP BY（按模型聚合成本），其余异构维度进 role_card ----
    ColumnSpec("model", "TEXT NOT NULL DEFAULT ''"),
    ColumnSpec("model_profile_id", "TEXT NOT NULL DEFAULT ''"),
    ColumnSpec("role_card", "TEXT NOT NULL DEFAULT '{}'", kind="json"),
    ColumnSpec("created_at", "TEXT NOT NULL"),
    ColumnSpec("updated_at", "TEXT NOT NULL"),
)


def _employees_ddl() -> str:
    lines = [f"    {c.name:<16} {c.ddl}" for c in EMPLOYEE_COLUMNS]
    return "CREATE TABLE IF NOT EXISTS employees (\n" + ",\n".join(lines) + "\n)"


def table_shape(conn: sqlite3.Connection, table: str) -> dict[str, tuple[str, int, Any]]:
    """`列名 → (类型, notnull, 默认值)`。给测试比对「迁移后的库」与「全新的库」用。"""
    return {row[1]: (row[2], row[3], row[4]) for row in conn.execute(f"PRAGMA table_info({table})")}


#: schema 版本。
#: ★ `_DDL` 描述的是**当前全量形状**（新库直接建到 v2），`_MIGRATIONS` 描述的是
#:   **从旧库怎么爬上来**（既有 `workspace.db` 一行数据都不丢）。两份定义必然漂移，
#:   所以有一条测试把「v1 库迁移后」与「全新库」逐表做 `PRAGMA table_info` 比对。
SCHEMA_VERSION = 4

#: 建表 DDL —— v1 就有的七张表（顺序有依赖：先被引用的表先建）。
#: 字段命名 snake_case、时间戳 ISO8601 TEXT —— 与全项目约定一致。
_DDL_BASE_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workspaces (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        -- ★ v2：归档时间。空串=在用。归档是「不再出现在列表里但一切都在」，
        --   与 delete 是两件事，不能做成一个布尔开关。
        archived_at TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS groups (
        id           TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        created_at   TEXT NOT NULL
    )
    """,
    _employees_ddl(),
    """
    CREATE TABLE IF NOT EXISTS group_members (
        id          TEXT PRIMARY KEY,
        group_id    TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        employee_id TEXT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        role        TEXT NOT NULL DEFAULT 'member',
        created_at  TEXT NOT NULL,
        UNIQUE(group_id, employee_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id          TEXT PRIMARY KEY,
        group_id    TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        employee_id TEXT,
        kind        TEXT NOT NULL DEFAULT 'report',
        content     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        meta        TEXT NOT NULL DEFAULT '{}',
        -- ★ v2：七个可查询维度。`artifact_refs` / `handoff_depth` / `deadline`
        --   仍然留在 meta —— 它们只用于展示，不进 WHERE。
        run_id                 TEXT NOT NULL DEFAULT '',
        task_id                TEXT NOT NULL DEFAULT '',
        sender_kind            TEXT NOT NULL DEFAULT '',   -- employee|user|coordinator|system
        recipient_employee_id  TEXT NOT NULL DEFAULT '',   -- 定向消息的收件人（认领轮的收件箱）
        reply_to               TEXT NOT NULL DEFAULT '',   -- 环检测的边
        status                 TEXT NOT NULL DEFAULT '',   -- 消息自身生命周期（如 request 的 accepted）
        visibility             TEXT NOT NULL DEFAULT 'group'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS employee_threads (
        id          TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        thread_id   TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'private',
        created_at  TEXT NOT NULL,
        UNIQUE(employee_id, thread_id)
    )
    """,
]

#: v2 新增的四张表。
#: 新库随 `_DDL` 一起建；旧库由 `_MIGRATIONS[0]` 建。两份共用这个列表，不留两处抄写。
_DDL_V2_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS runs (
        id            TEXT PRIMARY KEY,
        group_id      TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        instruction   TEXT NOT NULL,
        mode          TEXT NOT NULL DEFAULT 'negotiated',   -- negotiated | direct
        status        TEXT NOT NULL DEFAULT 'planned',      -- 唯一写入点 run_store.advance()
        ledger        TEXT NOT NULL DEFAULT '{}',           -- task/progress 双账本
        pending       TEXT NOT NULL DEFAULT '[]',           -- 未完成 task_id（重启对账用）
        plan_version  INTEGER NOT NULL DEFAULT 1,
        token_used    INTEGER NOT NULL DEFAULT 0,
        -- ★ -1 = 不知道（未配单价）；0 = 真的是免费。把「不知道」显示成 0 是撒谎。
        cost_micro    INTEGER NOT NULL DEFAULT -1,
        token_budget  INTEGER NOT NULL DEFAULT 0,
        wall_limit    INTEGER NOT NULL DEFAULT 0,
        claim_rounds  INTEGER NOT NULL DEFAULT 0,
        -- ★ 停滞检测与「认领轮没有新增 owner」共用这一个计数器，不另起第二套
        stall_count   INTEGER NOT NULL DEFAULT 0,
        error         TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        finished_at   TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_tasks (
        id                TEXT PRIMARY KEY,
        run_id            TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        title             TEXT NOT NULL,
        brief             TEXT NOT NULL DEFAULT '{}',
        status            TEXT NOT NULL DEFAULT 'todo',
        priority          TEXT NOT NULL DEFAULT 'normal',
        batch             INTEGER NOT NULL DEFAULT 0,
        depends_on        TEXT NOT NULL DEFAULT '[]',
        -- ★ 只有 board.claim() 能写它：建卡 ≠ 指派（ADR 0020 + 施工图 §5.4）
        owner_employee_id TEXT NOT NULL DEFAULT '',
        task_id           TEXT NOT NULL DEFAULT '',
        acceptance        TEXT NOT NULL DEFAULT '',
        artifact_ids      TEXT NOT NULL DEFAULT '[]',
        handoff_depth     INTEGER NOT NULL DEFAULT 0,
        deadline          TEXT NOT NULL DEFAULT '',
        blocked_reason    TEXT NOT NULL DEFAULT '',
        created_by        TEXT NOT NULL DEFAULT 'coordinator',
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id          TEXT PRIMARY KEY,
        run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        card_id     TEXT NOT NULL DEFAULT '',
        employee_id TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL DEFAULT 'note',
        name        TEXT NOT NULL,
        -- ★ carrier=file|url|note：不变量是「完成必须可寻址」，不是「必须有磁盘文件」
        carrier     TEXT NOT NULL DEFAULT 'note',
        path        TEXT NOT NULL DEFAULT '',
        url         TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL DEFAULT '',
        summary     TEXT NOT NULL DEFAULT '',
        sha256      TEXT NOT NULL DEFAULT '',
        version     INTEGER NOT NULL DEFAULT 1,
        visibility  TEXT NOT NULL DEFAULT 'project',
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id          TEXT PRIMARY KEY,
        -- ⚠️ 刻意不加 FK：删项目不该删掉「谁在什么时候做过什么」的证据。
        --    审计的意义正是项目已经不在了而发生过的事还在。代价是孤儿行会累积，
        --    由清理脚本按 --keep-projectless-days 显式处置，不做自动 GC。
        project_id  TEXT NOT NULL,
        run_id      TEXT NOT NULL DEFAULT '',
        card_id     TEXT NOT NULL DEFAULT '',
        employee_id TEXT NOT NULL DEFAULT '',
        action      TEXT NOT NULL,
        tool_name   TEXT NOT NULL DEFAULT '',
        artifact_id TEXT NOT NULL DEFAULT '',
        token_cost  INTEGER NOT NULL DEFAULT 0,
        status      TEXT NOT NULL DEFAULT '',
        -- ★ 每 action 一个脱敏 formatter，禁止 json.dumps(args) 整包塞进来
        --   （与 approval/records.py 的「必须记完整入参」刻意不同口径：这里不记思维链、不记密钥）
        detail      TEXT NOT NULL DEFAULT '{}',
        created_at  TEXT NOT NULL
    )
    """,
]

#: **当前全量表形状** = v1 的七张 + v2 的四张。`_ensure_conn` 对每个库都跑它
#: （全部 `IF NOT EXISTS`，旧库上是空操作），所以「新库」与「v1 迁移后的库」
#: 的表集合由同一份列表保证一致 —— 曾经 `_DDL_V2_TABLES` 只挂在迁移里，
#: 全新库就没有 `runs`/`run_tasks`/`artifacts`/`audit_events` 四张表。
_DDL: list[str] = _DDL_BASE_TABLES + _DDL_V2_TABLES

# Durable coordination state. New tables are created for both fresh and upgraded databases.
_DDL += [
    """CREATE TABLE IF NOT EXISTS run_options (
        run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS message_requests (
        group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        client_id TEXT NOT NULL, message_id TEXT NOT NULL, run_id TEXT NOT NULL,
        PRIMARY KEY(group_id,client_id))""",
    """CREATE TABLE IF NOT EXISTS deliveries (
        message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        employee_id TEXT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        state TEXT NOT NULL DEFAULT 'pending', task_id TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        deadline TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(message_id, employee_id))""",
    """CREATE TABLE IF NOT EXISTS dispatches (
        task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        card_id TEXT NOT NULL, employee_id TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS run_budgets (
        run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
        reserved INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
        max_calls INTEGER NOT NULL, max_tokens INTEGER NOT NULL, max_seconds INTEGER NOT NULL,
        max_retries INTEGER NOT NULL, started REAL NOT NULL, error TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS workspace_memories (
        scope TEXT NOT NULL, owner_id TEXT NOT NULL, key TEXT NOT NULL,
        value TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
        author_id TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        PRIMARY KEY(scope, owner_id, key))""",
    "CREATE INDEX IF NOT EXISTS idx_delivery_pending ON deliveries(employee_id,state)",
    "CREATE INDEX IF NOT EXISTS idx_dispatch_run ON dispatches(run_id,state)",
]

#: 索引。**必须在 `_apply_schema_version` 之后执行**：`idx_msg_run` /
#: `idx_msg_inbox` 建在 v2 新增列上，升级路径上那时列还没加出来
#: （列不存在是 `no such column` 硬错，不是空操作）。
_SCHEMA_INDEXES: list[str] = [
    # 双跑约束：一群同时最多一个非终态 run。partial unique 是数据库层的保证，
    # 不是「前端把按钮 disable 掉」——见施工图 §3。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_active ON runs(group_id)
        WHERE status IN ('planned','running','input_required','stalling','replan','budget_limited','concluding')
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_group ON runs(group_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)",
    "CREATE INDEX IF NOT EXISTS idx_board_run ON run_tasks(run_id, batch, status)",
    "CREATE INDEX IF NOT EXISTS idx_board_owner ON run_tasks(owner_employee_id, status)",
    # note/url 行的 path='' 会互相撞，所以版本链是 partial unique
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_version
        ON artifacts(run_id, path, version) WHERE carrier='file' AND path<>''
    """,
    "CREATE INDEX IF NOT EXISTS idx_artifact_run ON artifacts(run_id, card_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_events(project_id, created_at DESC)",
    # ★ 索引列里不能写 rowid（SQLite 直接报 no such column: rowid）—— 但补拉
    #   `WHERE group_id=? AND rowid>? ORDER BY rowid` 仍走这两条索引：二级索引
    #   的每条目都自带 rowid，等值命中后按它天然有序。
    "CREATE INDEX IF NOT EXISTS idx_msg_run ON messages(group_id, run_id)",
    "CREATE INDEX IF NOT EXISTS idx_msg_inbox ON messages(recipient_employee_id, kind)",
    # 项目 = 群聊（一级化）：今天 store.create_workspace 是唯一建群路径，
    # 既有数据天然满足 1:1，这里只是把隐含约定写成约束。
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_group_workspace ON groups(workspace_id)",
]

#: v2 的加列清单：(表, 列, 列定义)。**必须与 `_DDL` 里的表形状逐条一致**。
_V2_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("workspaces", "archived_at", "TEXT NOT NULL DEFAULT ''"),
    ("employees", "model", "TEXT NOT NULL DEFAULT ''"),
    ("employees", "role_card", "TEXT NOT NULL DEFAULT '{}'"),
    ("messages", "run_id", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "task_id", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "sender_kind", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "recipient_employee_id", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "reply_to", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "status", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "visibility", "TEXT NOT NULL DEFAULT 'group'"),
]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """
    列是否存在（`PRAGMA table_info` 探测）。

    ★ 为什么不用 `try: ALTER TABLE ... except`
    后者会把「列已存在」和「磁盘满 / 权限错 / 库损坏」压成同一个静默成功 ——
    而后者是必须让人看见的。探测式重入是**可区分**的。
    """
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """加一列（已存在则跳过）。返回是否真的执行了 ALTER —— 供日志计数。

    ⚠️ 加列一律**不带 FK**：SQLite 要求带 `REFERENCES` 的新列默认值必须是 NULL，
    而这里全部要 `NOT NULL DEFAULT ''`。新表照实做 FK。
    """
    if _has_column(conn, table, column):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")  # 一条 ALTER 只能加一列
    return True


def _backfill_messages(conn: sqlite3.Connection) -> int:
    """
    把 `meta` 里已经埋着的 run_id / task_id 提到列上。

    ★ 不回填的用户可见症状是**老群组在新界面里时间线是空的**
    （新界面按 run_id 分组），而那看起来像「数据丢了」。埋点见
    `orchestrator.py` 的 `meta={"run_id": ...}` / `meta={"task_id": ...}`。
    """
    before = conn.total_changes
    conn.execute(
        "UPDATE messages SET run_id = json_extract(meta, '$.run_id') "
        "WHERE run_id = '' AND json_extract(meta, '$.run_id') IS NOT NULL"
    )
    conn.execute(
        "UPDATE messages SET task_id = json_extract(meta, '$.task_id') "
        "WHERE task_id = '' AND json_extract(meta, '$.task_id') IS NOT NULL"
    )
    # sender_kind：有 employee_id 就是员工发言，否则是主持人/系统的成品文本
    conn.execute(
        "UPDATE messages SET sender_kind = CASE WHEN employee_id IS NOT NULL "
        "AND employee_id <> '' THEN 'employee' ELSE 'coordinator' END WHERE sender_kind = ''"
    )
    return conn.total_changes - before


def _migrate_v2(conn: sqlite3.Connection) -> str:
    """
    v1 → v2 的**数据**步骤：加列 + 把 meta 里的 run_id/task_id 提出来。

    表与索引不在这里建 —— 它们由 `_ensure_conn` 对**所有**库统一执行
    （`_DDL` / `_SCHEMA_INDEXES`，全 `IF NOT EXISTS`）。少一处「只有升级路径才有」
    的定义，就少一处新库缺表的可能。
    """
    added = sum(_add_column(conn, t, c, ddl) for t, c, ddl in _V2_ADDED_COLUMNS)
    backfilled = _backfill_messages(conn)
    return f"加列 {added} / 回填消息 {backfilled} 条"


#: 迁移脚本，按目标版本**升序**。`(目标版本, 说明, callable)`。
#: ⚠️ callable 里不得有「依赖后续版本列」的写法：它们按顺序执行，中途失败会停在
#: 半路上一个版本，`meta('schema_version')` 不会前跳，下次启动重试同一步。
def _migrate_v3(conn: sqlite3.Connection) -> str:
    columns = {r[1] for r in conn.execute("PRAGMA table_info(employees)")}
    if "model_profile_id" not in columns:
        conn.execute("ALTER TABLE employees ADD COLUMN model_profile_id TEXT NOT NULL DEFAULT ''")
    return "员工模型独立连接"


_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], str]]] = [
    (2, "run 实体 + 任务板 + 工件库 + 审计；messages 提列并回填；employees 提 model/role_card", _migrate_v2),
    (3, "员工模型独立连接", _migrate_v3),
    (4, "持久化收件箱、派发意图、协作预算与范围记忆", lambda conn: "新增协调状态表"),
]


class WorkspaceDB:
    """工作空间模块的 SQLite 连接与轻量迁移。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._resolved: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        #: 迁移失败的原因（None=没有）。健康检查与启动日志都读它。
        self._migration_error: Optional[str] = None
        #: 本次启动真的跑了哪些迁移（只给日志用，空串=没迁移）
        self._migrated_note: str = ""

    # ---------- 路径与开关 ----------
    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "WORKSPACE_ENABLED", True))

    @property
    def migration_error(self) -> Optional[str]:
        return self._migration_error

    @property
    def schema_version(self) -> int:
        """当前库里的版本号（读不到=0，即全新库）。"""
        # 走 `query_one` 而不是直接摸 `self._conn`：连接是懒建的，
        # 没查过库时 `_conn` 还是 None，属性就会对着一个 v2 库回 0 ——
        # 而 0 在日志里的意思正是「全新库」。
        row = self.query_one("SELECT value FROM meta WHERE key='schema_version'")
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    @property
    def path(self) -> Path:
        if self._resolved is None:
            # 强制转 Path：`settings.workspace_db_path` 这类配置项完全可能给出字符串，
            # 而下面 `_ensure_conn` 要用 `.parent.mkdir()` —— 传 str 时症状是一个
            # AttributeError，看起来像「路径不存在」，实际是类型没收敛。
            base = self._path if self._path is not None else settings.workspace_db_path
            self._resolved = Path(base)
        return self._resolved

    # ---------- 连接 ----------
    def _ensure_conn(self) -> sqlite3.Connection:
        """惰性打开连接 + 建表 + **按版本迁移**（幂等、可重入）。"""
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with self._lock:
                for ddl in _DDL:
                    conn.execute(ddl)
                self._apply_schema_version(conn)
                # ★ 索引必须在迁移**之后**：有两条建在 v2 新增列上，升级路径上
                #   先建会 `no such column`。放在同一个 commit 前 = 建索引失败
                #   就不写版本号，下次启动整步重试。
                for ddl in _SCHEMA_INDEXES:
                    conn.execute(ddl)
                conn.commit()
        except Exception as e:  # noqa: BLE001
            # ★ 迁移失败**必须**响亮：静默降级会让下面每一条 SQL 都在错误的库形状上跑，
            #   症状是一堆指向「业务逻辑」的误导错误（真实的 13 位置列 INSERT 病根就在这）。
            #   回滚 + 不写版本号 + 关闭连接：下次启动重试同一步。
            conn.rollback()
            conn.close()
            self._migration_error = f"{type(e).__name__}: {e}"
            logger.error(
                "✗ 工作空间 schema 迁移到 v%s 失败（库=%s）: %s",
                SCHEMA_VERSION, self.path, self._migration_error,
            )
            raise RuntimeError(
                f"工作空间数据库无法升级到 schema v{SCHEMA_VERSION}：{self._migration_error}。"
                "若是唯一索引冲突（如 groups.workspace_id 已有重复行），需要先手工合并数据；"
                "确认可以丢弃时，删除该库文件即可重建。"
            ) from e
        self._conn = conn
        logger.info(
            "工作空间 SQLite 已就绪: %s (schema v%s%s)",
            self.path,
            SCHEMA_VERSION,
            "" if not self._migrated_note else f" · 本次迁移：{self._migrated_note}",
        )
        return conn

    def _apply_schema_version(self, conn: sqlite3.Connection) -> None:
        """
        三种情形：
        - **新库**（meta 里没有版本）：`_DDL` 已经把全部 v2 结构建好了，只写版本号。
        - **旧库**（版本 < SCHEMA_VERSION）：按升序跑缺失的迁移步。
        - **未来的库**（版本 > SCHEMA_VERSION）：代码被回滚过 —— 只告警不动结构，
          因为我们不知道那个版本做了什么，盲目迁移比停下来更危险。
        """
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        raw = str(row[0]) if row is not None else ""
        current = int(raw) if raw.isdigit() else 0

        if current == 0:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        if current > SCHEMA_VERSION:
            self._migration_error = (
                f"库版本 {current} 比代码版本 {SCHEMA_VERSION} 新（代码被回滚过？），已跳过迁移"
            )
            logger.error("✗ 工作空间 schema: %s", self._migration_error)
            return
        if current == SCHEMA_VERSION:
            return

        notes: list[str] = []
        for target, label, migrate in _MIGRATIONS:
            if target <= current or target > SCHEMA_VERSION:
                continue
            logger.info("工作空间 schema %s → %s: %s", current, target, label)
            notes.append(f"v{target}: {migrate(conn)}")
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(target),),
            )
            current = target
        self._migrated_note = "；".join(notes)

    # ---------- 通用访问 ----------

    @contextmanager
    def transaction(self):
        """A synchronous transaction; never hold this context across an await."""
        with self._lock:
            conn = self._ensure_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """执行一条写语句，返回受影响行数（自动提交）。"""
        if not self.enabled:
            return 0
        with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount

    def executemany(self, sql: str, seq: list[tuple[Any, ...]]) -> int:
        if not self.enabled:
            return 0
        with self._lock:
            conn = self._ensure_conn()
            cur = conn.executemany(sql, seq)
            conn.commit()
            return cur.rowcount

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """读多行（返回 dict 列表）。"""
        if not self.enabled:
            return []
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        """读一行，不存在返回 None。"""
        if not self.enabled:
            return None
        with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def query_scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[Any]:
        """读单个标量（计数等）。"""
        if not self.enabled:
            return None
        with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(sql, params).fetchone()
            return row[0] if row is not None else None

    def stats(self) -> dict[str, Any]:
        """表规模统计（启动日志与调试用）。"""
        tables = (
            "workspaces", "groups", "employees", "group_members", "messages",
            "runs", "run_tasks", "artifacts", "audit_events",
        )
        counts = {}
        for t in tables:
            try:
                counts[t] = self.query_scalar(f"SELECT COUNT(*) FROM {t}") or 0
            except Exception as e:  # noqa: BLE001
                logger.warning("统计表 %s 失败: %s", t, e)
                counts[t] = 0
        return {
            "enabled": self.enabled,
            "path": str(self.path),
            "schema_version": SCHEMA_VERSION,
            "migration_error": self._migration_error,
            **counts,
        }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


#: 进程级单例（与 conversation_store / task_store 同理：单进程部署）
workspace_db = WorkspaceDB()


__all__ = [
    "ColumnSpec",
    "EMPLOYEE_COLUMNS",
    "SCHEMA_VERSION",
    "WorkspaceDB",
    "table_shape",
    "workspace_db",
]
