"""
工作空间模块的存储层 —— 单例 + 十表 CRUD。

★ 与 `conversations/store.py` / `tasks/store.py` 的关系

同样是「单例 + 进程内锁 + 失败只告警不抛」的纪律，但底层是 `WorkspaceDB`
（SQLite）而不是文件。理由见 `db.py` 模块注释：工作空间实体有真实外键级联。

★ 员工字段清单只有一份（v2 起）

`db.EMPLOYEE_COLUMNS` 同时决定 DDL、INSERT 的列与占位符、UPDATE 的白名单、
行还原的反序列化。原先这四件事是四处硬编码（13 位置列 INSERT + 7 键 UPDATE 白名单
+ 逐字段 `_row_to_employee` + DDL），加一个字段要改四处、漏一处的症状是
「存进去了但读不出来」。

★ 所有 id 的校验

workspace / group / employee / message 的 id 会直接出现在 URL 路径段里
（`/api/workspace/workspaces/{id}`），与 `thread_id` 是同一类攻击面，
所以一律复用 `is_valid_thread_id`（只允许字母数字与 `-` / `_`）。
非法与不存在统一返回 None/False（接口层据此 404）。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Optional

from app.agents.think import split_think
from app.conversations.models import now_iso
from app.conversations.store import is_valid_thread_id
from app.config import settings
from app.workspace import events
from app.workspace.db import EMPLOYEE_COLUMNS, ColumnSpec, WorkspaceDB, workspace_db
from app.workspace.models import normalize_base_agent

logger = logging.getLogger(__name__)

#: 员工表里由存储层自己维护、不接受外部赋值的列。
#: （`id` 由 `_new_id()` 生成，两个时间戳由 `now_iso()` 生成，`workspace_id` 是归属关系。）
_EMPLOYEE_IMMUTABLE = frozenset({"id", "workspace_id", "created_at", "updated_at"})

_EMPLOYEE_BY_NAME: dict[str, ColumnSpec] = {c.name: c for c in EMPLOYEE_COLUMNS}

#: 群组消息的 kind 白名单（施工图 §4.1，阶段 3 提到本期）。
#: ★ 必须是**常量**而不是 `append_message` 里的内联元组：这条清单有三端
#:   （后端白名单 / 前端 `GroupMessageKind` / 徽章表），三处不一致的症状是
#:   「消息存进去了但界面按默认徽章渲染」或反过来「界面能显示的类型存不进去」。
MESSAGE_KINDS: tuple[str, ...] = (
    "goal",
    "proposal",
    "request",
    "commitment",
    "deliverable",
    "blocker",
    "decision",
    "summary",
    "system",
)

#: v1 的两个旧名 → 现名。**只读别名**：库里 `report`/`conclusion` 有真实历史行，
#: 删列或改行都是把用户的时间线改写一遍；所以写入侧归一、读取侧也归一，历史行原样躺在库里。
KIND_ALIASES: dict[str, str] = {"report": "deliverable", "conclusion": "summary"}

#: 定向消息（说给某一个人听的）。护栏 1 数的就是这四类 —— 它们是「同事之间在对话」，
#: 而 `goal/decision/summary/system` 是广播，条数由 run 生命周期决定，不会发散。
DIRECTED_KINDS: frozenset[str] = frozenset({"request", "commitment", "deliverable", "blocker"})

#: `messages.status`：一条定向消息的三种命运。
#: `''` = 还没人处理过它（会进收件箱）；
#: `seen` = 已经喂进过一轮认领输入 —— 同一条话不许每轮被重读一遍；
#: `answered` = 有人 `reply_to` 回答了它。
STATUS_OPEN = ""
STATUS_SEEN = "seen"
STATUS_ANSWERED = "answered"


def normalize_kind(kind: Any) -> str:
    """
    任何 kind（含旧名、含非法值）→ 白名单里的一个值。

    ★ 非法值落到 `system` 而不是被拒：`append_message` 是群组时间线的唯一写入口，
      在这里丢消息等于「护栏自己把证据吞了」。界面渲染得出来的一句「系统」，
      永远好过一条从来没出现过的消息。
    """
    text = str(kind or "").strip()
    text = KIND_ALIASES.get(text, text)
    return text if text in MESSAGE_KINDS else "system"


#: 工具返回的信封特征。它们出现在群聊正文里就意味着「有人把工具原始输出贴进了时间线」
#: —— 那既是禁令 3 的违例，也是泄露面（工具结果里可能有密钥与文件内容）。
_TOOL_ENVELOPE = re.compile(r'"(?:tool_name|call_id|observation|tool_input)"\s*:')

#: 超长/含工具输出时补在末尾的那句话。★ 处置必须是「指向全文所在的地方」，
#: 而不是砍尾 —— 砍尾会让人读到一个断在半句的结论，那是新的欺骗（施工图 §4.2）。
_TRUNCATED_MARK = "（这里是摘要，全文在他的私聊工作台）"
_TOOL_OUTPUT_MARK = "（工具原始输出不进群聊，明细在他的私聊工作台）"


def sanitize_group_text(content: str) -> tuple[str, str]:
    """
    三条禁令落在这一个函数里（施工图 §4.2）：禁思维链 / 禁工具原始输出 / 禁长篇全文。

    Returns:
        (可以进群聊的文本, 处置说明)。`处置说明` 为空表示什么都没动 ——
        调用方只在**动过**的时候记日志：每条消息都记一遍会把真信号淹在噪声里。
    """
    text = (content or "").strip()
    if not text:
        return "", ""
    notes: list[str] = []

    reasoning, answer = split_think(text)
    if reasoning:
        # 思维链整条丢掉（不是截断、不是转述）：禁令 1 没有「压缩版」这一档。
        text = answer.strip()
        notes.append("含思维链，已丢弃只保留成品句")
    if not text:
        return "", "整条内容都是思维链，没有成品可进群聊"

    if _TOOL_ENVELOPE.search(text):
        text = _TOOL_OUTPUT_MARK.strip("（）")
        notes.append("含工具原始输出，已替换成一句实话")

    limit = max(80, int(settings.GROUP_SUMMARY_CHARS))
    if len(text) > limit:
        text = text[: limit - len(_TRUNCATED_MARK)] + _TRUNCATED_MARK
        notes.append(f"超过 {limit} 字，已按摘要截断")

    return text, "；".join(notes)



def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _to_sql(spec: ColumnSpec, value: Any) -> Any:
    """Python 值 → 落库值。转换规则只在这一处（与 `ColumnSpec.kind` 配对）。"""
    if spec.kind == "json":
        return json.dumps(value if value is not None else json.loads(spec.json_default),
                          ensure_ascii=False)
    if spec.kind == "int":
        return 1 if value else 0
    return value


def _row_to_employee(row: dict[str, Any]) -> dict[str, Any]:
    """
    把 employees 行还原成接口层形状（按 `EMPLOYEE_COLUMNS` 的 `kind` 反序列化）。

    ★ 为什么是元数据驱动而不是逐字段写
    原先这里是「enabled 转 bool + tools_allowed 解 JSON」两条硬编码，
    与 INSERT 的 13 个位置、UPDATE 的 7 个键白名单合起来共**三份清单** ——
    加一个字段要改三处，漏一处的症状是「字段存进去了但读出来没有」。
    """
    out = dict(row)
    for name, spec in _EMPLOYEE_BY_NAME.items():
        if name not in out:
            continue
        if spec.kind == "json":
            try:
                parsed = json.loads(out[name] if out[name] is not None else spec.json_default)
            except (json.JSONDecodeError, TypeError):
                parsed = json.loads(spec.json_default)
            out[name] = parsed
        elif spec.kind == "int":
            out[name] = bool(out[name])
    if "base_agent" in out:
        out["base_agent"] = normalize_base_agent(out["base_agent"])
    return out


def _row_to_message(row: dict[str, Any]) -> dict[str, Any]:
    """把 messages 行还原（`meta` JSON 字段解析）。"""
    out = dict(row)
    try:
        meta = json.loads(out.get("meta") or "{}")
        out["meta"] = meta if isinstance(meta, dict) else {}
    except (json.JSONDecodeError, TypeError):
        out["meta"] = {}
    # ★ 别名在**读取侧**也归一：v1 的老行存的是 `report`/`conclusion`，
    #   白名单已经不含这两个值。不归一的话前端按 9 类渲染，老消息全成默认徽章 ——
    #   症状是「升级之后历史消息看不懂了」，而这正是回填要防的那类事。
    out["kind"] = normalize_kind(out.get("kind"))
    return out


class WorkspaceStore:
    """工作空间 / 群组 / 成员 / 员工 / 员工间消息的 CRUD。"""

    def __init__(self, db: Optional[WorkspaceDB] = None) -> None:
        self._db = db or workspace_db

    @property
    def enabled(self) -> bool:
        return self._db.enabled

    # ---------- 工作空间 ----------

    def create_workspace(self, *, name: str, description: str = "") -> Optional[dict[str, Any]]:
        """
        创建一个工作空间，并**自动创建同名群组**（需求 2）。

        ★ 为什么群组跟着工作空间一起建

        「以项目方式创建」的语义就是「拉一个团队出来协作」—— 群组是协作的容器。
        分开两步建会让「建了工作空间却忘了建群组」这种空态成为常态，
        而空态没有任何补救提示，只会让群组界面永远空着。
        """
        if not self.enabled:
            return None
        clean_name = " ".join((name or "").split())[:60]
        if not clean_name:
            return None
        ws_id = _new_id()
        group_id = _new_id()
        now = now_iso()
        ok = self._db.execute(
            "INSERT INTO workspaces(id, name, description, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (ws_id, clean_name, (description or "").strip()[:500], now, now),
        )
        if not ok:
            return None
        self._db.execute(
            "INSERT INTO groups(id, workspace_id, name, created_at) VALUES(?, ?, ?, ?)",
            (group_id, ws_id, clean_name, now),
        )
        return self.get_workspace(ws_id)

    def list_workspaces(
        self, *, limit: int = 50, offset: int = 0, include_archived: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        """列出工作空间（按更新时间倒序），返回 (本页, 总数)。

        ★ 默认**不含**归档项目：归档的语义就是「不再出现在列表里但一切都在」。
        总数与本页同口径（都排除归档），否则界面会显示「共 12 个」却只列 5 个。
        """
        if not self.enabled:
            return [], 0
        clause = "" if include_archived else "WHERE archived_at = ''"
        total = self._db.query_scalar(f"SELECT COUNT(*) FROM workspaces {clause}") or 0
        rows = self._db.query_all(
            f"SELECT * FROM workspaces {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (max(1, limit), max(0, offset)),
        )
        out = []
        for r in rows:
            item = dict(r)
            item["group_count"] = self._db.query_scalar(
                "SELECT COUNT(*) FROM groups WHERE workspace_id = ?", (item["id"],)
            ) or 0
            item["employee_count"] = self._db.query_scalar(
                "SELECT COUNT(*) FROM employees WHERE workspace_id = ?", (item["id"],)
            ) or 0
            out.append(item)
        return out, int(total)

    def get_workspace(self, ws_id: str) -> Optional[dict[str, Any]]:
        if not is_valid_thread_id(ws_id):
            return None
        row = self._db.query_one("SELECT * FROM workspaces WHERE id = ?", (ws_id,))
        if row is None:
            return None
        item = dict(row)
        item["groups"] = self._db.query_all(
            "SELECT * FROM groups WHERE workspace_id = ? ORDER BY created_at ASC", (ws_id,)
        )
        item["employee_count"] = self._db.query_scalar(
            "SELECT COUNT(*) FROM employees WHERE workspace_id = ?", (ws_id,)
        ) or 0
        return item

    def update_workspace(self, ws_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        if not is_valid_thread_id(ws_id):
            return None
        sets: list[str] = []
        params: list[Any] = []
        if "name" in fields and fields["name"] is not None:
            clean = " ".join(str(fields["name"]).split())[:60]
            if clean:
                sets.append("name = ?")
                params.append(clean)
        if "description" in fields and fields["description"] is not None:
            sets.append("description = ?")
            params.append(str(fields["description"]).strip()[:500])
        if "archived_at" in fields and fields["archived_at"] is not None:
            # 归档=写时间戳，恢复=写空串。两者都经这一个字段，界面不需要两个端点。
            sets.append("archived_at = ?")
            params.append(str(fields["archived_at"]).strip()[:40])
        if not sets:
            return self.get_workspace(ws_id)
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(ws_id)
        self._db.execute(f"UPDATE workspaces SET {', '.join(sets)} WHERE id = ?", tuple(params))
        return self.get_workspace(ws_id)

    def delete_workspace(self, ws_id: str) -> bool:
        """删除工作空间（级联删群组/成员/员工/消息/员工线程）。"""
        if not is_valid_thread_id(ws_id):
            return False
        snapshots = self._db.query_all("SELECT a.id FROM artifacts a JOIN runs r ON r.id=a.run_id JOIN groups g ON g.id=r.group_id "
                                      "WHERE g.workspace_id=? AND a.carrier='file'", (ws_id,))
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM workspace_memories WHERE (scope='project' AND owner_id=?) OR "
                         "(scope='employee' AND owner_id IN (SELECT id FROM employees WHERE workspace_id=?))", (ws_id, ws_id))
            deleted = conn.execute("DELETE FROM workspaces WHERE id=?", (ws_id,)).rowcount > 0
        if deleted:
            from app.workspace.artifact_store import ArtifactStore
            artifacts = ArtifactStore(self._db)
            for item in snapshots:
                try:
                    artifacts.blob_path(item["id"]).unlink(missing_ok=True)
                except (ValueError, OSError) as exc:
                    logger.warning("清理项目成果快照失败: %s", exc)
        return deleted

    # ---------- 群组 ----------

    def list_groups(self, workspace_id: str = "") -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        if workspace_id:
            if not is_valid_thread_id(workspace_id):
                return []
            rows = self._db.query_all(
                "SELECT * FROM groups WHERE workspace_id = ? ORDER BY created_at ASC", (workspace_id,)
            )
        else:
            rows = self._db.query_all("SELECT * FROM groups ORDER BY created_at ASC")
        out = []
        for r in rows:
            item = dict(r)
            item["member_count"] = self._db.query_scalar(
                "SELECT COUNT(*) FROM group_members WHERE group_id = ?", (item["id"],)
            ) or 0
            item["message_count"] = self._db.query_scalar(
                "SELECT COUNT(*) FROM messages WHERE group_id = ?", (item["id"],)
            ) or 0
            out.append(item)
        return out

    def get_group(self, group_id: str) -> Optional[dict[str, Any]]:
        if not is_valid_thread_id(group_id):
            return None
        row = self._db.query_one("SELECT * FROM groups WHERE id = ?", (group_id,))
        if row is None:
            return None
        item = dict(row)
        item["members"] = self.list_members(group_id)
        item["message_count"] = self._db.query_scalar(
            "SELECT COUNT(*) FROM messages WHERE group_id = ?", (group_id,)
        ) or 0
        return item

    def add_member(self, group_id: str, employee_id: str, role: str = "member") -> bool:
        if not is_valid_thread_id(group_id) or not is_valid_thread_id(employee_id):
            return False
        role = role if role in ("member", "orchestrator") else "member"
        try:
            self._db.execute(
                "INSERT OR IGNORE INTO group_members(id, group_id, employee_id, role, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (_new_id(), group_id, employee_id, role, now_iso()),
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("添加群组成员失败: %s", e)
            return False

    def remove_member(self, group_id: str, employee_id: str) -> bool:
        if not is_valid_thread_id(group_id) or not is_valid_thread_id(employee_id):
            return False
        return self._db.execute(
            "DELETE FROM group_members WHERE group_id = ? AND employee_id = ?",
            (group_id, employee_id),
        ) > 0

    def list_members(self, group_id: str) -> list[dict[str, Any]]:
        if not is_valid_thread_id(group_id):
            return []
        rows = self._db.query_all(
            "SELECT gm.role, gm.created_at AS joined_at, e.* "
            "FROM group_members gm JOIN employees e ON e.id = gm.employee_id "
            "WHERE gm.group_id = ? ORDER BY gm.created_at ASC",
            (group_id,),
        )
        return [_row_to_employee({**dict(r)}) for r in rows]

    # ---------- 员工 ----------

    def create_employee(self, workspace_id: str, **payload: Any) -> Optional[dict[str, Any]]:
        """
        新建员工。

        ★ INSERT 的列与占位符由 `EMPLOYEE_COLUMNS` 生成，不给的列走 DDL 默认值。
        原先这里是 13 个位置列 + 13 个 `?` 的手抄，加一列就要数一遍位置；
        而漏掉的那一列会让整条语句 `OperationalError` 并被 `execute` 的告警纪律吞成
        「员工创建失败」—— 这是 v2 迁移最容易被漏掉的一处，所以先把它换成元数据驱动。
        """
        if not self.enabled or not is_valid_thread_id(workspace_id):
            return None
        now = now_iso()
        values: dict[str, Any] = {
            "id": _new_id(),
            "workspace_id": workspace_id,
            "created_at": now,
            "updated_at": now,
        }
        editable = {c.name for c in EMPLOYEE_COLUMNS} - _EMPLOYEE_IMMUTABLE
        values.update({k: v for k, v in payload.items() if k in editable and v is not None})

        cols = [c for c in EMPLOYEE_COLUMNS if c.name in values]
        sql = (
            f"INSERT INTO employees({', '.join(c.name for c in cols)}) "
            f"VALUES({', '.join('?' * len(cols))})"
        )
        ok = self._db.execute(sql, tuple(_to_sql(c, values[c.name]) for c in cols))
        if not ok:
            return None
        return self.get_employee(str(values["id"]))

    def get_employee(self, employee_id: str) -> Optional[dict[str, Any]]:
        if not is_valid_thread_id(employee_id):
            return None
        row = self._db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))
        return _row_to_employee(row) if row is not None else None

    def list_employees(self, workspace_id: str = "") -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        if workspace_id:
            if not is_valid_thread_id(workspace_id):
                return []
            rows = self._db.query_all(
                "SELECT * FROM employees WHERE workspace_id = ? ORDER BY created_at ASC", (workspace_id,)
            )
        else:
            rows = self._db.query_all("SELECT * FROM employees ORDER BY created_at ASC")
        return [_row_to_employee(dict(r)) for r in rows]

    def update_employee(self, employee_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        if not is_valid_thread_id(employee_id):
            return None
        editable = {c.name for c in EMPLOYEE_COLUMNS} - _EMPLOYEE_IMMUTABLE
        sets: list[str] = []
        params: list[Any] = []
        # 列名取自 EMPLOYEE_COLUMNS（不是用户输入），所以拼进 SQL 是安全的
        for name, value in fields.items():
            if name in editable and value is not None:
                sets.append(f"{name} = ?")
                params.append(_to_sql(_EMPLOYEE_BY_NAME[name], value))
        if not sets:
            return self.get_employee(employee_id)
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(employee_id)
        self._db.execute(f"UPDATE employees SET {', '.join(sets)} WHERE id = ?", tuple(params))
        return self.get_employee(employee_id)

    def delete_employee(self, employee_id: str) -> bool:
        if not is_valid_thread_id(employee_id):
            return False
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM workspace_memories WHERE scope='employee' AND owner_id=?", (employee_id,))
            return conn.execute("DELETE FROM employees WHERE id=?", (employee_id,)).rowcount > 0

    # ---------- 员工间消息 ----------

    def append_message(
        self,
        group_id: str,
        *,
        employee_id: str = "",
        kind: str = "report",
        content: str = "",
        meta: Optional[dict[str, Any]] = None,
        run_id: str = "",
        task_id: str = "",
        recipient_employee_id: str = "",
        reply_to: str = "",
        status: str = "",
        visibility: str = "group",
        sender_kind: str = "",
        recipient_employee_ids: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        追加一条群组消息（员工之间、员工与用户之间**可见的成品文本**）。

        ★ 这里是三条禁令的唯一落点（施工图 §4.2）
        截断与过滤写在调用方（原先是 `orchestrator`）会被绕过 —— 多一个写入点
        就多一处「忘了截」。所以正文一进这个函数就先过 `sanitize_group_text`。

        ★ `request` 没有收件人时改成 `blocker`
        「向同事要一件可指名的东西」而没有指名，语义上就退化成「我缺东西」。
        留成 `request` 的话它永远进不了任何人的收件箱，而预算已经花掉了。
        """
        if not self.enabled or not is_valid_thread_id(group_id):
            return None
        kind = normalize_kind(kind)
        recipient_employee_id = (recipient_employee_id or "").strip()
        recipients = list(dict.fromkeys(recipient_employee_ids or ([recipient_employee_id] if recipient_employee_id else [])))
        if kind == "request" and not recipients:
            logger.info("一条 request 没有收件人，按 blocker 落库（广播的「我缺东西」）")
            kind = "blocker"
        if sender_kind == "user" and kind == "goal":
            content, note = (content or "").strip()[:4000], ""
        else:
            content, note = sanitize_group_text(content)
        if note:
            logger.info("群聊消息已按禁令处理（group=%s kind=%s）：%s", group_id, kind, note)
        if not content:
            return None
        mid = _new_id()
        # sender_kind 缺省由 employee_id 推：有作者就是员工，没有就是主持人的成品文本
        sender = sender_kind or ("employee" if employee_id else "coordinator")
        with self._db.transaction() as conn:
            ok = conn.execute(
                "INSERT INTO messages(id, group_id, employee_id, kind, content, created_at, meta, "
                "run_id, task_id, sender_kind, recipient_employee_id, reply_to, status, visibility) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mid,
                    group_id,
                    employee_id or None,
                    kind,
                    content,
                    now_iso(),
                    json.dumps(meta or {}, ensure_ascii=False),
                    run_id,
                    task_id,
                    sender,
                    recipient_employee_id,
                    reply_to,
                    status,
                    visibility or "group",
                ),
            )
            if kind == "request" and recipients:
                from datetime import datetime, timedelta, timezone
                deadline = (datetime.now(timezone.utc) + timedelta(seconds=settings.WORKSPACE_DELIVERY_TIMEOUT_SECONDS)).isoformat()
                conn.executemany("INSERT OR IGNORE INTO deliveries(message_id,employee_id,deadline,updated_at) VALUES(?,?,?,?)",
                                 [(mid, recipient, deadline, now_iso()) for recipient in recipients])
            if sender == "user" and (meta or {}).get("client_id"):
                conn.execute("INSERT INTO message_requests(group_id,client_id,message_id,run_id) VALUES(?,?,?,?) "
                             "ON CONFLICT(group_id,client_id) DO UPDATE SET message_id=excluded.message_id,run_id=excluded.run_id "
                             "WHERE message_requests.message_id=''",
                             (group_id, meta["client_id"], mid, run_id))
        if not ok:
            return None
        msg = self.get_message(mid)
        if msg is not None and kind == "request" and recipients:
            from app.workspace.messaging import messenger
            for recipient in recipients:
                messenger.arm_deadline({**msg, "recipient_employee_id": recipient})
        if msg is not None and msg.get("visibility") == "group":
            # ★ 广播长在唯一写入口里，不是「记得调用」（`events.py` 模块头那条纪律）
            # 协调者这一轮多了三个写入点（ask / answer / handoff）。漏一处的症状
            # 不是报错，是「同事的话要刷新才看得见」—— 而那正是互聊看起来像坏了。
            events.message(self.enrich_message(msg))
        return msg

    def get_message(self, message_id: str) -> Optional[dict[str, Any]]:
        if not is_valid_thread_id(message_id):
            return None
        row = self._db.query_one(
            "SELECT rowid AS seq, * FROM messages WHERE id = ?", (message_id,)
        )
        return _row_to_message(row) if row is not None else None

    def enrich_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """
        补上「谁说的 / 说给谁」的人话名字（派生字段，不落库）。

        ★ 为什么不存名字
        `employee_id` 是唯一真相：员工改名后历史消息要跟着变。存名字会留下
        「旧消息还是旧名」的脏数据，而那是审计里最难看的一类错。
        """
        out = dict(msg)
        author = out.get("employee_id")
        if author:
            employee = self.get_employee(str(author))
            if employee is not None:
                out["employee_name"] = employee.get("name") or ""
                out["employee_tagline"] = employee.get("tagline") or ""
        recipient = out.get("recipient_employee_id")
        if recipient:
            target = self.get_employee(str(recipient))
            if target is not None:
                out["recipient_name"] = target.get("name") or ""
            delivery = self._db.query_one("SELECT state,last_error FROM deliveries WHERE message_id=? AND employee_id=?",
                                          (msg["id"], recipient))
            if delivery:
                out["delivery_state"] = delivery["state"]
                out["delivery_error"] = delivery["last_error"]
        else:
            deliveries = self._db.query_all("SELECT state,last_error FROM deliveries WHERE message_id=?", (msg["id"],))
            if deliveries:
                states = {d["state"] for d in deliveries}
                out["delivery_state"] = next((s for s in ("failed", "processing", "pending", "read", "cancelled", "answered") if s in states), "answered")
                out["delivery_error"] = next((d["last_error"] for d in deliveries if d["last_error"]), "")
        return out

    # ---------- 定向消息：收件箱 / 预算 / 协商图 ----------

    def pending_inbox(
        self, group_id: str, *, employee_id: str, run_id: str = "", limit: int = 0
    ) -> list[dict[str, Any]]:
        """认领轮可见的未解决定向消息；读过仍保留，回答后才移出。

        模型调用由认领轮和运行预算约束。实际投递与执行中的消息处理见 inbox.py。
        """
        if not is_valid_thread_id(group_id) or not employee_id:
            return []
        cap = max(1, int(limit or settings.INBOX_MAX))
        where = ["group_id = ?", "recipient_employee_id = ?", "status IN ('','seen')"]
        params: list[Any] = [group_id, employee_id]
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        rows = self._db.query_all(
            f"SELECT rowid AS seq, * FROM messages WHERE {' AND '.join(where)} "
            "ORDER BY rowid ASC LIMIT ?",
            (*params, cap),
        )
        return [_row_to_message(dict(r)) for r in rows]

    def mark_inbox_seen(self, message_ids: list[str]) -> int:
        """把这些消息标成「已读过」（只动 `status=''` 的那些，所以重复调用无害）。"""
        return self._mark_status(message_ids, STATUS_SEEN)

    def mark_answered(self, message_id: str) -> bool:
        """
        一条 request 被回答了。**CAS**：只有还没变成 `answered` 才生效。

        返回 False 的含义是「它早就被回答过了」——调用方据此不要重复写第二条答复。

        ★ 谓词是 `<> answered` 而不是 `= ''`
        认领轮在回答**之前**就把这一批收件箱标成了 `seen`（读过的都得标，不然下轮重读）。
        只认 `''` 的话，第一条真答复会被判成「已经有人回过了」而写不进库 ——
        提问的人永远等不到回音，症状与没做这条护栏一模一样。
        """
        if not is_valid_thread_id(message_id):
            return False
        with self._db.transaction() as conn:
            changed = conn.execute("UPDATE messages SET status=? WHERE id=? AND status<>?",
                                   (STATUS_ANSWERED, message_id, STATUS_ANSWERED)).rowcount
            conn.execute("UPDATE deliveries SET state='answered',updated_at=? WHERE message_id=?", (now_iso(), message_id))
        return changed == 1

    def _mark_status(self, message_ids: list[str], status: str) -> int:
        """只动还没被处置过（`status = ''`）的那些，所以重复调用无害、也不会把 `answered` 改回 `seen`。"""
        written = 0
        for mid in message_ids:
            mid = str(mid or "").strip()
            if not is_valid_thread_id(mid):
                continue
            written += self._db.execute(
                "UPDATE messages SET status = ? WHERE id = ? AND status = ''",
                (status, mid),
            )
        return written

    def count_directed(self, run_id: str) -> int:
        """
        护栏 1 的计数：这次 run 里四类定向消息（request/commitment/deliverable/blocker）共几条。

        ★ 数库里的行，不数内存里的计数器
        进程重启后内存计数器归零，而那条 run 可能还在跑（阶段 1 的 `expired` 对账之前，
        审批挂起的 run 就停在那里）。计数器会撒谎，行数不会。
        """
        if not run_id:
            return 0
        marks = ",".join("?" for _ in DIRECTED_KINDS)
        kinds = sorted(DIRECTED_KINDS)
        value = self._db.query_scalar(
            f"SELECT COUNT(*) FROM messages WHERE run_id = ? AND kind IN ({marks})",
            (run_id, *kinds),
        )
        return int(value or 0)

    def negotiation_edges(self, run_id: str) -> list[dict[str, Any]]:
        """
        护栏 4 的原料：`(作者, 收件人, 回答的那条)` 三类定向消息。

        行数已被护栏 1 限死（默认 ≤24），所以环检测在 Python 里做 DFS 就够，
        不需要图数据库，也不需要递归深度上限之外的算法。
        """
        if not run_id:
            return []
        marks = ",".join("?" for _ in DIRECTED_KINDS)
        kinds = sorted(DIRECTED_KINDS)
        rows = self._db.query_all(
            "SELECT id, employee_id, recipient_employee_id, reply_to, kind "
            f"FROM messages WHERE run_id = ? AND kind IN ({marks}) ORDER BY rowid ASC",
            (run_id, *kinds),
        )
        return [dict(r) for r in rows]

    def list_messages(
        self,
        group_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        after_seq: int = 0,
        run_id: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        """
        列出群组消息（按时间正序——群组是时间线，不是最新在前的会话列表）。

        ★ `seq` 就是 SQLite 的 `rowid`，它是本项目唯一可比较的游标
        （`messages.id` 是 `uuid4().hex[:16]`，不可比较，所以做不了 Last-Event-ID 式补拉）。
        断线重连带 `after_seq=客户端见过的最大值`，即可补齐漏掉的几条而不重放全量。
        """
        if not is_valid_thread_id(group_id):
            return [], 0
        where = ["group_id = ?", "visibility = 'group'"]
        params: list[Any] = [group_id]
        if after_seq > 0:
            where.append("rowid > ?")
            params.append(after_seq)
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        clause = " AND ".join(where)
        total = self._db.query_scalar(f"SELECT COUNT(*) FROM messages WHERE {clause}", tuple(params))
        rows = self._db.query_all(
            f"SELECT rowid AS seq, * FROM messages WHERE {clause} "
            "ORDER BY rowid ASC LIMIT ? OFFSET ?",
            (*params, max(1, limit), max(0, offset)),
        )
        return [_row_to_message(dict(r)) for r in rows], int(total or 0)

    # ---------- 员工私聊线程 ----------

    def link_thread(self, employee_id: str, thread_id: str, kind: str = "private") -> bool:
        """登记 `employee_id ↔ thread_id` 映射（幂等）。"""
        if not is_valid_thread_id(employee_id) or not is_valid_thread_id(thread_id):
            return False
        kind = kind if kind in ("private", "orchestration") else "private"
        try:
            self._db.execute(
                "INSERT OR IGNORE INTO employee_threads(id, employee_id, thread_id, kind, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (_new_id(), employee_id, thread_id, kind, now_iso()),
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("登记员工线程失败: %s", e)
            return False

    def stats(self) -> dict[str, Any]:
        return self._db.stats()


#: 进程级单例
workspace_store = WorkspaceStore()


__all__ = ["WorkspaceStore", "workspace_store"]
