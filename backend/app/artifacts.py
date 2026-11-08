"""
产物版本 —— 每次成功写入后给文件留一份不可变快照

★ 为什么在轮次落盘后做，而不是每个工具调用时做

同一个文件一轮里可能被写多次。逐次快照会留下用户从没见过的中间态，
工具失败回滚时还会指向一份已被覆盖的内容。轮次落盘时文件已经定稿，
一轮一条，正好对应用户看到的「这次生成了什么」。

★ 为什么按内容寻址

同一份内容重复写入不产生新版本；blob 用 sha256 命名，索引只记元数据，
删版本时按引用计数清理。这样「把第二版恢复回来」只需复制一个 blob。

★ 边界

只收沙箱内的路径。用户在授权目录里的文件不进版本库 ——
那是用户自己的目录，我们只负责「Agent 交付的产物」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_INDEX_VERSION = 1
_LOCK = threading.RLock()
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def _thread_dir(thread_id: str) -> Path:
    return settings.artifact_revisions_path / thread_id


def _index_path(thread_id: str) -> Path:
    return _thread_dir(thread_id) / "index.json"


def _blobs_dir(thread_id: str) -> Path:
    p = _thread_dir(thread_id) / "blobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_index(thread_id: str) -> Dict[str, Any]:
    path = _index_path(thread_id)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("paths"), dict):
                return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("产物索引读取失败，按空索引处理 %s: %s", thread_id, e)
    return {"v": _INDEX_VERSION, "thread_id": thread_id, "paths": {}}


def _save_index(thread_id: str, data: Dict[str, Any]) -> None:
    path = _index_path(thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"index.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix if _SUFFIX_RE.match(suffix) else ""


def _revision_id() -> str:
    return f"{int(datetime.now().timestamp() * 1000)}-{uuid.uuid4().hex[:6]}"


def _blob_referenced(paths: Dict[str, Any], blob_name: str) -> bool:
    for entry in paths.values():
        for revision in entry.get("revisions") or []:
            if revision.get("blob") == blob_name:
                return True
    return False


def record_turn(
    thread_id: str,
    files: Optional[Iterable[Dict[str, Any]]],
    turn_id: str = "",
) -> Dict[str, int]:
    """
    给本轮写入的文件做快照。内容没变就不产生新版本。

    返回 `{"created": n, "skipped": n}`，只用于日志与自检；
    任何单个文件的失败都不影响其它文件，更不影响轮次落盘。
    """
    summary = {"created": 0, "skipped": 0}
    if not thread_id or not _THREAD_ID_RE.match(thread_id) or not files:
        return summary

    try:
        from app.core.sandbox import resolve_in_sandbox
    except Exception as e:  # noqa: BLE001
        logger.warning("产物版本不可用（沙箱模块加载失败）: %s", e)
        return summary

    max_bytes = max(0, settings.ARTIFACT_REVISION_MAX_MB) * 1024 * 1024
    keep = max(1, settings.ARTIFACT_REVISION_KEEP)

    with _LOCK:
        index = _load_index(thread_id)
        paths: Dict[str, Any] = index.setdefault("paths", {})
        changed = False

        for item in files:
            rel = str(item.get("path") or "").strip()
            if not rel:
                continue
            try:
                target = resolve_in_sandbox(rel)
            except PermissionError:
                summary["skipped"] += 1
                continue
            if not target.is_file():
                summary["skipped"] += 1
                continue
            try:
                size = target.stat().st_size
                if max_bytes and size > max_bytes:
                    logger.info("产物超过版本快照上限，跳过: %s (%d bytes)", rel, size)
                    summary["skipped"] += 1
                    continue
                digest = _sha256_file(target)
            except OSError as e:
                logger.warning("产物读取失败，跳过快照 %s: %s", rel, e)
                summary["skipped"] += 1
                continue

            entry = paths.setdefault(rel, {"revisions": []})
            revisions: List[Dict[str, Any]] = entry.setdefault("revisions", [])
            if revisions and revisions[-1].get("sha256") == digest:
                continue

            suffix = _safe_suffix(target)
            blob_name = f"{digest}{suffix}"
            blob = _blobs_dir(thread_id) / blob_name
            if not blob.is_file():
                tmp = blob.with_name(f"{blob.name}.{uuid.uuid4().hex}.tmp")
                try:
                    shutil.copyfile(target, tmp)
                    os.replace(tmp, blob)
                except OSError as e:
                    logger.warning("产物快照写入失败 %s: %s", rel, e)
                    summary["skipped"] += 1
                    continue

            revisions.append(
                {
                    "revision_id": _revision_id(),
                    "sha256": digest,
                    "size": size,
                    "created_at": _now_iso(),
                    "turn_id": turn_id,
                    "tool": str(item.get("tool") or ""),
                    "suffix": suffix,
                    "blob": blob_name,
                }
            )
            while len(revisions) > keep:
                dropped = revisions.pop(0)
                blob_name_dropped = str(dropped.get("blob") or "")
                if blob_name_dropped and not _blob_referenced(paths, blob_name_dropped):
                    try:
                        (_blobs_dir(thread_id) / blob_name_dropped).unlink()
                    except OSError:
                        pass
            changed = True
            summary["created"] += 1

        if changed:
            try:
                _save_index(thread_id, index)
            except OSError as e:
                logger.warning("产物索引写入失败 %s: %s", thread_id, e)
                summary["created"] = 0
    return summary


def revision_summary(thread_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """`{相对路径: [版本元数据…]}`，给 /files 接口补字段用。"""
    if not thread_id or not _THREAD_ID_RE.match(thread_id):
        return {}
    with _LOCK:
        index = _load_index(thread_id)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for rel, entry in (index.get("paths") or {}).items():
        out[rel] = [
            {
                "revision_id": str(r.get("revision_id") or ""),
                "sha256": str(r.get("sha256") or ""),
                "size": int(r.get("size") or 0),
                "created_at": str(r.get("created_at") or ""),
                "turn_id": str(r.get("turn_id") or ""),
                "tool": str(r.get("tool") or ""),
                "restored_from": str(r.get("restored_from") or ""),
            }
            for r in (entry.get("revisions") or [])
        ]
    return out


def open_revision(thread_id: str, rel: str, revision_id: str) -> Optional[Path]:
    """取某个历史版本的 blob（只读）。找不到返回 None。"""
    if not thread_id or not _THREAD_ID_RE.match(thread_id):
        return None
    with _LOCK:
        index = _load_index(thread_id)
        entry = (index.get("paths") or {}).get(rel) or {}
        for revision in entry.get("revisions") or []:
            if str(revision.get("revision_id")) == revision_id:
                blob = _thread_dir(thread_id) / "blobs" / str(revision.get("blob") or "")
                return blob if blob.is_file() else None
    return None


def restore_revision(thread_id: str, rel: str, revision_id: str, target: Path) -> bool:
    """
    把某个历史版本写回沙箱里的目标路径，并记一条 `restored_from` 版本。

    恢复本身也是一次内容变更：不记的话，下次轮次快照会把它当成「未知来源」，
    历史里就断了一环。
    """
    if not thread_id or not _THREAD_ID_RE.match(thread_id):
        return False
    with _LOCK:
        index = _load_index(thread_id)
        paths: Dict[str, Any] = index.setdefault("paths", {})
        entry = paths.get(rel) or {}
        revisions: List[Dict[str, Any]] = entry.get("revisions") or []
        wanted = next(
            (r for r in revisions if str(r.get("revision_id")) == revision_id),
            None,
        )
        if wanted is None:
            return False
        blob = _thread_dir(thread_id) / "blobs" / str(wanted.get("blob") or "")
        if not blob.is_file():
            return False

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
            shutil.copyfile(blob, tmp)
            os.replace(tmp, target)
        except OSError as e:
            logger.warning("恢复产物失败 %s: %s", rel, e)
            return False

        if not revisions or revisions[-1].get("sha256") != wanted.get("sha256"):
            revisions.append(
                {
                    "revision_id": _revision_id(),
                    "sha256": wanted.get("sha256"),
                    "size": wanted.get("size"),
                    "created_at": _now_iso(),
                    "turn_id": "",
                    "tool": "restore",
                    "suffix": wanted.get("suffix") or "",
                    "blob": wanted.get("blob"),
                    "restored_from": revision_id,
                }
            )
            try:
                _save_index(thread_id, index)
            except OSError as e:
                logger.warning("恢复后索引写入失败 %s: %s", thread_id, e)
    return True


def purge_thread(thread_id: str) -> None:
    """删掉一个会话的全部版本数据（会话被删除 / 清空时调用）。"""
    if not thread_id or not _THREAD_ID_RE.match(thread_id):
        return
    with _LOCK:
        shutil.rmtree(_thread_dir(thread_id), ignore_errors=True)


__all__ = [
    "open_revision",
    "purge_thread",
    "record_turn",
    "restore_revision",
    "revision_summary",
]
