# -*- coding: utf-8 -*-
"""
定时任务的持久化 —— 定义走 `index.json`，运行记录走每任务一个 append-only jsonl。

★ 两套数据、两种写法的分工（与 `tasks/store.py` 同一套论证，不重复）

- **定义**（可编辑配置）：`index.json` 原子整文件写。它是事实来源本身
  （运行记录都能从 jsonl 重建，定义丢了就真丢了），所以坏了要能从
  `<id>.jsonl` 里的 `schedule` 事件折叠重建 —— 索引永远是缓存。
- **运行**（append-only 历史）：`<schedule_id>.jsonl` 逐行追加。
  崩了最多丢最后一行，丢一行运行的后果是「那次跑完没记上」，
  而不是「所有运行都不知去哪了」。

★ 所有写方法同步、失败只告警不抛（不变量 44）

调用方是调度循环和 HTTP 接口两层，都没有「抛了有人接」的语境。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings
from app.schedules.models import (
    KIND_CHAT,
    KIND_WORKFLOW,
    RUN_RUNNING,
    RUN_TERMINAL_STATUSES,
    SCHEDULE_KINDS,
    ScheduleRecord,
    IllegalRunTransition,
    is_valid_schedule_id,
    new_schedule_record,
    record_from_dict,
    run_from_dict,
    validate_freq,
    RunRecord,
)

logger = logging.getLogger(__name__)

INDEX_VERSION = 1
FILE_VERSION = 1

#: jsonl 里的行类型。`run_patch` 与 `schedule_patch` 是「对之前某行的补丁」，
#: 折叠时按 run_id / 整条覆盖 —— 与 tasks 的 `task` / `progress` 同构。
EVENT_SCHEDULE = "schedule"
EVENT_SCHEDULE_PATCH = "schedule_patch"
EVENT_RUN = "run"
EVENT_RUN_PATCH = "run_patch"


def _now_iso() -> str:
    from app.conversations.models import now_iso

    return now_iso()


class ScheduleStore:
    """定时任务定义 + 运行记录的持久化。进程级单例 `schedule_store`。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root
        self._resolved: Optional[Path] = None
        self._lock = threading.RLock()
        self._loaded = False
        #: schedule_id → 定义
        self._index: Dict[str, ScheduleRecord] = {}
        #: schedule_id → 运行记录（run_id → 折叠后的最新态），按启动顺序保留最近 N 条
        self._runs: Dict[str, Dict[str, RunRecord]] = {}

    # ---------- 路径与开关 ----------

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "SCHEDULED_ENABLED", True))

    @property
    def root(self) -> Path:
        if self._resolved is None:
            self._resolved = self._root or settings.schedule_dir
        return self._resolved

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _file_path(self, schedule_id: str) -> Path:
        return self.root / f"{schedule_id}.jsonl"

    # ---------- 加载与重建 ----------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            loaded = self._read_index()
            rebuilt = self._scan_files()
            if loaded is None:
                self._index = {sid: rec for sid, (rec, _) in rebuilt.items() if rec}
                self._runs = {sid: runs for sid, (_, runs) in rebuilt.items()}
            else:
                self._index = loaded
                # 运行记录**永远**从文件折叠 —— index 不存运行（避免双份真相）
                self._runs = {sid: runs for sid, (_, runs) in rebuilt.items()}
            self._loaded = True

    def _read_index(self) -> Optional[Dict[str, ScheduleRecord]]:
        """读定义索引；不存在/损坏返回 None（走重建）。空字典 ≠ None。"""
        path = self.index_path
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("schedules") or []
            index: Dict[str, ScheduleRecord] = {}
            for item in raw:
                if isinstance(item, dict):
                    rec = record_from_dict(item)
                    if rec is not None:
                        index[rec["schedule_id"]] = rec
            return index
        except Exception as e:  # noqa: BLE001
            logger.warning("定时任务索引读取失败，将从任务文件重建: %s", e)
            return None

    def _scan_files(self) -> Dict[str, tuple[Optional[ScheduleRecord], Dict[str, RunRecord]]]:
        """扫目录逐文件折叠：定义取最后一条 `schedule` 行 + 补丁，运行按 run_id 合并。"""
        out: Dict[str, tuple[Optional[ScheduleRecord], Dict[str, RunRecord]]] = {}
        try:
            paths = sorted(self.root.glob("*.jsonl"))
        except OSError as e:  # noqa: BLE001
            logger.warning("定时任务目录不可读（按空处理）: %s", e)
            return out
        for path in paths:
            sid = path.stem
            if not is_valid_schedule_id(sid):
                continue
            base: Optional[Dict[str, Any]] = None
            patches: List[Dict[str, Any]] = []
            runs: Dict[str, RunRecord] = {}
            for kind, payload in self._iter_file(path):
                if kind == EVENT_SCHEDULE:
                    base = dict(payload)
                    base["schedule_id"] = sid
                    patches.clear()
                elif kind == EVENT_SCHEDULE_PATCH:
                    patches.append(dict(payload))
                elif kind == EVENT_RUN:
                    rec = run_from_dict(payload)
                    if rec is not None:
                        runs[rec["run_id"]] = rec
                elif kind == EVENT_RUN_PATCH:
                    target = runs.get(str(payload.get("run_id") or ""))
                    if target is not None:
                        merged = {**dict(target), **{k: v for k, v in payload.items() if v is not None}}
                        rec = run_from_dict(merged)
                        if rec is not None:
                            runs[target["run_id"]] = rec
            record: Optional[ScheduleRecord] = None
            if base is not None:
                for patch in patches:
                    base = {**base, **{k: v for k, v in patch.items() if k != "schedule_id"}}
                record = record_from_dict(base)
            out[sid] = (record, runs)
        if out:
            logger.info("定时任务已从 %s 个任务文件重建状态", len(out))
        return out

    def _iter_file(self, path: Path) -> Iterable[tuple[str, Dict[str, Any]]]:
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
                    if isinstance(data, dict) and isinstance(data.get("event"), str):
                        yield data["event"], data
        except OSError as e:  # noqa: BLE001
            logger.warning("定时任务文件读取失败，按空处理: %s (%s)", path, e)

    def _append_line(self, path: Path, payload: Dict[str, Any]) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("定时任务事件写入失败: %s", e)
            return False

    def _save_index(self) -> None:
        payload = {
            "version": INDEX_VERSION,
            "schedules": [dict(rec) for rec in self._index.values()],
        }
        tmp = self.index_path.with_suffix(".json.tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("定时任务索引写入失败（任务文件不受影响）: %s", e)

    # ---------- 定义：读 ----------

    def get(self, schedule_id: str) -> Optional[ScheduleRecord]:
        if not is_valid_schedule_id(schedule_id):
            return None
        with self._lock:
            self._ensure_loaded()
            return dict(self._index[schedule_id]) if schedule_id in self._index else None

    def list(
        self,
        *,
        enabled: Optional[bool] = None,
        query: str = "",
    ) -> List[ScheduleRecord]:
        with self._lock:
            self._ensure_loaded()
            items = list(self._index.values())
        if enabled is not None:
            items = [r for r in items if bool(r.get("enabled")) is enabled]
        if query:
            needle = query.strip().lower()
            items = [
                r for r in items
                if needle in str(r.get("title") or "").lower()
                or needle in str(r.get("instruction") or "").lower()
            ]
        items.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return [dict(r) for r in items]

    # ---------- 定义：写 ----------

    def create(self, **fields: Any) -> tuple[Optional[ScheduleRecord], str]:
        """新建定义。返回 (记录, 错误信息)；错误非空时记录为 None。

        ★ 校验在落盘之前：一条 `freq` 非法的定义写进磁盘，
        `record_from_dict` 重建时会把它当坏行丢掉 —— 「创建成功、重启消失」。
        """
        freq = fields.get("freq") or {}
        err = validate_freq(freq)
        if err:
            return None, err
        title = str(fields.get("title") or "").strip()
        instruction = str(fields.get("instruction") or "").strip()
        kind = str(fields.get("kind") or KIND_CHAT)
        workflow_id = str(fields.get("workflow_id") or "").strip()
        if not title:
            return None, "任务名称不能为空"
        if kind not in SCHEDULE_KINDS:
            return None, f"不支持的定时任务类型: {kind}"
        if kind == KIND_WORKFLOW:
            if not workflow_id:
                return None, "请选择要运行的工作流"
        elif not instruction:
            return None, "执行指令不能为空"
        if not self.enabled:
            return None, "定时任务存储不可用"
        sid = str(fields.get("schedule_id") or "") or _new_id()
        if not is_valid_schedule_id(sid):
            return None, "非法的任务 id"
        record = new_schedule_record(
            schedule_id=sid,
            title=title,
            instruction=instruction,
            freq=freq,
            start_date=str(fields.get("start_date") or ""),
            end_date=str(fields.get("end_date") or ""),
            enabled=bool(fields.get("enabled", True)),
            kind=kind,
            workflow_id=workflow_id if kind == KIND_WORKFLOW else "",
            agent=str(fields.get("agent") or ""),
            model=str(fields.get("model") or ""),
            web_enabled=bool(fields.get("web_enabled", True)),
            graph_enabled=bool(fields.get("graph_enabled", True)),
            permission_mode=str(fields.get("permission_mode") or ""),
            workdir_id=str(fields.get("workdir_id") or ""),
        )
        with self._lock:
            self._ensure_loaded()
            if sid in self._index:
                return None, "任务已存在"
            if not self._append_line(
                self._file_path(sid), {"v": FILE_VERSION, "event": EVENT_SCHEDULE, **dict(record)}
            ):
                return None, "定时任务存储不可用"
            self._index[sid] = record
            self._runs.setdefault(sid, {})
            self._save_index()
        return dict(record), ""

    def update(self, schedule_id: str, **fields: Any) -> tuple[Optional[ScheduleRecord], str]:
        """编辑定义。freq / 日期窗 / enabled 任一变化都会**当场重算 next_run_at** ——
        这个收口是 `models.py` 模块注释里那条不变量的唯一执法点。"""
        if not is_valid_schedule_id(schedule_id):
            return None, "非法的任务 id"
        with self._lock:
            self._ensure_loaded()
            current = self._index.get(schedule_id)
            if current is None:
                return None, "任务不存在"
            merged = dict(current)
            for key in ("title", "start_date", "end_date", "agent",
                        "model", "permission_mode", "workdir_id"):
                if key in fields:
                    value = str(fields[key] or "").strip()
                    if key == "title" and not value:
                        return None, "任务名称不能为空"
                    # ★ 清空 = 删键，而不是写空串：`record_from_dict` 对空串是
                    # 「不带这个键」，若这里只更新 merged 里已有的空值，
                    # 旧值会**清不掉** —— 用户删掉了结束日期，任务却仍在旧窗口里。
                    if value:
                        merged[key] = value
                    else:
                        merged.pop(key, None)
            if "instruction" in fields:
                # workflow 任务的 input 允许为空（「不带输入跑一次」是合法用法）
                instruction = str(fields["instruction"] or "").strip()
                if instruction:
                    merged["instruction"] = instruction
                else:
                    merged.pop("instruction", None)
            if "kind" in fields:
                kind = str(fields["kind"] or KIND_CHAT)
                if kind not in SCHEDULE_KINDS:
                    return None, f"不支持的定时任务类型: {kind}"
                merged["kind"] = kind
            if "workflow_id" in fields:
                workflow_id = str(fields["workflow_id"] or "").strip()
                if workflow_id:
                    merged["workflow_id"] = workflow_id
                else:
                    merged.pop("workflow_id", None)
            effective_kind = str(merged.get("kind") or KIND_CHAT)
            if effective_kind == KIND_WORKFLOW:
                if not str(merged.get("workflow_id") or "").strip():
                    return None, "请选择要运行的工作流"
            else:
                merged.pop("workflow_id", None)
                if not str(merged.get("instruction") or "").strip():
                    return None, "执行指令不能为空"
            for key in ("web_enabled", "graph_enabled", "enabled"):
                if key in fields:
                    merged[key] = bool(fields[key])
            if "freq" in fields:
                freq = fields["freq"] or {}
                err = validate_freq(freq)
                if err:
                    return None, err
                merged["freq"] = dict(freq)
            schedule_changed = (
                fields.get("freq") is not None
                or "start_date" in fields
                or "end_date" in fields
                or "enabled" in fields
            )
            if schedule_changed:
                from app.schedules.models import compute_next_run

                # ★ 停用 = 没有下一次，与 `new_schedule_record` 对 enabled=False
                # 的处理对齐。调度循环本来就只看 enabled 的任务，但界面上的
                # 「下次执行时间」读的是这个字段 —— 留着它，暂停的任务会顶着
                # 一个"看起来马上要跑"的时间，用户分不清它到底停没停。
                nxt = (
                    compute_next_run(
                        merged.get("freq") or {},
                        start_date=str(merged.get("start_date") or ""),
                        end_date=str(merged.get("end_date") or ""),
                    )
                    if merged.get("enabled")
                    else ""
                )
                if nxt:
                    merged["next_run_at"] = nxt
                else:
                    merged.pop("next_run_at", None)  # 空 = 不再执行，同样要**清掉旧值**
            merged["updated_at"] = _now_iso()
            record = record_from_dict(merged)
            if record is None:
                return None, "更新后的记录不合法"
            if not self._append_line(
                self._file_path(schedule_id),
                {"v": FILE_VERSION, "event": EVENT_SCHEDULE, **dict(record)},
            ):
                return None, "定时任务存储不可用"
            self._index[schedule_id] = record
            self._save_index()
            return dict(record), ""

    def set_enabled(self, schedule_id: str, enabled: bool) -> tuple[Optional[ScheduleRecord], str]:
        return self.update(schedule_id, enabled=bool(enabled))

    def delete(self, schedule_id: str) -> bool:
        """删除定义 + 运行文件。一次 unlink —— 这是「按条分文件」买来的东西。"""
        if not is_valid_schedule_id(schedule_id):
            return False
        with self._lock:
            self._ensure_loaded()
            if self._index.pop(schedule_id, None) is None:
                return False
            self._runs.pop(schedule_id, None)
            try:
                self._file_path(schedule_id).unlink(missing_ok=True)
            except OSError as e:  # noqa: BLE001
                logger.warning("定时任务文件删除失败（定义已从索引移除）: %s %s", schedule_id, e)
            self._save_index()
            return True

    def advance_next_run(self, schedule_id: str, next_run_at: str) -> bool:
        """派发时推进下一次时间。`next_run_at` 空串 = 窗口内不再执行
        （与「键不存在 = 还没算过」是两种语义，所以这里允许写空）。"""
        with self._lock:
            self._ensure_loaded()
            current = self._index.get(schedule_id)
            if current is None:
                return False
            updated = dict(current)
            if next_run_at:
                updated["next_run_at"] = next_run_at
            else:
                updated.pop("next_run_at", None)
            updated["updated_at"] = _now_iso()
            record = record_from_dict(updated)
            if record is None:
                return False
            self._index[schedule_id] = record
            self._append_line(
                self._file_path(schedule_id),
                {"v": FILE_VERSION, "event": EVENT_SCHEDULE_PATCH, "schedule_id": schedule_id,
                 "next_run_at": next_run_at, "updated_at": updated["updated_at"]},
            )
            self._save_index()
            return True

    def touch_on_dispatch(self, schedule_id: str, *, run: RunRecord) -> None:
        """派发成功后刷新定义上的运行快照（列表页免读运行文件）。"""
        fields = {
            "last_run_at": str(run.get("started_at") or ""),
            "last_status": str(run.get("status") or RUN_RUNNING),
            "last_thread_id": str(run.get("thread_id") or ""),
            "last_task_id": str(run.get("task_id") or ""),
        }
        with self._lock:
            self._ensure_loaded()
            current = self._index.get(schedule_id)
            if current is None:
                return
            updated = dict(current)
            updated.update({k: v for k, v in fields.items() if v})
            record = record_from_dict(updated)
            if record is None:
                return
            self._index[schedule_id] = record
            self._append_line(
                self._file_path(schedule_id),
                {"v": FILE_VERSION, "event": EVENT_SCHEDULE_PATCH, "schedule_id": schedule_id, **fields},
            )
            self._save_index()

    # ---------- 运行记录 ----------

    def start_run(
        self,
        *,
        schedule_id: str,
        thread_id: str = "",
        task_id: str = "",
        workflow_run_id: str = "",
        trigger: str,
    ) -> Optional[RunRecord]:
        from app.schedules.models import make_run_id

        now = _now_iso()
        record: RunRecord = RunRecord(
            run_id=make_run_id(),
            schedule_id=schedule_id,
            trigger=trigger,
            status=RUN_RUNNING,
            started_at=now,
        )
        if thread_id:
            record["thread_id"] = thread_id
        if task_id:
            record["task_id"] = task_id
        if workflow_run_id:
            record["workflow_run_id"] = workflow_run_id
        with self._lock:
            self._ensure_loaded()
            if not self._append_line(
                self._file_path(schedule_id), {"v": FILE_VERSION, "event": EVENT_RUN, **dict(record)}
            ):
                return None
            self._runs.setdefault(schedule_id, {})[record["run_id"]] = record
            self._prune_runs_locked(schedule_id)
        return dict(record)

    def patch_run(self, schedule_id: str, run_id: str, **fields: Any) -> Optional[RunRecord]:
        """结算一次运行。非法迁移抛 `IllegalRunTransition`（与子任务同一执法方式）。"""
        with self._lock:
            self._ensure_loaded()
            runs = self._runs.get(schedule_id) or {}
            current = runs.get(run_id)
            if current is None:
                return None
            target = str(fields.get("status") or current.get("status"))
            if target != current.get("status"):
                allowed = RUN_TERMINAL_STATUSES if current.get("status") == RUN_RUNNING else frozenset()
                if target not in allowed:
                    raise IllegalRunTransition(str(current.get("status")), target)
            merged = {**dict(current), **{k: v for k, v in fields.items() if v is not None}}
            if "status" in fields and fields["status"] in RUN_TERMINAL_STATUSES and "finished_at" not in fields:
                merged["finished_at"] = _now_iso()
            record = run_from_dict(merged)
            if record is None:
                return None
            self._append_line(
                self._file_path(schedule_id),
                {"v": FILE_VERSION, "event": EVENT_RUN_PATCH, "run_id": run_id, **fields,
                 "finished_at": merged.get("finished_at")},
            )
            runs[run_id] = record
            return dict(record)

    def get_run(self, schedule_id: str, run_id: str) -> Optional[RunRecord]:
        with self._lock:
            self._ensure_loaded()
            rec = (self._runs.get(schedule_id) or {}).get(run_id)
            return dict(rec) if rec else None

    def runs_of(self, schedule_id: str, *, limit: int = 50) -> List[RunRecord]:
        with self._lock:
            self._ensure_loaded()
            runs = list((self._runs.get(schedule_id) or {}).values())
        runs.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return [dict(r) for r in runs[:limit]]

    def all_runs(self, *, limit: int = 100) -> List[tuple[ScheduleRecord, RunRecord]]:
        """跨任务的全量运行（「运行记录」页签）。返回 (定义, 运行) 对 ——
        定义跟着运行走，列表页不用二次查名字。"""
        with self._lock:
            self._ensure_loaded()
            pairs: List[tuple[ScheduleRecord, RunRecord]] = []
            for sid, runs in self._runs.items():
                schedule = self._index.get(sid)
                if schedule is None:
                    continue
                for run in runs.values():
                    pairs.append((dict(schedule), dict(run)))
        pairs.sort(key=lambda p: str(p[1].get("started_at") or ""), reverse=True)
        return pairs[:limit]

    def active_run(self, schedule_id: str) -> Optional[RunRecord]:
        """该任务当前有没有一次还没结算的运行 —— 调度器的防重入门闸。

        ★ 为什么看落盘状态而不是内存标记：uvicorn --reload 会留下
        「旧进程协程没了、记录还是 running」的窗口（`sweep_orphans` 结算之前，
        新进程的循环如果靠内存标记判断，会对同一定时任务重复派发）。
        """
        with self._lock:
            self._ensure_loaded()
            for run in (self._runs.get(schedule_id) or {}).values():
                if run.get("status") == RUN_RUNNING:
                    return dict(run)
            return None

    def active_sweep_candidates(self) -> List[tuple[str, RunRecord]]:
        """所有未结算的运行（启动对账用）。"""
        with self._lock:
            self._ensure_loaded()
            out: List[tuple[str, RunRecord]] = []
            for sid, runs in self._runs.items():
                for run in runs.values():
                    if run.get("status") == RUN_RUNNING:
                        out.append((sid, dict(run)))
            return out

    def _prune_runs_locked(self, schedule_id: str) -> None:
        """只保留最近 N 条**已结算**的运行；未结算的一条不删
        （与 tasks 的「只淘汰终态」同一铁律 —— 删掉在跑的运行等于毁掉对账凭据）。"""
        limit = max(20, int(getattr(settings, "SCHEDULE_MAX_RUNS_PER_SCHEDULE", 200)))
        runs = self._runs.get(schedule_id) or {}
        if len(runs) <= limit:
            return
        terminal = sorted(
            (r for r in runs.values() if r.get("status") in RUN_TERMINAL_STATUSES),
            key=lambda r: str(r.get("started_at") or ""),
        )
        for victim in terminal[: len(runs) - limit]:
            runs.pop(victim["run_id"], None)


def _new_id() -> str:
    from app.schedules.models import make_schedule_id

    return make_schedule_id()


#: 进程级单例
schedule_store = ScheduleStore()
