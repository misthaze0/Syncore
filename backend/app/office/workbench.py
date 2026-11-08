"""Local office documents, optimistic saves and recoverable version history."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DocumentConflict(Exception):
    def __init__(self, document: dict):
        super().__init__("文档已在另一个窗口更新，请先保存本地副本或重新加载")
        self.document = document


class DocumentStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
                    kind TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
                    trashed INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    messages TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, title TEXT NOT NULL,
                    content TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, label TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS versions_document ON versions(document_id, revision DESC);
            """)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _document(row) -> dict[str, Any]:
        if row is None:
            raise KeyError("文档不存在")
        result = dict(row)
        result["favorite"] = bool(result["favorite"])
        result["trashed"] = bool(result["trashed"])
        result["messages"] = json.loads(result["messages"])
        return result

    @staticmethod
    def _valid_id(document_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", document_id):
            raise KeyError("文档不存在")

    def list(self) -> list[dict]:
        from app.office.workbench_html import plain_text

        with self._lock, self._connection() as db:
            rows = db.execute("SELECT * FROM documents ORDER BY updated_at DESC").fetchall()
        documents = []
        for row in rows:
            item = self._document(row)
            text = plain_text(item.pop("content"))
            item.pop("messages")
            item.update(excerpt=text[:100], characters=len(re.sub(r"\s", "", text)))
            documents.append(item)
        return documents

    def get(self, document_id: str) -> dict:
        self._valid_id(document_id)
        with self._lock, self._connection() as db:
            return self._document(db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone())

    def create(self, title: str, content: str, kind: str = "document", messages: list | None = None) -> dict:
        document_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection() as db:
            db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, 0, 0, 1, ?, ?, ?)",
                       (document_id, title, content, kind, now, now, json.dumps(messages or [], ensure_ascii=False)))
        return self.get(document_id)

    def update(self, document_id: str, expected_revision: int, patch: dict, reason: str = "autosave") -> dict:
        self._valid_id(document_id)
        allowed = {"title", "content", "kind", "favorite", "trashed", "messages"}
        with self._lock, self._connection() as db:
            # The write reservation covers both the version check and the write, across processes.
            db.execute("BEGIN IMMEDIATE")
            old = self._document(db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone())
            if old["revision"] != expected_revision:
                raise DocumentConflict(old)
            changes = {key: value for key, value in patch.items() if key in allowed and old[key] != value}
            if not changes and reason != "checkpoint":
                return old
            now = datetime.now(timezone.utc).isoformat()
            content_changed = "content" in changes or "title" in changes
            latest = db.execute("SELECT created_at FROM versions WHERE document_id=? ORDER BY revision DESC LIMIT 1", (document_id,)).fetchone()
            needs_snapshot = content_changed or reason == "checkpoint"
            if reason == "autosave" and latest:
                needs_snapshot = needs_snapshot and (datetime.now(timezone.utc) - datetime.fromisoformat(latest[0])).total_seconds() > 60
            if needs_snapshot:
                label = {"ai": "AI 修改前", "restore": "恢复版本前", "manual": "手动保存前", "checkpoint": "手动保存", "autosave": "自动保存"}.get(reason, "编辑前")
                db.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (uuid.uuid4().hex, document_id, old["title"], old["content"], old["revision"], now, label))
                db.execute("DELETE FROM versions WHERE document_id=? AND id NOT IN (SELECT id FROM versions WHERE document_id=? ORDER BY revision DESC LIMIT 40)", (document_id, document_id))
            changes.update(revision=old["revision"] + 1, updated_at=now)
            if "messages" in changes:
                changes["messages"] = json.dumps(changes["messages"], ensure_ascii=False)
            assignments = ", ".join(f"{key}=?" for key in changes)
            db.execute(f"UPDATE documents SET {assignments} WHERE id=?", (*changes.values(), document_id))
            return self._document(db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone())

    def versions(self, document_id: str) -> list[dict]:
        self.get(document_id)
        with self._lock, self._connection() as db:
            return [dict(row) for row in db.execute("SELECT id, title, revision, created_at, label FROM versions WHERE document_id=? ORDER BY revision DESC", (document_id,))]

    def version(self, document_id: str, version_id: str) -> dict:
        self._valid_id(document_id)
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM versions WHERE document_id=? AND id=?", (document_id, version_id)).fetchone()
            if row is None:
                raise KeyError("版本不存在")
            return dict(row)


def _default_store():
    from app.config import settings

    return DocumentStore(settings.backend_root / "data" / "office" / "workbench.db")


document_store = _default_store()
