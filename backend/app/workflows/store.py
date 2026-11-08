# -*- coding: utf-8 -*-
"""
工作流持久化 —— 定义走 `index.json`，运行记录走每流一个 append-only jsonl。

写法与 `schedules/store.py` 同源（原子整文件索引 + 逐行追加的运行历史）：
- 定义是事实来源本身，坏了就真丢了，所以单独一份索引、原子替换；
- 运行是历史，追加写、崩溃最多丢最后一行，不会让全部记录消失。

★ 进程重启后未结算的运行直接标 orphaned

工作流的执行协程活在进程里，进程没了它就不可能再继续。不标的话，
界面上会有一个永远转圈的运行记录 —— 那比「如实说被中断」糟糕得多。
这条对账与定时任务 `sweep_on_startup` 同一姿态：落盘状态才是真相。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.workflows.models import (
    RUN_ORPHANED,
    RUN_RUNNING,
    RUN_TERMINAL_STATUSES,
    WorkflowRecord,
    WorkflowRun,
    cap_output,
    is_valid_workflow_id,
    make_run_id,
    make_workflow_id,
    new_workflow_record,
    run_from_dict,
    validate_graph,
    workflow_from_dict,
)

logger = logging.getLogger(__name__)

INDEX_VERSION = 1
FILE_VERSION = 1
EVENT_RUN = "run"

#: 输入/输出会原样落盘，给一个上限防止手工塞进巨大的文本
RUN_INPUT_CHARS = 8000


class IllegalWorkflowTransition(RuntimeError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"非法的运行状态迁移: {current} → {target}")
        self.current = current
        self.target = target


def _now_iso() -> str:
    from app.conversations.models import now_iso

    return now_iso()


class WorkflowStore:
    """工作流定义 + 运行记录的持久化。进程级单例 `workflow_store`。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root
        self._resolved: Optional[Path] = None
        self._lock = threading.RLock()
        self._loaded = False
        self._index: Dict[str, WorkflowRecord] = {}
        self._runs: Dict[str, Dict[str, WorkflowRun]] = {}

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "WORKFLOW_ENABLED", True))

    @property
    def root(self) -> Path:
        if self._resolved is None:
            self._resolved = self._root or settings.workflow_dir
        return self._resolved

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _runs_path(self, workflow_id: str) -> Path:
        return self.root / f"{workflow_id}.jsonl"

    # ---------- 加载 ----------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._index = self._read_index()
            self._runs = self._scan_runs()
            self._loaded = True
            self._sweep_running_locked()

    def _read_index(self) -> Dict[str, WorkflowRecord]:
        path = self.index_path
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("workflows") or []
            index: Dict[str, WorkflowRecord] = {}
            for item in raw:
                if isinstance(item, dict):
                    record = workflow_from_dict(item)
                    if record is not None:
                        index[record["workflow_id"]] = record
            return index
        except Exception as e:  # noqa: BLE001
            logger.warning("工作流索引读取失败（按空处理，运行记录仍在）: %s", e)
            return {}

    def _scan_runs(self) -> Dict[str, Dict[str, WorkflowRun]]:
        out: Dict[str, Dict[str, WorkflowRun]] = {}
        try:
            paths = sorted(self.root.glob("*.jsonl"))
        except OSError as e:  # noqa: BLE001
            logger.warning("工作流目录不可读（按空处理）: %s", e)
            return out
        for path in paths:
            wid = path.stem
            if not is_valid_workflow_id(wid):
                continue
            runs: Dict[str, WorkflowRun] = {}
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data, dict):
                            record = run_from_dict(data)
                            if record is not None:
                                runs[record["run_id"]] = record
            except OSError as e:  # noqa: BLE001
                logger.warning("工作流运行文件读取失败，按空处理: %s (%s)", path, e)
            if runs:
                out[wid] = runs
        return out

    def _sweep_running_locked(self) -> None:
        """启动对账：上一个进程留下的 running 一律标 orphaned。"""
        count = 0
        for wid, runs in self._runs.items():
            for run in runs.values():
                if run.get("status") != RUN_RUNNING:
                    continue
                run["status"] = RUN_ORPHANED
                run["finished_at"] = _now_iso()
                run["error"] = "服务重启时这次运行还没结束，已按中断结算"
                self._append_line(
                    self._runs_path(wid),
                    {"v": FILE_VERSION, "event": EVENT_RUN, **dict(run)},
                )
                count += 1
        if count:
            logger.info("工作流启动对账：%s 条未结算运行标为中断", count)
            for wid in list(self._runs):
                self._touch_index_snapshot_locked(wid)

    def _save_index(self) -> None:
        payload = {
            "version": INDEX_VERSION,
            "workflows": [dict(rec) for rec in self._index.values()],
        }
        tmp = self.index_path.with_suffix(".json.tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("工作流索引写入失败（运行记录不受影响）: %s", e)

    def _append_line(self, path: Path, payload: Dict[str, Any]) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("工作流运行记录写入失败: %s", e)
            return False

    # ---------- 定义：读 ----------

    def get(self, workflow_id: str) -> Optional[WorkflowRecord]:
        if not is_valid_workflow_id(workflow_id):
            return None
        with self._lock:
            self._ensure_loaded()
            record = self._index.get(workflow_id)
            return dict(record) if record else None

    def list(self, *, query: str = "") -> List[WorkflowRecord]:
        with self._lock:
            self._ensure_loaded()
            items = list(self._index.values())
        if query:
            needle = query.strip().lower()
            items = [
                r for r in items
                if needle in str(r.get("name") or "").lower()
                or needle in str(r.get("description") or "").lower()
            ]
        items.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        return [dict(r) for r in items]

    # ---------- 定义：写 ----------

    def create(
        self,
        *,
        name: str,
        description: str = "",
        nodes: Any = None,
        edges: Any = None,
    ) -> tuple[Optional[WorkflowRecord], List[str]]:
        name = (name or "").strip()
        if not name:
            return None, ["工作流名称不能为空"]
        if not self.enabled:
            return None, ["工作流功能已关闭"]
        errors = validate_graph(nodes, edges)
        if errors:
            return None, errors
        wid = make_workflow_id()
        record = new_workflow_record(
            workflow_id=wid,
            name=name,
            description=description,
            nodes=nodes,
            edges=edges,
        )
        with self._lock:
            self._ensure_loaded()
            self._index[wid] = record
            self._runs.setdefault(wid, {})
            self._save_index()
        return dict(record), []

    def update(
        self,
        workflow_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        nodes: Any = None,
        edges: Any = None,
    ) -> tuple[Optional[WorkflowRecord], List[str]]:
        if not is_valid_workflow_id(workflow_id):
            return None, ["非法的工作流 id"]
        with self._lock:
            self._ensure_loaded()
            current = self._index.get(workflow_id)
            if current is None:
                return None, ["工作流不存在"]
            merged = dict(current)
            if name is not None:
                clean_name = name.strip()
                if not clean_name:
                    return None, ["工作流名称不能为空"]
                merged["name"] = clean_name
            if description is not None:
                merged["description"] = description.strip()
            if nodes is not None:
                merged["nodes"] = nodes
            if edges is not None:
                merged["edges"] = edges
            errors = validate_graph(merged.get("nodes"), merged.get("edges"))
            if errors:
                return None, errors
            record = workflow_from_dict({**merged, "updated_at": _now_iso()})
            if record is None:
                return None, ["更新后的记录不合法"]
            self._index[workflow_id] = record
            self._save_index()
            return dict(record), []

    def delete(self, workflow_id: str) -> bool:
        if not is_valid_workflow_id(workflow_id):
            return False
        with self._lock:
            self._ensure_loaded()
            if self._index.pop(workflow_id, None) is None:
                return False
            self._runs.pop(workflow_id, None)
            try:
                self._runs_path(workflow_id).unlink(missing_ok=True)
            except OSError as e:  # noqa: BLE001
                logger.warning("工作流运行文件删除失败（定义已移除）: %s %s", workflow_id, e)
            self._save_index()
            return True

    # ---------- 运行 ----------

    def start_run(self, *, workflow_id: str, input_text: str, trigger: str) -> Optional[WorkflowRun]:
        if not is_valid_workflow_id(workflow_id):
            return None
        now = _now_iso()
        run = WorkflowRun(
            run_id=make_run_id(),
            workflow_id=workflow_id,
            status=RUN_RUNNING,
            trigger=trigger,
            input=cap_output(input_text, RUN_INPUT_CHARS),
            started_at=now,
            steps=[],
        )
        with self._lock:
            self._ensure_loaded()
            if workflow_id not in self._index:
                return None
            if not self._append_line(
                self._runs_path(workflow_id), {"v": FILE_VERSION, "event": EVENT_RUN, **dict(run)}
            ):
                return None
            self._runs.setdefault(workflow_id, {})[run["run_id"]] = run
            self._prune_runs_locked(workflow_id)
            self._touch_index_snapshot_locked(workflow_id, started_at=now)
        return dict(run)

    def patch_run(self, workflow_id: str, run_id: str, **fields: Any) -> Optional[WorkflowRun]:
        with self._lock:
            self._ensure_loaded()
            runs = self._runs.get(workflow_id) or {}
            current = runs.get(run_id)
            if current is None:
                return None
            target = str(fields.get("status") or current.get("status"))
            if target != current.get("status"):
                if current.get("status") != RUN_RUNNING or target not in RUN_TERMINAL_STATUSES:
                    raise IllegalWorkflowTransition(str(current.get("status")), target)
            merged = {**dict(current), **{k: v for k, v in fields.items() if v is not None}}
            if "status" in fields and fields["status"] in RUN_TERMINAL_STATUSES and "finished_at" not in fields:
                merged["finished_at"] = _now_iso()
            record = run_from_dict(merged)
            if record is None:
                return None
            self._append_line(
                self._runs_path(workflow_id),
                {"v": FILE_VERSION, "event": EVENT_RUN, **dict(record)},
            )
            runs[run_id] = record
            self._touch_index_snapshot_locked(workflow_id)
            return dict(record)

    def get_run(self, workflow_id: str, run_id: str) -> Optional[WorkflowRun]:
        with self._lock:
            self._ensure_loaded()
            run = (self._runs.get(workflow_id) or {}).get(run_id)
            return dict(run) if run else None

    def runs_of(self, workflow_id: str, *, limit: int = 30) -> List[WorkflowRun]:
        with self._lock:
            self._ensure_loaded()
            runs = list((self._runs.get(workflow_id) or {}).values())
        runs.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return [dict(r) for r in runs[:limit]]

    def active_run(self, workflow_id: str) -> Optional[WorkflowRun]:
        with self._lock:
            self._ensure_loaded()
            for run in (self._runs.get(workflow_id) or {}).values():
                if run.get("status") == RUN_RUNNING:
                    return dict(run)
            return None

    def _touch_index_snapshot_locked(
        self, workflow_id: str, *, started_at: str = ""
    ) -> None:
        """把最近一次运行的时间/状态写回定义索引（列表页免读运行文件）。"""
        current = self._index.get(workflow_id)
        if current is None:
            return
        runs = self._runs.get(workflow_id) or {}
        latest = max(runs.values(), key=lambda r: str(r.get("started_at") or ""), default=None)
        if latest is None:
            return
        updated = dict(current)
        updated["last_run_at"] = started_at or str(latest.get("started_at") or "")
        updated["last_status"] = str(latest.get("status") or "")
        record = workflow_from_dict(updated)
        if record is not None:
            self._index[workflow_id] = record
            self._save_index()

    def _prune_runs_locked(self, workflow_id: str) -> None:
        limit = max(10, int(getattr(settings, "WORKFLOW_MAX_RUNS_PER_WORKFLOW", 100)))
        runs = self._runs.get(workflow_id) or {}
        if len(runs) <= limit:
            return
        terminal = sorted(
            (r for r in runs.values() if r.get("status") in RUN_TERMINAL_STATUSES),
            key=lambda r: str(r.get("started_at") or ""),
        )
        for victim in terminal[: len(runs) - limit]:
            runs.pop(victim["run_id"], None)


#: 进程级单例
workflow_store = WorkflowStore()


__all__ = ["WorkflowStore", "workflow_store"]
