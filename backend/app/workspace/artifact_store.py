"""Addressable, versioned deliveries, separate from conversation transcripts."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.conversations.models import now_iso
from app.workspace.db import WorkspaceDB, workspace_db


class ArtifactStore:
    def __init__(self, db: WorkspaceDB = workspace_db):
        self.db = db

    def blob_path(self, artifact_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{24}", artifact_id):
            raise ValueError("非法成果标识")
        root = self.db.path.parent.resolve()
        target = (root / "artifact-files" / f"{artifact_id}.bin").resolve()
        if root not in target.parents:
            raise ValueError("成果快照路径越界")
        return target

    def file_path(self, artifact: dict) -> Path:
        snapshot = self.blob_path(artifact["id"])
        if snapshot.is_file():
            return snapshot
        from app.core.sandbox import resolve_in_sandbox
        return resolve_in_sandbox(artifact["path"])

    def list(self, run_id: str, card_id: str = "") -> list[dict]:
        return self.db.query_all("SELECT * FROM artifacts WHERE run_id=?" + (" AND card_id=?" if card_id else "") +
                                 " ORDER BY created_at,version", (run_id, card_id) if card_id else (run_id,))

    def register_task(self, task: dict) -> list[dict]:
        run_id, card_id = str(task.get("run_id") or ""), str(task.get("board_card_id") or "")
        if not run_id or task.get("status") != "succeeded":
            return []
        if card_id:
            card = self.db.query_one("SELECT run_id,owner_employee_id,task_id FROM run_tasks WHERE id=?", (card_id,))
            if not card or card["run_id"] != run_id or (card["owner_employee_id"] and card["owner_employee_id"] != task.get("employee_id")):
                raise ValueError("成果与任务卡的归属不一致")
            if card["task_id"] and card["task_id"] != task["task_id"]:
                return self.list(run_id, card_id)  # A superseded attempt cannot publish over its replacement.
        # One immutable note per execution attempt makes completion callbacks idempotent.
        aid = hashlib.sha256(f"task:{task['task_id']}".encode()).hexdigest()[:24]
        if self.db.query_one("SELECT id FROM artifacts WHERE id=?", (aid,)):
            return self.list(run_id, card_id)
        body = str(task.get("result") or "")
        entries = [(aid, "note", "", body, str(task.get("title") or "工作成果"), hashlib.sha256(body.encode()).hexdigest())]
        from app.core.sandbox import resolve_in_sandbox
        for file in task.get("files") or []:
            path = str(file.get("path") or "")
            target = resolve_in_sandbox(path)
            if not target.is_file() or target.stat().st_size == 0:
                raise ValueError(f"成果文件不存在或为空：{path}")
            data = target.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            fid = hashlib.sha256(f"{task['task_id']}:{path}".encode()).hexdigest()[:24]
            snapshot = self.blob_path(fid)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            temporary = snapshot.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(snapshot)
            entries.append((fid, "file", path, "", Path(path).name, digest))
        with self.db.transaction() as conn:
            for item_id, carrier, path, content, name, digest in entries:
                version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM artifacts WHERE run_id=? AND carrier=? AND "
                                       + ("path=?" if carrier == "file" else "card_id=?"),
                                       (run_id, carrier, path if carrier == "file" else card_id)).fetchone()[0]
                conn.execute("INSERT OR IGNORE INTO artifacts(id,run_id,card_id,employee_id,kind,name,carrier,path,body,summary,sha256,version,visibility,created_at) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (item_id, run_id, card_id, task.get("employee_id", ""), carrier, name, carrier,
                              path, content, body[:500], digest, version, "project", now_iso()))
            if card_id:
                ids = [r[0] for r in conn.execute("SELECT id FROM artifacts WHERE run_id=? AND card_id=?", (run_id, card_id))]
                conn.execute("UPDATE run_tasks SET artifact_ids=?,updated_at=? WHERE id=? AND run_id=?",
                             (json.dumps(ids), now_iso(), card_id, run_id))
        return self.list(run_id, card_id)

    def validate(self, run_id: str, card_id: str) -> bool:
        rows = self.list(run_id, card_id)
        if not rows:
            return False
        latest = {}
        for row in rows:
            key = (row["carrier"], row["path"] if row["carrier"] == "file" else row["card_id"])
            if key not in latest or row["version"] > latest[key]["version"]:
                latest[key] = row
        for row in latest.values():
            if row["carrier"] == "file":
                try:
                    path = self.file_path(row)
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                        return False
                except (ValueError, OSError):
                    return False
        return True


artifact_store = ArtifactStore()
