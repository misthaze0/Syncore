"""
用量账本 —— 「这一次调用花了多少 token」的永久流水

★ 为什么不复用 trace，而是单独一本账

`app/observability/` 里已经有链路追踪了，理论上从 trace 聚合就能算出用量，
但它的生命周期和「账」这个语义根本不匹配：

|            | trace（诊断）        | 账本（事实）          |
|------------|---------------------|---------------------|
| 保留期     | `TRACE_RETENTION_DAYS`，默认 30 天后**删文件** | 不设保留期，只增不删 |
| 内存缓冲   | 最近 500 条环形缓冲，超出即淘汰 | 不留缓冲，读时按分片扫描 |
| 模型名     | 只在 `llm` span 的 metadata 里，列表接口取不到 | 每条记录一个字段 |
| 覆盖面     | 主对话 direct 链路根本没埋 `llm` span | 每条回答链路都记 |

用「会被滚动删除、且查询窗口只有 500 条」的东西去记账，结果是**账目会凭空变小** ——
用户上周花了 40 万 token，这周打开一看只剩 8 万，而且无从解释差额。
账本必须比诊断数据活得久，所以它是独立的一本账。

★ 为什么「无法删除」

- **没有** DELETE 接口，**没有** `clear()`，**没有** 保留期清理任务 —— 不是「暂时没做」，
  是这三样一旦存在，账本就不再是账本。
- 唯一的写入口是 `append()`，它只追加。
- 会话可以被用户删掉（`conversations/store.py` 的 `delete_conversation`），
  但删会话**不碰账本**：记录里的 `title` 是当时的快照，会话没了仍能认出是哪个窗口。
- 真要清理只能用户自己在关闭后端后删 `data/usage/*.jsonl`。界面上如实写明这一点。

★ 为什么仍然是 JSONL，而不是 SQLite

与 trace 同一套理由：单机桌面应用、访问形态只有「按时间倒序翻」和「按窗口/模型聚合」，
没有 join、没有事务。账本每天每用户几百行（一行 ≈ 200 字节），
十年也就几十 MB，扫文件完全够。而 append-only 文本在 SQLite 半截事务、
WAL 文件、锁库这些故障模式上天然免疫 —— 对「不能被改坏的账」这点很重要。

★ 读侧为什么不缓存全量

聚合查询（按天曲线、按窗口汇总）要扫全量，但账本可能有很多年的历史。
所以：按 `days` 参数决定读几个日分片（日分片命名可控），
每个分片按 (mtime, size) 缓存解析结果 —— 历史分片写完就不再变，缓存永远有效，
只有当天那个分片会因 mtime 变化重读。这样不需要维护任何增量状态，
也就不会出现「缓存和磁盘对不上」这类最难查的 bug。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 未显式配置时的默认落盘目录（相对 backend/）
DEFAULT_USAGE_DIR = "./data/usage"

#: 分片文件名模式：usage-YYYY-MM-DD.jsonl
_SHARD_PREFIX = "usage-"
_SHARD_SUFFIX = ".jsonl"

#: token 字段（账目口径的唯一来源，前端与聚合都按这个顺序/集合走）
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")

#: 记录用途取值 —— 只有「回答链路」入账（用户定的口径：意图识别、标题生成、
#: 记忆抽取、联网搜索这类旁路调用不进账本，它们不是「这次对话花了多少」的一部分）
PURPOSE_ANSWER = "answer"        # 主对话直接回答（含工具循环的每一步）
PURPOSE_SUBAGENT = "subagent"    # 子 Agent（管家）在一次问答里的调用

#: 对话窗口类型（由 thread_id 前缀派生，界面用它区分「主会话 / 员工私聊 / 后台任务」）
WINDOW_CHAT = "chat"
WINDOW_EMPLOYEE = "employee"
WINDOW_SUBTASK = "subtask"
WINDOW_ASSISTANT = "assistant"
WINDOW_LABELS = {
    WINDOW_CHAT: "会话",
    WINDOW_EMPLOYEE: "员工私聊",
    WINDOW_SUBTASK: "后台任务",
    WINDOW_ASSISTANT: "助理会话",
}


def window_kind_of(thread_id: str) -> str:
    """由 thread_id 前缀判断这条记录属于哪一类对话窗口"""
    if thread_id.startswith("emp_"):
        return WINDOW_EMPLOYEE
    if thread_id.startswith("subtask_"):
        return WINDOW_SUBTASK
    if thread_id.startswith("assistant_"):
        return WINDOW_ASSISTANT
    return WINDOW_CHAT


def _clean_int(value: Any) -> int:
    """把任意来源的 token 数收成非负整数；拿不到就 0（不猜、不补）"""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _clamp_text(value: Any, max_len: int) -> str:
    text = str(value or "")
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


class UsageLedger:
    """用量账本：只追加的 JSONL 日分片 + 读时聚合"""

    def __init__(
        self,
        directory: str | Path = DEFAULT_USAGE_DIR,
        *,
        enabled: bool = True,
        max_rows_per_file: int = 200000,
    ) -> None:
        self.enabled = enabled
        self.directory = Path(directory)
        self.max_rows_per_file = max_rows_per_file
        # 追加写要串行化：一轮问答里多个 Agent 会并发落账
        self._lock = threading.Lock()
        # 分片解析缓存：path -> (mtime, size, rows)。历史分片写完即冻结，命中即免扫
        self._shard_cache: Dict[str, Tuple[float, int, List[Dict[str, Any]]]] = {}
        self._appended = 0
        self._errors = 0

    # ---------- 写入（唯一入口，只追加） ----------
    def append(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        记一笔用量。返回补齐后的记录（用于测试与调试），未启用/失败时返回 None。

        ⚠️ 与 trace 落盘同一条纪律：**同步写、绝不抛异常**。
        调用点在 LLM 回合结束的收尾处（事件流的 finally 也会走到），
        那里 `create_task` 可能随事件循环一起被取消 —— 账就静默少一笔。
        一次追加只有亚毫秒级开销，同步写完是唯一可靠的选择。
        """
        if not self.enabled:
            return None
        try:
            payload = self._normalize(record)
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path_for(payload["ts_ms"])
            with self._lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                # 刚写过的那个分片的缓存已失效（下次读会因 mtime 变化自动重读，
                # 这里显式摘掉是为了不让「同一毫秒内 mtime 精度不够」这种边角情况留脏缓存）
                self._shard_cache.pop(path.name, None)
            self._appended += 1
            return payload
        except Exception as e:  # noqa: BLE001
            # 记账失败绝不能把问答打挂 —— 它和观测层是同一类旁路能力
            self._errors += 1
            logger.warning("用量记录写入失败（已忽略）: %s", e)
            return None

    @staticmethod
    def _normalize(record: Dict[str, Any]) -> Dict[str, Any]:
        """
        收口成「一定可序列化、字段一定在」的一行账。

        只保留白名单字段 —— 账本是长期资产，不能让某次改动顺手把
        一个大对象塞进每一行，那会让历史分片一起变重。
        """
        ts_ms = record.get("ts_ms")
        try:
            ts_ms = float(ts_ms) if ts_ms is not None else datetime.now().timestamp() * 1000.0
        except (TypeError, ValueError):
            ts_ms = datetime.now().timestamp() * 1000.0

        tokens = {name: _clean_int(record.get(name)) for name in TOKEN_FIELDS}
        # 网关有时只给 prompt/completion（不给 total）—— 这里补齐是口径推导，不是估算
        if not tokens["total_tokens"] and (tokens["prompt_tokens"] or tokens["completion_tokens"]):
            tokens["total_tokens"] = tokens["prompt_tokens"] + tokens["completion_tokens"]

        thread_id = _clamp_text(record.get("thread_id"), 120)
        payload: Dict[str, Any] = {
            "record_id": str(record.get("record_id") or uuid.uuid4().hex[:12]),
            "ts_ms": round(ts_ms, 2),
            "at": datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds"),
            "thread_id": thread_id,
            "window_kind": window_kind_of(thread_id),
            # 标题快照：会话删掉之后仍认得出这是哪个窗口。读侧优先用当前索引里的标题
            "title": _clamp_text(record.get("title"), 120),
            "purpose": _clamp_text(record.get("purpose") or PURPOSE_ANSWER, 24),
            "agent": _clamp_text(record.get("agent"), 60),
            "model": _clamp_text(record.get("model"), 120),
            "step": _clean_int(record.get("step")),
            "duration_ms": round(float(record.get("duration_ms") or 0.0), 2),
            "status": _clamp_text(record.get("status") or "ok", 16),
            # 网关没返回 usage 时这一笔是 0。标出来，界面就能说「这次没上报」
            # 而不是显示一个看起来像免费的 0（本项目不做字符数估算 —— 估出来的数没法对账）。
            "reported": bool(record.get("reported", True)),
            "trace_id": _clamp_text(record.get("trace_id"), 64),
            **tokens,
        }
        return payload

    def _path_for(self, ts_ms: float) -> Path:
        """按调用时间决定落在哪一天的分片里（与 trace 同一套日分片口径）"""
        day = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
        return self.directory / f"{_SHARD_PREFIX}{day}{_SHARD_SUFFIX}"

    # ---------- 读取 ----------
    def _shards(self, *, newest_first: bool = True, days: Optional[int] = None) -> List[Path]:
        """
        列出要扫的分片。`days` 给定时只取最近 N 个**自然日**（含今天）。

        按日期名过滤而不是「目录里最后 N 个文件」—— 中间某天没调用就没有分片，
        按文件数取会让「30 天」实际覆盖 30 个有活动的日子，曲线跨度会悄悄变长。
        """
        if not self.directory.exists():
            return []
        paths = [p for p in self.directory.glob(f"{_SHARD_PREFIX}*{_SHARD_SUFFIX}") if self._parse_day(p.name)]
        if days is not None and days > 0:
            today = datetime.now().date()
            keep = {(today - timedelta(days=i)).isoformat() for i in range(days)}
            paths = [p for p in paths if self._day_of(p.name) in keep]
        return sorted(paths, key=lambda p: p.name, reverse=newest_first)

    @staticmethod
    def _parse_day(name: str) -> Optional[date]:
        day = UsageLedger._day_of(name)
        if day is None:
            return None
        try:
            return datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def _day_of(name: str) -> Optional[str]:
        """从文件名取日期串；命名不认识就返回 None（宁可漏读也不碰陌生文件）"""
        if not (name.startswith(_SHARD_PREFIX) and name.endswith(_SHARD_SUFFIX)):
            return None
        raw = name[len(_SHARD_PREFIX) : -len(_SHARD_SUFFIX)]
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
        return raw

    def _read_shard(self, path: Path) -> List[Dict[str, Any]]:
        """
        读一个分片（带 (mtime, size) 缓存）。

        缓存不需要失效策略：写完即冻结是 JSONL 追加的本质，只有当天分片会变，
        而它一变 mtime 就变，自然回源。
        """
        try:
            stat = path.stat()
            key = (stat.st_mtime, stat.st_size)
        except OSError:
            return []

        cached = self._shard_cache.get(path.name)
        if cached and cached[0] == key[0] and cached[1] == key[1]:
            return cached[2]

        rows: List[Dict[str, Any]] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if len(rows) >= self.max_rows_per_file:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # 账本里出现坏行 = 被手工改坏或磁盘写坏。跳过它继续算，
                        # 而不是让整页用量打不开（少一笔比全看不到好）
                        continue
                    if isinstance(data, dict):
                        rows.append(data)
        except OSError as e:
            logger.warning("读取用量分片失败 %s: %s", path, e)
            return rows  # 返回已读到的部分，不写进缓存（下次重试）

        self._shard_cache[path.name] = (key[0], key[1], rows)
        return rows

    def iter_records(self, *, days: Optional[int] = None) -> Iterable[Dict[str, Any]]:
        """按时间**正序**产出记录（跨分片拼接用）。`days=None` 表示全部历史"""
        for path in self._shards(newest_first=False, days=days):
            yield from self._read_shard(path)

    def iter_records_desc(self, *, days: Optional[int] = None) -> Iterable[Dict[str, Any]]:
        """按时间**倒序**产出记录（明细列表用）"""
        for path in self._shards(newest_first=True, days=days):
            yield from reversed(self._read_shard(path))

    def query(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        days: Optional[int] = None,
        model: Optional[str] = None,
        thread_id: Optional[str] = None,
        window_kind: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        倒序翻账，返回 (页内记录, 过滤后的总条数)。

        总数靠扫一遍得到 —— 账本没有计数器可依赖（重启就归零），
        而「共 N 条」必须和过滤条件一致，不能显示一个未过滤的总数。

        ⚠️ 排序显式做，不靠「分片内倒序读」：`ts_ms` 记的是**调用开始**时间，
        而落账发生在调用结束 —— 并发的多个 Agent 会让两笔账的落盘顺序与开始顺序相反。
        按文件顺序倒读会把这些笔排错，排一次成本几乎为零。
        """
        matched = [
            r
            for r in self.iter_records_desc(days=days)
            if _matches(r, model=model, thread_id=thread_id, window_kind=window_kind)
        ]
        matched.sort(key=lambda r: _time_of(r.get("ts_ms")), reverse=True)
        return matched[offset : offset + limit], len(matched)

    # ---------- 聚合 ----------
    def summary(
        self,
        *,
        days: int = 30,
        model: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        一次扫描出全部聚合视图：按天曲线、按模型、按对话窗口。

        刻意放在一个方法里：三个视图若各扫一遍，同一次打开页面要读三遍磁盘，
        而且三份口径（过滤条件）极易写歪 —— 曲线和表格对不上是最难解释的 bug。
        """
        per_day: Dict[str, Dict[str, Any]] = {}
        per_model: Dict[str, Dict[str, Any]] = {}
        per_window: Dict[str, Dict[str, Any]] = {}
        totals = {name: 0 for name in TOKEN_FIELDS}
        calls = 0
        unreported = 0
        scanned = 0
        seen_models: set = set()

        for record in self.iter_records(days=days):
            scanned += 1
            # 模型清单**不受模型过滤影响** —— 否则下拉一旦选中某个模型，
            # 下拉里就只剩它一个，用户切不回去。
            seen_models.add(record.get("model") or "未知模型")
            if not _matches(record, model=model, thread_id=thread_id):
                continue
            calls += 1
            if not record.get("reported", True):
                unreported += 1
            day = _local_day(record.get("ts_ms"))
            tokens = {name: _clean_int(record.get(name)) for name in TOKEN_FIELDS}
            total = tokens["total_tokens"] or (tokens["prompt_tokens"] + tokens["completion_tokens"])
            model_name = record.get("model") or "未知模型"

            for name, value in tokens.items():
                totals[name] += value

            day_bucket = per_day.setdefault(
                day,
                {"date": day, "total_tokens": 0, "prompt_tokens": 0,
                 "completion_tokens": 0, "reasoning_tokens": 0, "calls": 0},
            )
            day_bucket["total_tokens"] += total
            day_bucket["calls"] += 1
            for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                day_bucket[name] += tokens[name]

            model_bucket = per_model.setdefault(
                model_name,
                {"model": model_name, "total_tokens": 0, "prompt_tokens": 0,
                 "completion_tokens": 0, "reasoning_tokens": 0, "calls": 0, "windows": set()},
            )
            model_bucket["total_tokens"] += total
            model_bucket["calls"] += 1
            for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                model_bucket[name] += tokens[name]
            model_bucket["windows"].add(record.get("thread_id") or "")

            key = record.get("thread_id") or ""
            win = per_window.setdefault(
                key,
                {
                    "thread_id": key,
                    "window_kind": record.get("window_kind") or window_kind_of(key),
                    "title": record.get("title") or "",
                    "first_ts_ms": record.get("ts_ms"),
                    "last_ts_ms": record.get("ts_ms"),
                    "turns": set(),
                    "calls": 0,
                    "total_tokens": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "models": {},
                },
            )
            # 标题快照取该窗口**最新**一条的 —— 改名后老记录还挂着旧名
            if _time_of(record.get("ts_ms")) >= _time_of(win["last_ts_ms"]):
                win["last_ts_ms"] = record.get("ts_ms")
                if record.get("title"):
                    win["title"] = record["title"]
            win["first_ts_ms"] = min(
                _time_of(win["first_ts_ms"]), _time_of(record.get("ts_ms"))
            )
            # 「轮次」= 一次问答（同一 trace 里的多次模型调用属于同一轮）
            if record.get("trace_id"):
                win["turns"].add(record["trace_id"])
            win["calls"] += 1
            win["total_tokens"] += total
            for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                win[name] += tokens[name]
            model_in_win = win["models"].get(model_name, 0) + total
            win["models"][model_name] = model_in_win

        windows = [
            {
                **{k: v for k, v in w.items() if k != "turns"},
                "turns": len(w["turns"]) or w["calls"],
                "models": [
                    {"model": m, "total_tokens": t}
                    for m, t in sorted(w["models"].items(), key=lambda kv: kv[1], reverse=True)
                ],
            }
            for w in per_window.values()
        ]
        windows.sort(key=lambda w: w["last_ts_ms"] or 0, reverse=True)

        return {
            "days": days,
            "totals": {
                **totals,
                "calls": calls,
                "windows": len(per_window),
                "unreported": unreported,
            },
            "models": sorted(seen_models),
            "per_day": _day_series(per_day, days),
            "per_model": [
                {**m, "windows": len(m["windows"])}
                for m in sorted(per_model.values(), key=lambda m: m["total_tokens"], reverse=True)
            ],
            "per_window": windows,
            "scanned": scanned,
        }

    def models(self, *, days: Optional[int] = None) -> List[str]:
        """账本里出现过的模型名（下拉过滤的数据源）"""
        seen = {
            (r.get("model") or "未知模型")
            for r in self.iter_records(days=days)
        }
        return sorted(seen)

    def stats(self) -> Dict[str, Any]:
        """账本存储状态（供 /api/usage/stats 与 /api/health 汇总）"""
        shards = self._shards()
        total_bytes = 0
        for path in shards:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
        today = datetime.now().strftime("%Y-%m-%d")
        today_path = self.directory / f"{_SHARD_PREFIX}{today}{_SHARD_SUFFIX}"
        return {
            "enabled": self.enabled,
            "directory": str(self.directory),
            "shards": len(shards),
            "total_bytes": total_bytes,
            "today_records": len(self._read_shard(today_path)) if today_path.exists() else 0,
            "appended_this_process": self._appended,
            "write_errors": self._errors,
            "immutable": True,
        }


# ---------- 过滤/口径小工具（模块级，便于单点改口径） ----------
def _matches(
    record: Dict[str, Any],
    *,
    model: Optional[str] = None,
    thread_id: Optional[str] = None,
    window_kind: Optional[str] = None,
) -> bool:
    if model and (record.get("model") or "未知模型") != model:
        return False
    if thread_id and record.get("thread_id") != thread_id:
        return False
    if window_kind and (record.get("window_kind") or window_kind_of(str(record.get("thread_id") or ""))) != window_kind:
        return False
    return True


def _time_of(ts_ms: Any) -> float:
    try:
        return float(ts_ms or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _local_day(ts_ms: Any) -> str:
    """epoch ms → 本机日期串（曲线的横轴口径：按用户所在时区的自然日，与分片一致）"""
    try:
        return datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return datetime.now().strftime("%Y-%m-%d")


def _day_series(per_day: Dict[str, Dict[str, Any]], days: Optional[int]) -> List[Dict[str, Any]]:
    """
    把曲线缺的自然日补成 0。

    只返回「有记录的日期」会让两段真实用量连成一根斜线 —— 用户读出的是
    「这些天也在跑，只是少一点」，而事实是一笔都没有。缺日的 0 必须显式给，
    前端无从区分「没数据」和「零消耗」。

    起点 = max(跨度起点, 账上第一笔)：所以「全部」等于「从第一笔到今天」，
    不会为一个刚建好的空账本画两千个空点。跨度超过 400 天时退回只给有记录的
    日期 —— 再长的跨度里，补零反而会淹没真实信号。
    """
    if not per_day:
        return []
    today = datetime.now().date()
    start = datetime.strptime(min(per_day), "%Y-%m-%d").date()
    if days:
        start = max(start, today - timedelta(days=int(days) - 1))
    span = (today - start).days + 1
    if span < 1 or span > 400:
        return [per_day[k] for k in sorted(per_day)]
    empty = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "reasoning_tokens": 0, "calls": 0}
    series: List[Dict[str, Any]] = []
    for offset in range(span):
        key = (start + timedelta(days=offset)).isoformat()
        row = per_day.get(key)
        series.append(dict(row) if row else {**empty, "date": key})
    return series


__all__ = [
    "UsageLedger",
    "DEFAULT_USAGE_DIR",
    "TOKEN_FIELDS",
    "PURPOSE_ANSWER",
    "PURPOSE_SUBAGENT",
    "WINDOW_LABELS",
    "window_kind_of",
]
