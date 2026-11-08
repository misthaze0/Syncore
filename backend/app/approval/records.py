"""
审批留痕 —— 把「谁在什么时候批了什么」变成可查询的实体

★ 为什么必须落盘

在 Phase 6 之前，一次审批的全部痕迹只存在于两处：
`ApprovalBroker` 的内存字典（请求结束即清）与 trace 的 JSONL（按天分片、
面向链路调试、不是面向审计查询）。这带来两个后果：

1. **重启即失忆**。容器重启时正在等待审批的请求全部消失，
   事后无法回答"那个 `rm -rf` 到底批没批"。
2. **无法聚合**。想做"最近 20 次危险操作有几次被拒"这种判断，
   唯一办法是 grep 全部 trace —— 那是链路数据，不是审计数据。

所以本模块把审批提升为**一等实体**：每次请求落一条、每次决策补一条、
每次执行结果再补一条。落盘用 **append-only 事件日志**，而不是"改一行"：

- 追加写天然崩溃安全（写一半最多多一条半截行，加载时跳过即可）
- 与长期记忆的「原子整文件写」是相反取舍，因为这里的更新频率更高、
  且**历史不可覆盖**（审批记录的价值就在"不可篡改"）

★ 为什么是「事件日志 + 折叠」而不是一张表

一个审批有三个时刻（请求、决策、执行结果），它们由**三个不同的代码位置**
产生（graph 的 interrupt 分支、/api/approval、graph 的 tool_result 分支）。
如果设计成"就地更新一条记录"，三个位置都要去改同一个文件/文档，
并发与顺序都会成为问题。设计成"各自追加一条事件"，三个位置互相独立，
查询时按 `record_id` 折叠取最新状态即可。

★ 查询口径

`list_records()` 折叠后的最新状态；`decision="pending"` 表示"请求了但没等到决策"
（超时、断连、进程被杀都会留下这种记录 —— 它本身就是有价值的信号：
一个长期 pending 的记录意味着有请求卡在那里）。

⚠️ 审批记录**不写入请求参数里的敏感内容**的过滤 —— 目前工具参数会原样记录，
因为审计的前提是完整。但这也意味着 `data/approvals/` 与 `backend/.env` 同级敏感，
需要与 trace 目录一样列进 `.gitignore`。
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

logger = logging.getLogger(__name__)

RECORD_VERSION = 1

#: 决策取值
DECISION_PENDING = "pending"
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISION_TIMEOUT = "timeout"

#: 决策来源
BY_USER = "user"
BY_GRANT = "grant"
BY_TIMEOUT = "timeout"
BY_SYSTEM = "system"
# ---- Phase 5：没有人点按钮的那些次 ----
# ★ 为什么必须区分开来
# 「已批准」有三种完全不同的来历：人点的、会话预授权放行的、权限规则/模式自动放行的。
# 都写成 `user` 的话，审计里就会出现一堆「用户批准了他根本没见过的操作」的记录 ——
# 那种数据比没有数据更糟（不变量 23）。
BY_RULE = "rule"
BY_MODE = "mode"
BY_HOOK = "hook"

DECISION_LABELS = {
    DECISION_PENDING: "等待中",
    DECISION_APPROVED: "已批准",
    DECISION_REJECTED: "已拒绝",
    DECISION_TIMEOUT: "超时未决",
}

BY_LABELS = {
    "assistant": "电脑操控授权",
    BY_USER: "用户点击",
    BY_GRANT: "预授权自动放行",
    BY_TIMEOUT: "等待超时",
    BY_SYSTEM: "系统",
    BY_RULE: "权限规则",
    BY_MODE: "权限模式",
    BY_HOOK: "自定义钩子",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


@dataclass
class ApprovalRecord:
    """一次审批的完整留痕（三个时刻折叠后的结果）"""

    record_id: str
    thread_id: str = ""
    call_id: str = ""
    tool_name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    danger_level: str = "danger"
    reason: str = ""

    #: ★ 护栏 6「审批不可委托」的两维（施工图 §5.7）。
    #: 工作空间里 `thread_id` 就是群组 id，三个员工共用它 ——
    #: 少了这两列，审计就回答不出「这条批准放行的是谁的操作」，
    #: 而审批面板上三条一模一样的弹窗里，人类其实是在盲签。
    #: 主对话（非工作空间）两维为空串，附加式改动、老记录读得出来。
    run_id: str = ""
    employee_id: str = ""

    requested_at: str = ""
    decided_at: str = ""
    decision: str = DECISION_PENDING
    decided_by: str = ""
    grant_id: str = ""
    #: 从请求到决策的人机往返耗时（毫秒）——「审批体验」唯一的量化指标
    latency_ms: Optional[float] = None

    #: 工具是否真的被执行、结果是否成功（拒绝/超时为 null）
    executed: Optional[bool] = None
    success: Optional[bool] = None
    result: str = ""

    @property
    def decision_label(self) -> str:
        return DECISION_LABELS.get(self.decision, self.decision)

    @property
    def decided_by_label(self) -> str:
        return BY_LABELS.get(self.decided_by, self.decided_by or "—")

    @property
    def is_settled(self) -> bool:
        return self.decision != DECISION_PENDING

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["decision_label"] = self.decision_label
        data["decided_by_label"] = self.decided_by_label
        data["is_settled"] = self.is_settled
        return data


class ApprovalStore:
    """
    审批记录仓库（append-only JSONL + 内存环形缓冲）

    写入是**同步**的：审批是低频事件（一次危险操作一条），
    同步落盘换来的是"决策完立刻可查"，而异步队列会引入
    "刚批完却查不到"的困惑，与 trace 的取舍相反。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._records: Dict[str, ApprovalRecord] = {}
        self._order: Deque[str] = deque()
        self._written = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "APPROVAL_RECORD_ENABLED", True))

    @property
    def path(self) -> Path:
        return self._path or settings.approval_record_path

    @property
    def memory_limit(self) -> int:
        return max(1, int(getattr(settings, "APPROVAL_RECORD_MEMORY", 500)))

    # ---------- 写入 ----------
    def _append(self, event: Dict[str, Any]) -> None:
        """
        追加一条事件。

        ⚠️ 任何 IO 失败都只告警、不抛 —— 审批留痕是**旁路能力**，
        它挂掉不该让一次危险操作无法执行（那会变成"因为记不了账，
        所以不能批"，把审计做成了阻塞点）。
        """
        if not self.enabled:
            return
        try:
            # 显式建父目录：`settings.approval_record_path` 会自己 mkdir，
            # 但 `ApprovalStore(path=...)` 的调用方（测试、自定义部署）不一定。
            # 少了这一行，症状是“留痕文件永远是空的、只有一条 easy-to-miss 的告警”。
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._written += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("审批留痕写入失败（不影响审批本身）: %s", e)

    def _remember(self, record: ApprovalRecord) -> None:
        """
        更新内存 + 追加一条快照事件。

        ⚠️ 落盘放在锁**内**。看起来"文件 IO 不该占着锁"，但这里必须如此：
        同一个 `record_id` 会先后产生「请求」「决策」「执行结果」三次快照，
        而重载时是**按行序折叠取最后一条**。若两次 append 的先后与内存更新的
        先后不一致，重启回填就会把一条更早的状态当成最新状态 ——
        症状是"审批面板上那条明明是批准了，重启后显示还在等待"。
        审批是人点出来的低频事件，同步写文件的开销无所谓。
        """
        with self._lock:
            if record.record_id not in self._records:
                self._order.append(record.record_id)
            self._records[record.record_id] = record
            # 环形上限：超出的从内存里丢，但**磁盘上仍在**
            # （查询接口只服务“最近发生了什么”，历史走 grep）
            while len(self._order) > self.memory_limit:
                evicted = self._order.popleft()
                if evicted != record.record_id:
                    self._records.pop(evicted, None)
            self._append({"v": RECORD_VERSION, "event": "snapshot", "record": record.to_dict()})

    # ---------- 生命周期 ----------
    def open_request(
        self,
        *,
        thread_id: str,
        call_id: str,
        tool_name: str,
        args: Optional[Dict[str, Any]] = None,
        danger_level: str = "danger",
        reason: str = "",
        run_id: str = "",
        employee_id: str = "",
    ) -> ApprovalRecord:
        """
        登记一次审批请求（在**推送 interrupt 之前**调用）。

        顺序很重要：先落库再推送事件，这样"前端看到了弹窗"必然意味着
        "记录已经存在"。反过来写的话，进程在两者之间被杀，就会出现
        "用户说弹过窗、系统里查无此事"。

        `run_id` / `employee_id` 是护栏 6 要求的那两维（见 `ApprovalRecord`）：
        一次批准必须回答得出「放行的是哪一次协作、哪个员工的操作」。
        """
        record = ApprovalRecord(
            record_id=uuid.uuid4().hex[:16],
            thread_id=thread_id,
            call_id=call_id,
            tool_name=tool_name,
            args=dict(args or {}),
            danger_level=danger_level,
            reason=reason,
            run_id=run_id,
            employee_id=employee_id,
            requested_at=_now_iso(),
        )
        self._remember(record)
        return record

    def settle(
        self,
        record_id: str,
        *,
        decision: str,
        decided_by: str = BY_USER,
        grant_id: str = "",
    ) -> Optional[ApprovalRecord]:
        """登记决策结果（批准 / 拒绝 / 超时 / 预授权放行）"""
        record = self._records.get(record_id)
        if record is None:
            return None
        record.decision = decision
        record.decided_by = decided_by
        record.grant_id = grant_id
        record.decided_at = _now_iso()
        record.latency_ms = _elapsed_ms(record.requested_at, record.decided_at)
        self._remember(record)
        return record

    def mark_executed(
        self,
        record_id: str,
        *,
        success: bool,
        result: str = "",
    ) -> Optional[ApprovalRecord]:
        """登记工具的实际执行结果（拒绝/超时的记录不会走到这里）"""
        record = self._records.get(record_id)
        if record is None:
            return None
        record.executed = True
        record.success = bool(success)
        record.result = str(result)[:500]
        self._remember(record)
        return record

    # ---------- 查询 ----------
    def get(self, record_id: str) -> Optional[ApprovalRecord]:
        return self._records.get(record_id)

    def find_by_call(
        self,
        thread_id: str,
        call_id: str,
        *,
        run_id: str = "",
        employee_id: str = "",
    ) -> Optional[ApprovalRecord]:
        """
        按四元组查最近的记录 —— graph 分支用它回填执行结果。

        ★ 后两维是**精确匹配**（空串只匹配空串），不做「取最近一条 call_id 相同的」
        便利匹配。理由与 broker 的 key 一样：群组里三个员工共用 thread_id，
        而 `call_id` 是模型给的短 id、并发下会撞。挑错一条的后果不是显示不对，
        是「甲的执行结果写进了乙的审批记录」—— 审计数据被交叉污染。
        """
        for record_id in reversed(self._order):
            record = self._records.get(record_id)
            if (
                record
                and record.thread_id == thread_id
                and record.call_id == call_id
                and record.run_id == run_id
                and record.employee_id == employee_id
            ):
                return record
        return None

    def list_records(
        self,
        *,
        thread_id: Optional[str] = None,
        decision: Optional[str] = None,
        tool_name: Optional[str] = None,
        employee_id: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[ApprovalRecord]:
        """按条件列出记录（最新的在前）"""
        out: List[ApprovalRecord] = []
        for record_id in reversed(self._order):
            record = self._records.get(record_id)
            if record is None:
                continue
            if thread_id and record.thread_id != thread_id:
                continue
            if decision and record.decision != decision:
                continue
            if tool_name and record.tool_name != tool_name:
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
        """留痕统计（供启动日志）"""
        by_decision: Dict[str, int] = {}
        by_tool: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        latencies: List[float] = []
        for record in self._records.values():
            by_decision[record.decision] = by_decision.get(record.decision, 0) + 1
            by_tool[record.tool_name] = by_tool.get(record.tool_name, 0) + 1
            if record.decided_by:
                by_source[record.decided_by] = by_source.get(record.decided_by, 0) + 1
            if record.latency_ms is not None:
                latencies.append(record.latency_ms)

        return {
            "enabled": self.enabled,
            "path": str(self.path),
            "in_memory": len(self._records),
            "written": self._written,
            "by_decision": by_decision,
            "by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1])[:10]),
            "by_source": by_source,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "pending": by_decision.get(DECISION_PENDING, 0),
        }

    # ---------- 启动清理归档 ----------
    def cleanup_old_records(self) -> Dict[str, int]:
        """
        把超过保留期的记录从 records.jsonl **归档**到 archive/records-YYYY-MM.jsonl。

        与 trace 的 ``cleanup_old_files`` 不同 —— 审批是单文件 append-only，
        清理是「按行迁移」而不是「整文件删除」。归档而不删除：审批记录是
        审计证据，宁可磁盘多点也不能让"半年前批过什么"变得不可回答。

        ★ 与 ``load_recent`` 的顺序：先清理再回填。这样回填不会读到马上要
        归档的行；保留期收紧后重启立即生效，而不是等下一次启动。

        ★ 崩溃安全：先写归档文件（append 模式，重复清理同一文件不会重复归档）
        → 再写 records.jsonl.tmp → 最后 rename。归档中途失败的话，这批行仍在
        records.jsonl，下次再试。

        ★ 时间解析失败的行**保留不动** —— 与 trace 同一原则：磁盘是排查证据，
        宁漏归档不误移。半截 JSON 行同理保留（load_recent 跳过它们也是这个理由）。

        :returns: ``{"kept": int, "archived": int, "errors": int}``
        """
        result = {"kept": 0, "archived": 0, "errors": 0}
        if not self.enabled:
            return result

        retention = int(getattr(settings, "APPROVAL_RETENTION_DAYS", 90))
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
                        # 半截行（写入时进程被杀）—— 保留，load_recent 同样会跳过
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
                        # 时间不可解析 → 保留（宁漏归档不误移）
                        keep_lines.append(line)
                        continue

                    if ts < cutoff:
                        month_key = ts.strftime("%Y-%m")
                        archive_by_month.setdefault(month_key, []).append(line)
                    else:
                        keep_lines.append(line)
        except Exception as e:  # noqa: BLE001
            # 整体读失败 → 不动文件，避免把读错误升级成数据丢失
            logger.warning("审批留痕清理失败（保留原文件）: %s", e)
            result["errors"] = 1
            return result

        if not archive_by_month:
            result["kept"] = len(keep_lines)
            return result

        # 1) 写归档文件（按月分片，append 模式）。失败的那批行留在 keep_lines，
        #    下次清理会再试 —— 等价于“归档幂等”。
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
                logger.warning(
                    "归档写入失败 %s: %s（这批行仍在 records.jsonl，下次再试）",
                    archive_path,
                    e,
                )
                keep_lines.extend(lines)
                result["errors"] += 1

        if archived == 0:
            result["kept"] = len(keep_lines)
            return result

        # 2) 原子重写 records.jsonl：先写 .tmp 再 rename。
        #    重写失败时归档文件**已写**，下次重启会发现 records.jsonl 仍是旧行
        #    （归档文件会多一份重复记录，stats 不读 archive，对前端透明）。
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                fh.writelines(keep_lines)
            tmp_path.replace(path)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "原子重写 records.jsonl 失败（归档已写，但 records.jsonl 仍是旧文件）: %s",
                e,
            )
            result["errors"] += 1

        result["kept"] = len(keep_lines)
        result["archived"] = archived
        return result

    # ---------- 启动回填 ----------
    def load_recent(self, limit: Optional[int] = None) -> int:
        """
        启动时从磁盘回填最近若干条记录进内存。

        不回填的话，「重启后审批面板空白、但文件里明明有数据」会成为一个
        反复被问的问题（与 TRACE_LOAD_ON_START 是同一个坑）。

        文件按行追加，可能很大，所以从**尾部**读：先读全量再截尾在
        记录数上万时会有明显的启动延迟。这里简单地整体读取后切片 ——
        审批记录的增长速率是"人工点击"级别，远达不到需要倒序读文件的量。
        """
        if not self.enabled:
            return 0
        path = self.path
        if not path.is_file():
            return 0
        cap = self.memory_limit if limit is None else max(1, limit)

        loaded = 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception as e:  # noqa: BLE001
            logger.warning("审批留痕文件不可读，按空处理: %s", e)
            return 0

        for line in lines[-cap * 6 :]:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # 半截行（写入时进程被杀）—— 跳过即可，不影响其余记录
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
            loaded += 1

        while len(self._order) > cap:
            evicted = self._order.popleft()
            self._records.pop(evicted, None)
        return len(self._records)

    def reset(self) -> None:
        """清空内存（测试用；磁盘文件不动）"""
        with self._lock:
            self._records.clear()
            self._order.clear()
            self._written = 0


def _record_from_dict(data: Dict[str, Any]) -> Optional[ApprovalRecord]:
    """从 JSON 还原记录（字段缺失/类型不对时返回 None 而不是抛异常）"""
    record_id = str(data.get("record_id", "") or "")
    if not record_id:
        return None
    return ApprovalRecord(
        record_id=record_id,
        thread_id=str(data.get("thread_id", "")),
        call_id=str(data.get("call_id", "")),
        tool_name=str(data.get("tool_name", "")),
        args=dict(data.get("args") or {}),
        danger_level=str(data.get("danger_level", "danger")),
        reason=str(data.get("reason", "")),
        # ★ 白名单里少这两行，症状不是报错而是「重启后审批面板上的记录不再显示是谁请求的」
        # —— 内存里有（`open_request` 写了），磁盘回填后没了。
        run_id=str(data.get("run_id", "")),
        employee_id=str(data.get("employee_id", "")),
        requested_at=str(data.get("requested_at", "")),
        decided_at=str(data.get("decided_at", "")),
        decision=str(data.get("decision", DECISION_PENDING)),
        decided_by=str(data.get("decided_by", "")),
        grant_id=str(data.get("grant_id", "")),
        latency_ms=data.get("latency_ms"),
        executed=data.get("executed"),
        success=data.get("success"),
        result=str(data.get("result", "")),
    )


def _elapsed_ms(start_iso: str, end_iso: str) -> Optional[float]:
    """两个 ISO 时间之间的毫秒差；解析失败返回 None（不猜一个数出来）"""
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return None
    return round((end - start).total_seconds() * 1000, 1)


# 全局单例
approval_store = ApprovalStore()


__all__ = [
    "BY_GRANT",
    "BY_HOOK",
    "BY_LABELS",
    "BY_MODE",
    "BY_RULE",
    "BY_SYSTEM",
    "BY_TIMEOUT",
    "BY_USER",
    "DECISION_APPROVED",
    "DECISION_LABELS",
    "DECISION_PENDING",
    "DECISION_REJECTED",
    "DECISION_TIMEOUT",
    "RECORD_VERSION",
    "ApprovalRecord",
    "ApprovalStore",
    "approval_store",
]
