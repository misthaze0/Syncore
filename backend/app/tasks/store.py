"""
后台子任务的持久化 —— 索引 + 每任务一个 append-only 事件文件。

★ 为什么是文件而不是 Redis（不重复 `conversations/store.py` 的论证，只补一句新的）

会话历史不能放 Redis 是因为它「重启最不该丢」。
子任务状态更不能 —— 丢了不会让子 Agent 停跑（它照跑、结果照落盘），
但用户和主 Agent 都**查不到**它，那批任务就成了界面之外的幽灵。
所以这里走完全相同的形状：文件是事实来源，`index.json` 是可再生的缓存。

★ 为什么每个任务一个文件（而不是一个总 jsonl）

同 `conversations/store.py`：任务有「按会话批量删」和「按 id 精确读」两种需求。
按会话分文件后前者是扫目录删一批，后者是读一个文件；
一个总 jsonl 则每次删都要重写全量，重写窗口里进程被杀就留下一堆残缺状态 ——
而状态残缺的直接后果是「任务永远停在那里」，比历史丢失严重得多。

★ 写入的两种取舍（与 ConversationStore 刻意相反，也各自相反才对）

- **索引**（`index.json`）：原子整文件写（tmp + `os.replace`）。
  它是派生数据，要的是「要么旧的完整版、要么新的完整版」。
- **事件流**（`<task_id>.jsonl`）：append-only。
  崩了最多丢最后一行，而丢一行进度是可以接受的（下次查询会补上）。

★ 所有的写方法都**同步**、失败只告警不抛

与不变量 44 同源，但这里还有一个额外理由：
子图的写入发生在后台任务里，调用点没有生成器的 `finally` 语境，
写成 async 就要求每个调用点都 `await` —— 一旦有人忘了，事件就静默丢了。
同步写几 KB 的代价可以接受，换来的是「任何上下文都能安全调用」。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings
from app.tasks.models import (
    ACTIVE_STATUSES,
    EVENT_PROGRESS,
    EVENT_TASK,
    INDEX_VERSION,
    PROGRESS_NARRATIVE,
    PROGRESS_QUESTION,
    PROGRESS_TEXT_CHARS,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_WAITING_APPROVAL,
    STATUS_WAITING_INPUT,
    TASK_FILE_VERSION,
    IllegalTransition,
    ProgressEntry,
    TaskProgressView,
    TaskRecord,
    can_transition,
    is_active,
    is_terminal,
    is_valid_task_id,
    new_task_record,
    record_from_dict,
    truncate_label,
)

logger = logging.getLogger(__name__)

#: 列表接口单次返回的进度条数上限。
#: 一个 6 步任务 × 4 子任务就是 24 条，用户不会逐条看完。
#: ★ 超限时**必须**在响应里说 `total_steps`，不能让前端以为就这么长
#: ——「悄悄截断」与「填 0」是同一类谎报。
PROGRESS_LIMIT = 50


class TaskStore:
    """后台子任务的持久化（索引 + 每任务一个 append-only 事件文件）"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root
        self._resolved: Optional[Path] = None
        self._lock = threading.RLock()
        self._loaded = False
        #: task_id → TaskRecord
        self._index: Dict[str, TaskRecord] = {}
        #: task_id → 已写出的事件条数（seq 的来源）。
        #: ⚠️ 存在内存里而不是每次数文件行：进度是高频写入，
        #: 每次追加都数一遍行数会让 jsonl 变成 O(n²)。
        #: 代价是进程重启后 seq 从 0 重新开始 —— 而 `seq` 的用途是
        #: 「同一次会话内去重与排序」，重启后前端会整体重拉，所以够用。
        self._seq: Dict[str, int] = {}

    # ---------- 路径与开关 ----------

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "SUBTASK_ENABLED", True))

    @property
    def root(self) -> Path:
        if self._resolved is None:
            self._resolved = self._root or settings.subtask_dir
        return self._resolved

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _task_path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.jsonl"

    @property
    def max_items(self) -> int:
        return max(0, int(getattr(settings, "SUBTASK_MAX_ITEMS", 200)))

    # ---------- 索引 ----------

    def _ensure_loaded(self) -> None:
        """惰性加载索引；缺失或损坏时从任务文件重建"""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._index = self._read_index() or self._rebuild_index()
            self._loaded = True

    def _read_index(self) -> Optional[Dict[str, TaskRecord]]:
        """读索引；文件不存在/损坏时返回 None（由调用方决定是否重建）

        返回空字典与 None 必须区分：前者是「确实没有任务」，
        后者是「读不出来」—— 后者要走重建。
        """
        path = self.index_path
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("tasks") or []
            index: Dict[str, TaskRecord] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                record = record_from_dict(item)
                if record is not None:
                    index[record["task_id"]] = record
            return index
        except Exception as e:  # noqa: BLE001
            logger.warning("任务索引读取失败，将从任务文件重建: %s", e)
            return None

    def _rebuild_index(self) -> Dict[str, TaskRecord]:
        """
        扫目录重建索引 —— 逐文件折叠，与 `ConversationStore._rebuild_index` 同一条路。

        事件文件里有两类行：
          - `{"event": "task", ...}`    创建时写的快照（字段最全）
          - `{"event": "progress", ...}`  之后每一次 `patch` 追加的状态
        折叠方式：**后写的 `task` 行覆盖先写的**（任务状态可能在建档时补过字段），
        进度行不参与字段合并（它们只进 progress，不进记录）。
        """
        index: Dict[str, TaskRecord] = {}
        try:
            paths = sorted(self.root.glob("*.jsonl"))
        except OSError as e:  # noqa: BLE001
            logger.warning("任务目录不可读（按空处理）: %s", e)
            return index

        for path in paths:
            task_id = path.stem
            if not is_valid_task_id(task_id):
                continue
            record: Optional[TaskRecord] = None
            for kind, payload in self._iter_file(path):
                if kind == EVENT_TASK and isinstance(payload, dict):
                    record = record_from_dict({**payload, "task_id": task_id})
            if record is not None:
                record.setdefault("task_id", task_id)
                index[task_id] = record
        if index:
            logger.info("任务索引已从 %s 个任务文件重建", len(index))
        return index

    def _save_index(self) -> None:
        """原子整文件写索引（临时文件 + `os.replace`）

        写失败只告警：文件才是事实来源，索引坏了只是下次读慢一次。
        """
        payload = {
            "version": INDEX_VERSION,
            "tasks": [dict(rec) for rec in self._sorted_records()],
        }
        tmp = self.index_path.with_suffix(".json.tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("任务索引写入失败（任务文件不受影响）: %s", e)

    def _sorted_records(self) -> List[TaskRecord]:
        """按最后活跃时间倒序（最近动的排最前）"""
        return sorted(
            self._index.values(),
            key=lambda r: (r.get("finished_at") or r.get("started_at") or "", r.get("created_at") or ""),
            reverse=True,
        )

    # ---------- 文件读写 ----------

    def _iter_file(self, path: Path) -> Iterable[tuple[str, Dict[str, Any]]]:
        """
        逐行读一个任务文件，坏行跳过。

        与 `_read_turns` 同一个理由：追加写的崩溃安全前提就是
        「崩在最后一行时之前的行都还是好的」。
        ⚠️ 但这里坏行的代价更高 —— 会话历史丢一轮只是少一条记录，
        任务记录丢了会**连带丢进度**，所以这个方法只认 `event` 字段。
        """
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
            logger.warning("任务文件读取失败，按空处理: %s (%s)", path, e)

    def _append_line(self, path: Path, payload: Dict[str, Any]) -> bool:
        """追加一行 JSON。任何 IO 失败只告警不抛（旁路能力）"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("任务事件写入失败（不影响任务执行）: %s", e)
            return False

    # ---------- 写入 ----------

    def create(
        self,
        *,
        thread_id: str,
        agent: str,
        title: str,
        instruction: str,
        parent_turn_id: str = "",
        task_id: str = "",
        web_enabled: bool = True,
        graph_enabled: bool = True,
        model: str = "",
        permission_mode: str = "",
        workdir_root: str = "",
        employee_id: str = "",
        group_id: str = "",
        run_id: str = "",
        board_card_id: str = "",
        source: str = "",
        depends_on: Optional[List[str]] = None,
        requires_file: bool = False,
    ) -> Optional[TaskRecord]:
        """
        登记一个新任务（状态 `pending`）。

        非法 id 一律拒绝（它会变成文件路径，与 `thread_id` 同一类攻击面）。
        `enabled=False` 返回 None —— 调用方**必须**能继续跑任务
        （降级不是失败），只是没有进度可查。
        """
        if not self.enabled:
            return None
        tid = task_id or _new_id()
        if not is_valid_task_id(tid):
            logger.warning("拒绝登记非法 task_id（疑似路径穿越）: %r", tid)
            return None
        if not is_valid_task_id(thread_id):
            logger.warning("拒绝登记非法 thread_id: %r", thread_id)
            return None

        from app.conversations.models import now_iso

        record = new_task_record(
            task_id=tid,
            thread_id=thread_id,
            agent=agent,
            title=title,
            instruction=instruction,
            parent_turn_id=parent_turn_id,
            created_at=now_iso(),
            web_enabled=web_enabled,
            graph_enabled=graph_enabled,
            model=model,
            permission_mode=permission_mode,
            workdir_root=workdir_root,
            employee_id=employee_id,
            group_id=group_id,
            run_id=run_id,
            board_card_id=board_card_id,
            source=source,
            depends_on=depends_on,
            requires_file=requires_file,
        )
        with self._lock:
            self._ensure_loaded()
            path = self._task_path(tid)
            if not path.is_file():
                if not self._append_line(
                    path,
                    {"v": TASK_FILE_VERSION, "event": EVENT_TASK, **dict(record)},
                ):
                    return None
                self._seq[tid] = 0
            self._index[tid] = record
            self._prune_locked()
            self._save_index()
        return record

    def patch(self, task_id: str, **fields: Any) -> Optional[TaskRecord]:
        """
        改任务的状态字段（状态迁移 + 可选字段）。

        ★ 不变量 57：**先落盘，再执行**。调用方在拿到返回值之后
        才去做真正的动作（启动子图、写文件、发邮件）。
        顺序反过来的症状极其隐蔽：进程被杀时任务永远停在 `running`，
        而没有任何东西在等它 —— 用户看到的是一个转不停的 spinner，
        重启后才发现任务早没了。落盘先于副作用，从根上消掉这个窗口。

        非法迁移抛 `IllegalTransition`：状态卡住不动的 bug
        应该在写它的那一行炸出来，而不是在界面上表现为「这任务怎么还在跑」。

        ⚠️ `started_at` / `finished_at` 是**副作用**而不是参数 ——
        它们由状态自己决定（第一次进 running 时打开始时间，
        进终态时打结束时间），这样调用方不会漏传、也不会传错。
        """
        if not is_valid_task_id(task_id):
            logger.warning("拒绝修改非法 task_id: %r", task_id)
            return None

        from app.conversations.models import now_iso

        with self._lock:
            self._ensure_loaded()
            current = self._index.get(task_id)
            if current is None:
                return None

            updated = dict(current)
            target = str(fields.get("status") or current.get("status") or STATUS_PENDING)
            now = now_iso()

            if target != current.get("status"):
                if not can_transition(str(current.get("status")), target):
                    raise IllegalTransition(str(current.get("status")), target)
                # ★ 副作用：时间戳由状态决定，不由调用方传
                if target == STATUS_RUNNING and "started_at" not in updated:
                    updated["started_at"] = now
                if is_terminal(target):
                    updated["finished_at"] = now
            updated["status"] = target

            # ---- 可选字段：空值不写，宁可键不存在 ----
            # 若无其事地写 `error=""`，回执里就会多出一个「错误信息为空」
            # 的假字段 —— 它看起来像「查过了，没有错误」，
            # 而真相是「根本没失败过」。这两件事对用户不是一回事。
            for key, value in fields.items():
                if key == "status":
                    continue
                if value is None:
                    updated.pop(key, None)
                    continue
                if isinstance(value, (str, list, dict)) and not value:
                    updated.pop(key, None)
                    continue
                updated[key] = value

            self._index[task_id] = updated  # type: ignore[assignment]
            # 状态变化追加一行，保证索引与文件都能重建出同一个状态
            self._append_line(
                self._task_path(task_id),
                {"v": TASK_FILE_VERSION, "event": EVENT_TASK, **updated},
            )
            self._save_index()
            return updated  # type: ignore[return-value]

    def append_narrative(self, task_id: str, text: str) -> Optional[ProgressEntry]:
        """
        追加一条「叙述正文」进度（子 Agent 在两次工具调用之间说的那段话）。

        ★ 为什么单独一个入口，而不是让调用方自己造 `token` 事件
        `token` 是**逐字**增量（一个回答几千条），而进度条目是「一步」。
        把逐字增量当事件灌进 `append_event`，要么每条落一行
        （jsonl 立刻爆掉、前端被几千条一模一样的事件刷屏），
        要么在 `_entry_from_event` 里做状态累积 —— 那会让折叠函数
        从「纯函数」变成「有记忆的函数」，两条通道（实时 / 轮询）
        就再也无法保证给出同一份事实了。

        所以累积放在**调用方**（`executor.emit`）：它本来就是有状态的，
        也天然知道「一段话说完了」这个边界（下一个非 token 事件）。
        这里只负责把**已经攒好的一段**落成一条。

        空正文返回 None —— 「这一步没有任何正文」不该变成一条空条目
        （与 `done` 里没有 answer 时不记那一步是同一个理由：
        界面上多一条空步骤比少一条更让人困惑）。
        """
        if not is_valid_task_id(task_id):
            return None
        clean = _clean_narrative(text)
        if not clean:
            return None

        entry: ProgressEntry = ProgressEntry(
            kind=PROGRESS_NARRATIVE,
            # label 取正文首句（收起的列表里显示的就是它）
            label=truncate_label(clean),
            status="ok",
            text=clean,
        )
        with self._lock:
            self._ensure_loaded()
            seq = self._seq.get(task_id, 0) + 1
            self._seq[task_id] = seq

            from app.conversations.models import now_iso

            entry["seq"] = seq
            entry["at"] = now_iso()
            ok = self._append_line(
                self._task_path(task_id),
                {"v": TASK_FILE_VERSION, "event": EVENT_PROGRESS, "task_id": task_id, **entry},
            )
        return entry if ok else None

    def append_event(self, task_id: str, event: Dict[str, Any]) -> Optional[ProgressEntry]:
        """
        追加一条进度。

        ★ 事件 → 进度的折叠只有这一处（`_entry_from_event`），
        所以「实时流看到的」与「轮询查到的」必然是同一套判断。
        """
        if not is_valid_task_id(task_id):
            return None
        entry = _entry_from_event(event)
        if entry is None:
            return None

        with self._lock:
            self._ensure_loaded()
            seq = self._seq.get(task_id, 0) + 1
            self._seq[task_id] = seq

            from app.conversations.models import now_iso

            entry["seq"] = seq
            entry["at"] = now_iso()
            ok = self._append_line(
                self._task_path(task_id),
                {"v": TASK_FILE_VERSION, "event": EVENT_PROGRESS, "task_id": task_id, **entry},
            )
        return entry if ok else None

    # ---------- 读取 ----------

    def get(self, task_id: str) -> Optional[TaskRecord]:
        """取单个任务；id 非法与不存在都返回 None（不变量 47 要求两者不可区分）"""
        if not is_valid_task_id(task_id):
            return None
        with self._lock:
            self._ensure_loaded()
            record = self._index.get(task_id)
            return dict(record) if record is not None else None

    def progress(self, task_id: str, *, limit: int = PROGRESS_LIMIT) -> TaskProgressView:
        """
        取一个任务的进度快照。

        ★ 超限时会带 `total_steps` —— 界面上据此说明「只显示了最近 N 步」。
        悄悄截断与填 0 是同一类谎报（`reference/api/sse.md` 对 done.files 的同一约定）。
        """
        view: TaskProgressView = TaskProgressView(
            task_id=task_id, status="", entries=[]
        )
        if not is_valid_task_id(task_id):
            return view
        with self._lock:
            self._ensure_loaded()
            record = self._index.get(task_id)
            view["status"] = str(record.get("status")) if record else ""
            entries = self._read_entries(self._task_path(task_id))
            total = len(entries)
            shown = entries[-limit:] if limit > 0 else entries
            if total > len(shown):
                view["total_steps"] = total
            # 重新编号：截断后 seq 从 1 连续，前端不需要知道它被切过
            view["entries"] = [{**e, "seq": i + 1} for i, e in enumerate(shown)]
        return view

    def _read_entries(self, path: Path) -> List[ProgressEntry]:
        """读一个任务的全部进度条目（按落盘顺序）"""
        entries: List[ProgressEntry] = []
        for kind, payload in self._iter_file(path):
            if kind != EVENT_PROGRESS:
                continue
            entry = _entry_from_stored(payload)
            if entry is not None:
                entries.append(entry)
        return entries

    def read_progress_lines(self, task_id: str) -> List[Dict[str, Any]]:
        """
        读一个任务的**全部进度行**（落盘形状：折叠后的条目 + seq/at）。

        ★ 与 `progress()` 的分工
        `progress()` 给界面看的是重新编号、截断后的「最近 N 步」；
        恢复评估要判断「最后一步到底执行到哪」，需要**完整、有序、带 call_id**
        的那一份（`tool_result` 的 call_id 只在落盘行里有，接口层不需要它）。
        注意落盘的是**折叠后**的条目（`kind` / `status` / `call_id`），
        原始事件的 `type` 不落盘 —— 所以这里只能按 `kind` 判断事件类别。
        """
        if not is_valid_task_id(task_id):
            return []
        with self._lock:
            self._ensure_loaded()
            return [
                payload
                for kind, payload in self._iter_file(self._task_path(task_id))
                if kind == EVENT_PROGRESS
            ]

    def list(
        self,
        *,
        thread_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
        sources: Optional[Iterable[str]] = None,
        employee_id: str = "",
    ) -> tuple[List[TaskRecord], int]:
        """
        列出任务（按最后活跃时间倒序）。

        返回 `(本页, 总数)` —— 前端要能算「第几页 / 共几页」，
        只给本页的话分页控件就只能猜。

        Args:
            sources: 只返回这些 `source` 的任务；None = 不按来源过滤。
                ★ 「后台任务」这一屏传的是 `("", supervisor)`：
                空串是**改造前落盘的旧记录**（那时还没有 source 这一维），
                它们的真实身份就是主对话的子智能体，把它们过滤掉会让老任务
                在界面上凭空消失 —— 那是一种新式静默失效。
                工作空间派发的（`board` / `employee_private`）不属于这一屏，
                口径见施工图 §4.3。
            employee_id: 只返回这名员工干的活。★ 私聊工作台的回填按它查、**不按 thread_id** ——
                他为团队跑的任务落在**群组的线程**上（`thread_id == group_id`），
                按线程查会查出「他什么都没干过」。
        """
        with self._lock:
            self._ensure_loaded()
            records = self._sorted_records()
            if thread_id:
                records = [r for r in records if r.get("thread_id") == thread_id]
            if employee_id:
                records = [r for r in records if str(r.get("employee_id") or "") == employee_id]
            if status:
                wanted = {s.strip() for s in status.split(",") if s.strip()}
                records = [r for r in records if r.get("status") in wanted]
            if sources is not None:
                allowed = set(sources)
                records = [r for r in records if str(r.get("source") or "") in allowed]
            total = len(records)
            page = records[max(0, offset) : max(0, offset) + max(0, limit)]
            return [dict(r) for r in page], total

    def active(self, thread_id: str = "", *, limit: int = 5) -> List[TaskRecord]:
        """
        取最近若干个**未结束**的任务（prompt 注入与界面概览用）。

        ⚠️ 不走 `list()` 再过滤：那个入口要先按时间排序全表，
        而这里要的「未结束的任务」通常只有个位数。
        """
        with self._lock:
            self._ensure_loaded()
            records = [r for r in self._sorted_records() if is_active(str(r.get("status")))]
            if thread_id:
                records = [r for r in records if r.get("thread_id") == thread_id]
            return [dict(r) for r in records[: max(0, limit)]]

    def stats(self) -> Dict[str, Any]:
        """
        按状态计数（运维用，不变量 23：状态查询接口必须自己解析惰性单例）。

        ★ 顺带暴露审批闸门的持有者与等待队列 ——
        闸门死锁是这类系统唯一「不报错但功能全停」的故障
        （见 ADR 0019 的风险一节），而它在界面上完全看不出来。
        元数据说错比没有元数据更糟，所以这里必须能查到。
        """
        with self._lock:
            self._ensure_loaded()
            counts = {s: 0 for s in ACTIVE_STATUSES}
            counts.update(
                {s: 0 for s in ("interrupted", "succeeded", "failed", "cancelled", "orphaned")}
            )
            for record in self._index.values():
                st = str(record.get("status") or "")
                if st in counts:
                    counts[st] += 1
            return {
                "enabled": self.enabled,
                "total": len(self._index),
                "by_status": counts,
                "gate": approval_gate_snapshot(),
            }

    # ---------- 状态操作 ----------

    def cancel(self, task_id: str) -> bool:
        """
        取消一个**未结束**的任务。

        终态返回 False 而不是 404：任务确实存在，只是已经结束了。
        把它报成「不存在」会让界面显示一个误导性的错误。
        """
        if not is_valid_task_id(task_id):
            return False
        with self._lock:
            self._ensure_loaded()
            record = self._index.get(task_id)
            if record is None or is_terminal(str(record.get("status"))):
                return False
            try:
                self.patch(task_id, status=STATUS_CANCELLED)
            except IllegalTransition:
                return False
        return True

    def sweep_orphans(self) -> Dict[str, Any]:
        """
        启动时结算上次进程留下的非终态任务。

        ★ `pending` 与 `running` / `waiting_approval` 必须区别对待

        - `pending`：还没拿到执行槽位、**没有任何副作用**，重新派发是安全的。
        - `running` / `waiting_approval`：**可能已经产生了副作用**
          （工具已执行、文件已写），不能自动 resume —— 那会重放它们。

        但「不能自动续」不等于「只能判死」。这里统一落成 `interrupted`（非终态），
        再由 `app/tasks/recovery.py` 逐个对照 checkpointer 与事件流给出三种结论：

            checkpoint → 可以直接接着跑
            approval   → 停在等确认，续跑会重新问一次
            reconcile  → 最后一步可能已有副作用，续跑前先核对
            没有状态   → 才落 `orphaned`（真的不可续）

        ★ 为什么自动续仍然禁止
        不变量 6 的重放安全论证是「一次 superstep 一个工具调用」，它保证的是
        **同一 superstep 内**不重复，**不保证**跨进程恢复时不会重跑已完成的 superstep。
        而 Checkpointer 在 Redis 挂掉降级成 InMemorySaver 时状态必然已清空，
        此时 resume 等于从头重跑 —— `delete` 会删第二遍。
        所以「续不续」必须由人点，且点之前要能看到最后一步是什么。

        Returns:
            `{"redispatched": [task_id, ...], "interrupted": [task_id, ...]}`
            `redispatched` 是**待重新派发**的 pending 任务 id 列表 ——
            store 层不能 create_task（那是 executor 的职责），
            所以这里只把它们挑出来，由调用方（main.py）经 executor 重新派发。
            `interrupted` 是待评估可续性的任务 id，由调用方交给 recovery.py。
        """
        counts: Dict[str, Any] = {"redispatched": [], "interrupted": []}
        if not self.enabled:
            return counts
        with self._lock:
            self._ensure_loaded()
            for task_id, record in list(self._index.items()):
                status = str(record.get("status") or "")
                if is_terminal(status):
                    continue
                if status == STATUS_PENDING:
                    # pending 无副作用，重新派发是安全的（见 ADR 0019 / 不变量 57）
                    counts["redispatched"].append(task_id)
                elif status != STATUS_INTERRUPTED:
                    try:
                        self.patch(
                            task_id,
                            status=STATUS_INTERRUPTED,
                            resume_note="服务重启时任务中断",
                            # 旧卡片的等待者随进程一起没了；留着会让界面
                            # 挂一张点不动的问卷（续跑会重新生成一张新的）
                            pending_question=None,
                        )
                        counts["interrupted"].append(task_id)
                    except IllegalTransition:
                        # 并发里刚被别人结算掉了 —— 不是错误
                        continue
        if counts["interrupted"] or counts["redispatched"]:
            logger.info(
                "启动时结算后台任务: 待重新派发 %s 个 / 待评估可续性 %s 个",
                len(counts["redispatched"]),
                len(counts["interrupted"]),
            )
        return counts

    def mark_running(self, task_id: str) -> Optional[TaskRecord]:
        """把任务从 `pending` 推进到 `running`（拿到执行槽位时调用）

        顺带清掉可能残留的提问载荷：回答已经被消费，卡片不该在
        「running」的任务上继续挂着（`patch` 对 None 是「删掉这个键」）。
        """
        return self.patch(task_id, status=STATUS_RUNNING, pending_question=None)

    def mark_waiting_approval(self, task_id: str) -> Optional[TaskRecord]:
        """任务挂起等人工审批"""
        return self.patch(task_id, status=STATUS_WAITING_APPROVAL)

    def mark_waiting_input(
        self, task_id: str, question: Dict[str, Any]
    ) -> Optional[TaskRecord]:
        """
        任务挂起等模型提问的回答。

        ★ 提问载荷必须落盘
        用户刷新 / 换设备后要能重新画出同一张卡片，而「哪个 call_id、
        哪些问题、选项是什么」只有执行器手里那一份。只落一个状态的话，
        界面只能显示「等你回答」却说不出在等什么。
        """
        return self.patch(
            task_id, status=STATUS_WAITING_INPUT, pending_question=question
        )

    def mark_succeeded(
        self,
        task_id: str,
        *,
        result: str = "",
        usage: Optional[Dict[str, int]] = None,
        files: Optional[List[Dict[str, str]]] = None,
    ) -> Optional[TaskRecord]:
        """
        任务成功收尾。

        ⚠️ 落盘顺序 = 不变量 57：先 `patch` 落盘（结果就可见了），
        再由调用方把结果推给事件流。反过来做的话，
        进程被杀会出现「界面显示成功、但下一轮查不到结果」的不一致。
        """
        from app.agents.think import split_think

        # 结果正文要过一遍 `split_think` —— 与会话历史同一约定：
        # 落盘的必须是**干净的回答**，不是混着英文内心独白的原始流
        _reasoning, clean = split_think(result or "")
        return self.patch(
            task_id,
            status=STATUS_SUCCEEDED,
            result=clean,
            usage=usage or None,
            files=files or None,
        )

    def mark_failed(self, task_id: str, error: str) -> Optional[TaskRecord]:
        """任务失败收尾。`error` 为空时**不写** error 键（宁可没有，不要空串）"""
        return self.patch(task_id, status=STATUS_FAILED, error=error or None)

    # ---------- 删除 ----------

    def delete(self, task_id: str) -> bool:
        """删除单个任务（索引项 + 事件文件）"""
        if not is_valid_task_id(task_id):
            return False
        with self._lock:
            self._ensure_loaded()
            if task_id not in self._index:
                return False
            self._index.pop(task_id, None)
            self._seq.pop(task_id, None)
            try:
                self._task_path(task_id).unlink(missing_ok=True)
            except OSError as e:  # noqa: BLE001
                logger.warning("任务文件删除失败: %s (%s)", task_id, e)
            self._save_index()
        return True

    def delete_by_thread(self, thread_id: str, *, keep_active: bool = True) -> List[str]:
        """
        删掉一个会话的全部后台任务（终态的那些），返回被删掉的 task_id。

        会话被删了而任务还留着，用户就会看到一批「查不到来源」的任务
        在界面上挂着 —— 悬空引用比没有更让人困惑。

        ★ 为什么返回 id 而不是数量
        每个子任务在共享 Checkpointer 里还有一份 `subtask_<task_id>` 状态。
        删了 jsonl 却留着它，Redis 里就永远躺着一份没人能查、也没人能删的
        上下文。调用方拿到 id 才能把那份状态一起丢掉。

        ★ `keep_active=True`（默认）时不动正在跑的任务
        同 `clear` 的理由：删掉它的 jsonl 而子 Agent 还在往里写，
        会产生一个**孤儿文件**（有事件、索引里没有），
        下次索引重建时它会凭空冒出来。
        正在跑的那些跑完落成终态后，会被下一次清理带走。
        """
        # 复用会话的正则（不变量 47：它会变成路径，必须同一份校验）
        from app.conversations.store import is_valid_thread_id

        if not is_valid_thread_id(thread_id):
            return []
        with self._lock:
            self._ensure_loaded()
            victims = [
                tid
                for tid, r in self._index.items()
                if r.get("thread_id") == thread_id
                and not (keep_active and is_active(str(r.get("status"))))
            ]
            for tid in victims:
                self.delete(tid)
            return victims

    def clear(self, *, keep_active: bool = True) -> List[str]:
        """
        清空任务，返回被删掉的 task_id 列表（理由见 `delete_by_thread`）。

        ★ `keep_active=True`（默认）时**不删正在跑的任务**
        删掉一个正在跑的任务不只是「少了一条记录」：
        它的 jsonl 没了，而子 Agent 还在往里 `append_event` ——
        于是产生一个**孤儿文件**（有事件、索引里没有），
        下次索引重建时它会凭空冒出来（`_rebuild_index` 扫目录）。
        界面上则是一个「查不到」的任务显示成功。

        强杀的另一层问题：它已经产生的副作用（写了文件、发了邮件）
        无处可查，而用户以为清空了。

        所以「清空」的语义是「清掉历史」，不是「杀掉正在做的事」。
        正在跑的那些跑完落成终态后，会被下一次 `clear` 或
        `_prune_locked` 带走。
        """
        with self._lock:
            self._ensure_loaded()
            victims = [
                tid
                for tid, r in self._index.items()
                if not (keep_active and is_active(str(r.get("status"))))
            ]
            for task_id in victims:
                self._index.pop(task_id, None)
                self._seq.pop(task_id, None)
                try:
                    self._task_path(task_id).unlink(missing_ok=True)
                except OSError as e:  # noqa: BLE001
                    logger.warning("任务文件删除失败: %s (%s)", task_id, e)
            self._save_index()
        return victims

    # ---------- 淘汰 ----------

    def _prune_locked(self) -> None:
        """超出上限时按最后活跃时间淘汰最旧的任务（连同它的文件）"""
        limit = self.max_items
        if limit <= 0 or len(self._index) <= limit:
            return
        records = self._sorted_records()
        for record in records[limit:]:
            task_id = record.get("task_id")
            if not task_id:
                continue
            # ★ 只淘汰终态任务。正在跑的那个被删掉等于凭空丢一次工作，
            # 而索引里少一条不会有人发现 —— 这是更坏的那种丢。
            if is_active(str(record.get("status"))):
                continue
            self._index.pop(task_id, None)
            self._seq.pop(task_id, None)
            try:
                self._task_path(str(task_id)).unlink(missing_ok=True)
            except OSError as e:  # noqa: BLE001
                logger.warning("淘汰任务文件失败: %s (%s)", task_id, e)


# ---------- 模块级辅助 ----------


def _new_id() -> str:
    from app.tasks.models import make_task_id

    return make_task_id()


def _entry_from_stored(payload: Dict[str, Any]) -> Optional[ProgressEntry]:
    """从文件里的一行还原进度条目

    ⚠️ 这里**逐字段挑拣**而不是整个 dict 直接用：文件是可以被手工改坏的，
    而一个带 `null` 值的 `detail` 会一路走到界面上。
    """
    kind = payload.get("kind")
    label = payload.get("label")
    status = payload.get("status")
    if not isinstance(kind, str) or not isinstance(label, str) or not isinstance(status, str):
        return None
    entry: ProgressEntry = ProgressEntry(
        kind=kind,
        label=label,
        status=status,
    )
    for key in ("detail", "text", "call_id", "at"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            entry[key] = value  # type: ignore[literal-required]
    return entry


def _clean_narrative(text: str) -> str:
    """
    把子 Agent 的一段流式正文收拾成可以落盘、可以直接渲染的段落。

    三件事，缺一不可：
    1. **切掉推理**。不变量 51：子 Agent 的 token 里混着思考正文
       （网关会把独白混进 `content`，还有字面量 `</think>`）。
       落盘的正文必须干净 —— 进度条目会长期留在 jsonl 里，
       事后补切是补不回来的。
    2. **保留换行，不压平**。`label` 用 `truncate_label` 压平成一行是对的
       （它要塞进列表），但正文是**段落**：压平会把「1. … 2. …」
       变成一坨。所以这里只收敛连续空行。
    3. **按上限截断并补省略号**。截断必须说出来，不能悄悄截
       （与 `total_steps` 同一约定）。
    """
    from app.agents.think import split_think

    _reasoning, clean = split_think(text or "")
    # 收敛 3 个以上连续换行 → 2 个（保留段落感，但不留大段空白）
    lines = [ln.rstrip() for ln in clean.splitlines()]
    collapsed: list[str] = []
    blanks = 0
    for ln in lines:
        if ln:
            blanks = 0
            collapsed.append(ln)
        else:
            blanks += 1
            if blanks <= 1:
                collapsed.append("")
    body = "\n".join(collapsed).strip()
    if len(body) <= PROGRESS_TEXT_CHARS:
        return body
    return body[: PROGRESS_TEXT_CHARS - 1] + "…"


def _entry_from_event(event: Dict[str, Any]) -> Optional[ProgressEntry]:
    """
    把子 Agent 的一个事件折叠成一条进度 —— **全项目唯一**的折叠函数。

    ★ 为什么实时流与轮询必须共用它
    两条通道给前端的是同一份事实（同一个任务的进度）。
    如果两边各自判断「什么事件算一步进度」，
    就会漂移成「实时看得到、刷新后看不到」——
    而用户对这种不一致的信任度是零。
    """
    etype = event.get("type")

    if etype == "sources":
        items = event.get("content")
        count = len(items) if isinstance(items, list) else 0
        detail = f"命中 {count} 个来源" if count else "未检索到来源"
        return ProgressEntry(kind="sources", label="查资料", status="ok", detail=detail)

    if etype == "tool_call":
        name = str(event.get("tool_name") or "工具")
        label = str(event.get("tool_label") or name)
        return ProgressEntry(
            kind="tool_call",
            label=f"调用 {label}",
            status="running",
            detail=label,
            call_id=str(event.get("call_id") or "") or None,  # type: ignore[typeddict-item]
        )

    if etype == "tool_result":
        name = str(event.get("tool_name") or "工具")
        label = str(event.get("tool_label") or name)
        ok = bool(event.get("success"))
        call_id = str(event.get("call_id") or "")
        entry = ProgressEntry(
            kind="tool_result",
            label=f"{label} {'完成' if ok else '失败'}",
            status="ok" if ok else "failed",
            detail=label,
        )
        if call_id:
            entry["call_id"] = call_id
        return entry

    if etype in {"interrupt", "approval_auto"}:
        name = str(event.get("tool_name") or "危险操作")
        label = str(event.get("tool_label") or name)
        entry = ProgressEntry(
            kind="approval",
            label=f"{label} 等你确认" if etype == "interrupt" else f"{label} 已自动放行",
            status="awaiting" if etype == "interrupt" else "ok",
            detail=label,
        )
        call_id = str(event.get("call_id") or "")
        if call_id:
            entry["call_id"] = call_id
        return entry

    if etype == "state_update" and event.get("state_key") == "approval":
        # ★ 审批决策也要落进度 —— 否则 approval 条目永远停在「等你确认」，
        # 用户批准/拒绝/超时之后，进度列表里那条仍是 awaiting，
        # 与任务已经往下走的事实对不上。
        approved = bool(event.get("value"))
        return ProgressEntry(
            kind="approval",
            label=truncate_label(
                str(event.get("content") or ("已批准执行" if approved else "已拒绝执行"))
            ),
            status="ok" if approved else "failed",
        )

    if etype == "question":
        # 提问与审批都进进度，但 kind 不同：一个是「问你选哪个」，
        # 一个是「问你同不同意」。前端据此决定要不要画选项。
        questions = event.get("questions") or []
        first = questions[0] if isinstance(questions, list) and questions else {}
        header = str(first.get("header") or first.get("question") or "有问题要问你")
        entry = ProgressEntry(
            kind=PROGRESS_QUESTION,
            label=truncate_label(f"向你提问：{header}"),
            status="awaiting",
            detail=truncate_label(str(first.get("question") or "")),
        )
        call_id = str(event.get("call_id") or "")
        if call_id:
            entry["call_id"] = call_id
        return entry

    if etype == "question_resolved":
        status = str(event.get("status") or "")
        # 跳过不是失败：用户看过后主动放弃，任务会按假设继续。
        # 只有超时才值得在进度里标红（它是「根本没人看」的信号）。
        ok = status in {"answered", "skipped"}
        return ProgressEntry(
            kind=PROGRESS_QUESTION,
            label=truncate_label(str(event.get("content") or "提问已结束")),
            status="ok" if ok else "failed",
        )

    if etype == "error":
        return ProgressEntry(
            kind="error",
            label=truncate_label(str(event.get("content") or "出错了")),
            status="failed",
        )

    if etype == "done":
        # `done` 里若有 answer 就用它（外层已切分过，不变量 51），
        # 没有则退回 content —— ⚠️ 但**仍要自己切一次**：
        # 不能假设调用方一定切过。外层切分是为了不把推理混进界面，
        # 而进度条目要落进 jsonl 长期保存，它必须自己保证干净。
        answer = str(event.get("answer") or event.get("content") or "")
        if not answer.strip():
            # 没有正文就不记这一步 —— 「完成」后面什么都不写
            # 会被界面渲染成一个空步骤，比不记更让人困惑
            return None
        from app.agents.think import split_think

        _reasoning, clean = split_think(answer)
        if not clean.strip():
            return None
        return ProgressEntry(kind="answer", label=truncate_label(clean), status="ok")

    # token / state_update / memory 等不进进度：
    # token 是逐字的增量（一步几万条），其余是装饰性或旁路信号。
    return None


def approval_gate_snapshot() -> Dict[str, Any]:
    """审批闸门的当前状态（闸门模块可能还不存在 —— 阶段 1 时它还没建）"""
    try:
        from app.agents.approval_gate import approval_gate

        return approval_gate.snapshot()
    except Exception:  # noqa: BLE001
        return {"available": False}


#: 进程级单例。与 `conversations.store` 的 `conversation_store` 同理：
#: 单进程部署，取单例是为了让索引与事件文件只被一把锁保护。
task_store = TaskStore()
