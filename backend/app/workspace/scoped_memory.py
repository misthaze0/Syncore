"""Project facts and employee experience with explicit, independently queryable ownership."""
from __future__ import annotations

from app.conversations.models import now_iso
from app.config import settings
from app.workspace.db import WorkspaceDB, workspace_db


class ScopedMemory:
    def __init__(self, db: WorkspaceDB = workspace_db):
        self.db = db

    def put(self, scope: str, owner_id: str, key: str, value: str,
            evidence: str = "", author_id: str = "") -> None:
        if scope not in ("project", "employee") or not owner_id or not key.strip() or not value.strip():
            raise ValueError("记忆需要有效范围、归属、键和内容")
        table = "workspaces" if scope == "project" else "employees"
        if not self.db.query_one(f"SELECT id FROM {table} WHERE id=?", (owner_id,)):
            raise ValueError("记忆归属不存在")
        self.db.execute("INSERT INTO workspace_memories(scope,owner_id,key,value,evidence,author_id,updated_at) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,owner_id,key) DO UPDATE SET "
                        "value=excluded.value,evidence=excluded.evidence,author_id=excluded.author_id,updated_at=excluded.updated_at",
                        (scope, owner_id, key.strip()[:100], value.strip()[:2000], evidence[:2000], author_id, now_iso()))

    def list(self, employee_id: str) -> list[dict]:
        employee = self.db.query_one("SELECT workspace_id FROM employees WHERE id=?", (employee_id,))
        if not employee:
            return []
        return self.db.query_all("SELECT * FROM workspace_memories WHERE (scope='employee' AND owner_id=?) "
                                 "OR (scope='project' AND owner_id=?) ORDER BY updated_at DESC LIMIT 30",
                                 (employee_id, employee["workspace_id"]))

    def prompt(self, employee_id: str) -> str:
        rows = self.list(employee_id)
        if not settings.MEMORY_ENABLED:
            rows = [r for r in rows if r["scope"] == "project"]
        lines = [f"- [{r['scope']} / {r['updated_at']}] {r['value']}（依据：{r['evidence'] or '员工经验，待核实'}）" for r in rows]
        return ("【当前项目事实与本人经验；以本轮用户要求和工具事实为准】\n" + "\n".join(lines))[:6000] if lines else ""

    def delete(self, scope: str, owner_id: str, key: str) -> bool:
        return bool(self.db.execute("DELETE FROM workspace_memories WHERE scope=? AND owner_id=? AND key=?", (scope, owner_id, key)))


scoped_memory = ScopedMemory()
