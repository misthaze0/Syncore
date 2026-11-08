"""
提问留痕 —— 「她当时问了什么、用户怎么答的、还是没人理」的可查询记录。

★ 为什么不复用审批留痕

两者都是「停下来等人」，但审计问题完全不同：
   审批：谁授权了哪一次危险操作、由谁放行（用户 / 规则 / 预授权）
   提问：模型缺了哪条信息、用户给了什么答案、有没有跳过
混进同一份记录后，审批面板会出现一堆「没有批准任何人」的条目；
而提问面板会被「哪一次是真正的授权」淹没。事件类型与审计落点都必须分开。

★ 为什么 append-only

一次提问有两个时刻（请求、回答/跳过/超时），由两个不同的代码位置产生。
就地更新一条记录会让两个位置争同一份文件；追加快照再按 `record_id`
折叠取最新，是审批留痕已经验证过的那套做法（见 `app/approval/records.py`）。

⚠️ 问答里可能包含用户的业务信息（预算、姓名、偏好）。
`data/questions/` 与 `backend/.env` 同级敏感，必须与 trace / approvals
一起列进 `.gitignore`。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from app.config import settings
from app.questions.models import (
    QUESTION_STATUSES,
    STATUS_ANSWERED,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
)

logger = logging.getLogger(__name__)

RECORD_VERSION = 1

STATUS_LABELS = {
    STATUS_PENDING: "等待中",
    STATUS_ANSWERED: "已回答",
    STATUS_SKIPPED: "已跳过",
    STATUS_TIMEOUT: "超时未答",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


@dataclass
class QuestionRecord:
    """一次提问的完整留痕（请求与回答折叠后的结果）"""

    record_id: str
    question_id: str = ""
    thread_id: str = ""
    call_id: str = ""
    run_id: str = ""
    employee_id: str = ""
    requestor: str = ""
    questions: List[Dict[str, Any]] = field(default_factory=list)
    answers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    status: str = STATUS_PENDING
    reason: str = ""
    timeout_seconds: int = 0
    requested_at: str = ""
    resolved_at: str = ""
    latency_ms: Optional[float] = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status_label"] = self.status_label
        return data


class QuestionStore:
    """提问记录仓库（append-only JSONL + 内存环形缓冲）"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._records: Dict[str, QuestionRecord] = {}
        self._order: Deque[str] = deque()
        self._written = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "QUESTION_RECORD_ENABLED", True))

    @property
    def path(self) -> Path:
        return self._path or settings.question_record_path

    @property
    def memory_limit(self) -> int:
        return max(1, int(getattr(settings, "QUESTION_RECORD_MEMORY", 500)))

    # ---------- 写入 ----------
    def _append(self, event: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._written += 1
        except Exception as e:  # noqa: BLE001
            # 留痕是旁路：它挂掉不该让一次提问无法发出（不变量 18）
            logger.warning("提问留痕写入失败（不影响提问本身）: %s", e)

    def _remember(self, record: QuestionRecord) -> None:
        with self._lock:
            if record.record_id not in self._records:
                self._order.append(record.record_id)
            self._records[record.record_id] = record
            while len(self._order) > self.memory_limit:
                evicted = self._order.popleft()
                if evicted != record.record_id:
                    self._records.pop(evicted, None)
            self._append({"v": RECORD_VERSION, "event": "snapshot", "record": record.to_dict()})

    # ---------- 生命周期 ----------
    def open_question(
        self,
        *,
        question_id: str,
        thread_id: str,
        call_id: str,
        questions: List[Dict[str, Any]],
        run_id: str = "",
        employee_id: str = "",
        requestor: str = "",
        timeout_seconds: int = 0,
    ) -> QuestionRecord:
        """登记一次提问（在推送 `question` 事件之前调用）。"""
        record = QuestionRecord(
            record_id=uuid.uuid4().hex[:16],
            question_id=question_id,
            thread_id=thread_id,
            call_id=call_id,
            run_id=run_id,
            employee_id=employee_id,
            requestor=requestor,
            questions=[dict(q) for q in questions if isinstance(q, dict)],
            timeout_seconds=int(timeout_seconds or 0),
            requested_at=_now_iso(),
        )
        self._remember(record)
        return record

    def settle(
        self,
        record_id: str,
        *,
        status: str,
        answers: Optional[Dict[str, Dict[str, Any]]] = None,
        reason: str = "",
    ) -> Optional[QuestionRecord]:
        """登记结果（回答 / 跳过 / 超时）。未知状态按 skipped 处理。"""
        record = self._records.get(record_id)
        if record is None:
            return None
        record.status = status if status in QUESTION_STATUSES else STATUS_SKIPPED
        record.answers = dict(answers or {})
        record.reason = reason or record.status
        record.resolved_at = _now_iso()
        record.latency_ms = _elapsed_ms(record.requested_at, record.resolved_at)
        self._remember(record)
        return record

    # ---------- 查询 ----------
    def get(self, record_id: str) -> Optional[QuestionRecord]:
        return self._records.get(record_id)

    def find_by_call(
        self,
        thread_id: str,
        call_id: str,
        *,
        run_id: str = "",
        employee_id: str = "",
        question_id: str = "",
    ) -> Optional[QuestionRecord]:
        """
        按四元组（+ question_id）查最近的记录。

        ★ 后两维与 question_id 都是**精确匹配**，不做宽松兜底
        群组里 thread_id 共用、模型给的 call_id 并发下会撞；
        挑错一条的后果是「甲的回答写进乙的留痕」。
        """
        for record_id in reversed(self._order):
            record = self._records.get(record_id)
            if record is None:
                continue
            if (
                record.thread_id == thread_id
                and record.call_id == call_id
                and record.run_id == run_id
                and record.employee_id == employee_id
                and (not question_id or record.question_id == question_id)
            ):
                return record
        return None

    def list_records(
        self,
        *,
        thread_id: Optional[str] = None,
        status: Optional[str] = None,
        employee_id: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[QuestionRecord]:
        out: List[QuestionRecord] = []
        for record_id in reversed(self._order):
            record = self._records.get(record_id)
            if record is None:
                continue
            if thread_id and record.thread_id != thread_id:
                continue
            if status and record.status != status:
                continue
            if employee_id and record.employee_id != employee_id:
                continue
            if run_id and record.run_id != run_id:
                continue
            out.append(record)
            if len(out) >= max(1, limit):
                break
        return out

    def stats(self) -> Dict[str, Any]:
        by_status: Dict[str, int] = {}
        latencies: List[float] = []
        for record in self._records.values():
            by_status[record.status] = by_status.get(record.status, 0) + 1
            if record.latency_ms is not None:
                latencies.append(record.latency_ms)
        return {
            "enabled": self.enabled,
            "path": str(self.path),
            "in_memory": len(self._records),
            "written": self._written,
            "by_status": by_status,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "pending": by_status.get(STATUS_PENDING, 0),
        }

    # ---------- 启动清理 / 回填 ----------
    def cleanup_old_records(self) -> Dict[str, int]:
        """把超过保留期的记录归档到 `archive/records-YYYY-MM.jsonl`。"""
        result = {"kept": 0, "archived": 0, "errors": 0}
        if not self.enabled:
            return result

        retention = int(getattr(settings, "QUESTION_RETENTION_DAYS", 90))
        if retention <= 0:
            return result

        path = self.path
        if not path.is_file():
            return result

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
        keep_lines: List[str] = []
        archive_by_month: Dict[str, List[str]] = {}

        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        keep_lines.append(line)
                        continue

                    record_data = (
                        event.get("record", {}) if event.get("event") == "snapshot" else {}
                    )
                    requested_at = str(record_data.get("requested_at", ""))
                    ts: Optional[datetime] = None
                    if requested_at:
                        try:
                            ts = datetime.fromisoformat(requested_at)
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                        except ValueError:
                            ts = None
                    if ts is None:
                        keep_lines.append(line)
                    elif ts < cutoff:
                        archive_by_month.setdefault(ts.strftime("%Y-%m"), []).append(line)
                    else:
                        keep_lines.append(line)
        except Exception as e:  # noqa: BLE001
            logger.warning("提问留痕清理失败（保留原文件）: %s", e)
            result["errors"] = 1
            return result

        if not archive_by_month:
            result["kept"] = len(keep_lines)
            return result

        archive_dir = path.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived = 0
        for month, lines in archive_by_month.items():
            archive_path = archive_dir / f"records-{month}.jsonl"
            try:
                with archive_path.open("a", encoding="utf-8") as fh:
                    fh.writelines(lines)
                archived += len(lines)
            except Exception as e:  # noqa: BLE001
                logger.warning("提问留痕归档失败 %s: %s（下次再试）", archive_path, e)
                keep_lines.extend(lines)
                result["errors"] += 1

        if archived == 0:
            result["kept"] = len(keep_lines)
            return result

        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                fh.writelines(keep_lines)
            tmp_path.replace(path)
        except Exception as e:  # noqa: BLE001
            logger.warning("提问留痕重写失败（归档已写）: %s", e)
            result["errors"] += 1

        result["kept"] = len(keep_lines)
        result["archived"] = archived
        return result

    def load_recent(self, limit: Optional[int] = None) -> int:
        """启动时从磁盘回填最近若干条记录进内存。"""
        if not self.enabled:
            return 0
        path = self.path
        if not path.is_file():
            return 0
        cap = self.memory_limit if limit is None else max(1, limit)

        try:
            with path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception as e:  # noqa: BLE001
            logger.warning("提问留痕文件不可读，按空处理: %s", e)
            return 0

        for line in lines[-cap * 6 :]:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            payload = event.get("record") if event.get("event") == "snapshot" else None
            if not isinstance(payload, dict):
                continue
            record = _record_from_dict(payload)
            if record is None:
                continue
            with self._lock:
                if record.record_id not in self._records:
                    self._order.append(record.record_id)
                self._records[record.record_id] = record

        while len(self._order) > cap:
            evicted = self._order.popleft()
            self._records.pop(evicted, None)
        return len(self._records)

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._order.clear()
            self._written = 0


def _record_from_dict(data: Dict[str, Any]) -> Optional[QuestionRecord]:
    record_id = str(data.get("record_id", "") or "")
    if not record_id:
        return None
    questions = data.get("questions")
    answers = data.get("answers")
    return QuestionRecord(
        record_id=record_id,
        question_id=str(data.get("question_id", "")),
        thread_id=str(data.get("thread_id", "")),
        call_id=str(data.get("call_id", "")),
        run_id=str(data.get("run_id", "")),
        employee_id=str(data.get("employee_id", "")),
        requestor=str(data.get("requestor", "")),
        questions=[q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else [],
        answers=answers if isinstance(answers, dict) else {},
        status=str(data.get("status", STATUS_PENDING)),
        reason=str(data.get("reason", "")),
        timeout_seconds=int(data.get("timeout_seconds") or 0),
        requested_at=str(data.get("requested_at", "")),
        resolved_at=str(data.get("resolved_at", "")),
        latency_ms=data.get("latency_ms"),
    )


def _elapsed_ms(start_iso: str, end_iso: str) -> Optional[float]:
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return None
    return round((end - start).total_seconds() * 1000, 1)


question_store = QuestionStore()


__all__ = [
    "RECORD_VERSION",
    "STATUS_LABELS",
    "QuestionRecord",
    "QuestionStore",
    "question_store",
]
