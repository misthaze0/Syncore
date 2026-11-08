"""
Trace 存储 —— JSONL 落盘 + 内存环形缓冲

★ 为什么是「JSONL 文件」而不是数据库

追踪数据的访问形态很单一：**按时间倒序看最近 N 条**、**按 trace_id 取一条**。
没有 join、没有事务、没有并发写压力（单机桌面应用）。
JSONL 恰好满足这三点，而且额外带来两个实际好处：

1. 崩溃不丢 —— 追加写、逐行完整，进程被 kill 也不会像 SQLite 那样留下半截事务；
2. 离线可查 —— `grep` / `jq` 直接可用，出问题时不必先起一个查询服务。

代价是没有索引、按字段过滤要全量扫。所以让**内存环形缓冲**承担「最近 N 条」的查询，
文件只作为持久化与追溯用途。数据量超过缓冲上限时，检索退化但不会报错。

★ 按天分文件

文件名 `traces-YYYY-MM-DD.jsonl`。按天滚动让「清理旧数据」变成删文件，
而不必写一个带条件的批量删除 —— 顺便也避免了单个文件无限增长。

★ 为什么 save() 是同步的

它由 `TraceRecorder.finish()` 触发，而 finish 会在**事件流的 finally 分支**里被调用 ——
包括客户端断连、任务被取消这两条路径。此时再 `create_task` 落盘，
任务可能随事件循环一起被取消，trace 就静默丢了。
一次追加写只有亚毫秒级开销，同步写完是最可靠的选择。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from app.observability.trace import Trace

logger = logging.getLogger(__name__)

#: 未显式配置时的默认落盘目录（相对 backend/）
DEFAULT_TRACE_DIR = "./data/traces"


class TraceStore:
    """Trace 的内存缓冲 + JSONL 落盘"""

    def __init__(
        self,
        directory: str | Path = DEFAULT_TRACE_DIR,
        *,
        max_memory: int = 500,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.directory = Path(directory)
        # maxlen 让写入自动淘汰最旧的 —— 不必手写 LRU
        self._memory: Deque[Dict[str, Any]] = deque(maxlen=max_memory)
        # 文件追加需要串行化：uvicorn 的多个请求处理任务会并发触发落盘
        self._lock = threading.Lock()
        self._written = 0
        self._save_errors = 0

    # ---------- 写入 ----------
    def save(self, trace: Trace) -> None:
        """落盘 + 进内存缓冲。任何异常都只记日志，绝不向上抛。"""
        if not self.enabled:
            return

        try:
            payload = trace.to_dict()
        except Exception as e:  # noqa: BLE001
            self._save_errors += 1
            logger.warning("Trace 序列化失败，已跳过: %s", e)
            return

        self._memory.append(payload)

        try:
            self._append_line(payload)
            self._written += 1
        except Exception as e:  # noqa: BLE001
            self._save_errors += 1
            logger.warning("Trace 落盘失败（内存中仍可查）: %s", e)

    def _append_line(self, payload: Dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path_for(payload.get("start_ms"))
        # ensure_ascii=False：中文问题、中文来源名要能直接肉眼读，否则全是 \uXXXX
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _path_for(self, start_ms: Any) -> Path:
        """按 trace 的开始时间决定落在哪一天的文件里（而不是按写入时间）"""
        try:
            ts = float(start_ms) / 1000.0
            day = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            day = datetime.now().strftime("%Y-%m-%d")
        return self.directory / f"traces-{day}.jsonl"

    # ---------- 查询 ----------
    def list(
        self,
        *,
        limit: int = 50,
        thread_id: Optional[str] = None,
        status: Optional[str] = None,
        intent: Optional[str] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        按时间倒序列出最近的 trace（读内存缓冲，不回扫文件）。

        缓冲只保留最近 `max_memory` 条 —— 这是刻意的取舍：
        历史追溯走文件（`grep`/`jq`），接口只服务「看最近发生了什么」。
        """
        items = list(self._memory)[::-1]  # deque 尾部最新 → 反转成倒序

        if thread_id:
            items = [t for t in items if t.get("thread_id") == thread_id]
        if status:
            items = [t for t in items if t.get("status") == status]
        if intent:
            items = [t for t in items if t.get("intent") == intent]

        return items[offset : offset + limit]

    def get(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """按 trace_id 取完整 trace（含全部 span）"""
        for trace in reversed(self._memory):
            if trace.get("trace_id") == trace_id:
                return trace
        return self._load_from_disk(trace_id)

    def _load_from_disk(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """内存里没有时回扫文件（最近 7 天的分片，足够覆盖 UI 的查看需求）"""
        if not self.directory.exists():
            return None
        for path in sorted(self.directory.glob("traces-*.jsonl"), reverse=True)[:7]:
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        if trace_id not in line:
                            continue
                        data = json.loads(line)
                        if data.get("trace_id") == trace_id:
                            return data
            except Exception as e:  # noqa: BLE001
                logger.warning("读取 trace 文件失败 %s: %s", path, e)
        return None

    # ---------- 维护 ----------
    def load_recent(self, limit: int = 100) -> int:
        """
        启动时把最近的 trace 回填进内存缓冲。

        不做这一步会出现很反直觉的现象：**重启服务后追踪面板空白**，
        而文件里明明有数据 —— 使用者会以为追踪根本没生效。
        """
        if not self.enabled or not self.directory.exists():
            return 0

        loaded = 0
        for path in sorted(self.directory.glob("traces-*.jsonl"), reverse=True):
            if loaded >= limit:
                break
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as e:  # noqa: BLE001
                logger.warning("读取 trace 文件失败 %s: %s", path, e)
                continue
            # 从文件尾部往前取，保证拿到的是最新的
            for line in reversed(lines):
                if loaded >= limit:
                    break
                try:
                    self._memory.appendleft(json.loads(line))
                    loaded += 1
                except json.JSONDecodeError:
                    continue
        return loaded

    def clear(self) -> int:
        """清空内存缓冲（不动磁盘文件），返回清掉的条数"""
        count = len(self._memory)
        self._memory.clear()
        return count

    def cleanup_old_files(self, retention_days: int) -> int:
        """
        删除超过保留期的日分片文件，返回删除的文件数。

        ★ 只删「严格匹配 `traces-YYYY-MM-DD.jsonl` 且日期合法」的文件 ——
        目录是排查证据的存放地，宁可漏删也不能碰任何命名不认识的东西。
        日期解析失败的分片**保留不动**（它的来源未知，删错的代价不可逆）。

        与 `load_recent` 的顺序：清理在前。这样回填不会去读马上要删的文件，
        也让「保留期收紧后重启」立即生效，而不是等下一次启动。
        """
        if retention_days <= 0 or not self.directory.exists():
            return 0

        cutoff = (datetime.now() - timedelta(days=retention_days)).date()
        removed = 0
        for path in self.directory.iterdir():
            day = self._parse_day(path.name)
            if day is None or day >= cutoff:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as e:
                # 单个文件删不掉不中断整轮清理，但必须留下痕迹
                logger.warning("清理过期 trace 文件失败 %s: %s", path, e)
        if removed:
            logger.info("已清理 %s 个超过 %s 天的 trace 分片", removed, retention_days)
        return removed

    @staticmethod
    def _parse_day(name: str) -> Optional[date]:
        """从文件名解析日期；不匹配严格模式（含日期非法）返回 None"""
        if not (name.startswith("traces-") and name.endswith(".jsonl")):
            return None
        try:
            return datetime.strptime(name[len("traces-") : -len(".jsonl")], "%Y-%m-%d").date()
        except ValueError:
            return None

    def stats(self) -> Dict[str, Any]:
        today = datetime.now().strftime("%Y-%m-%d")
        today_path = self.directory / f"traces-{today}.jsonl"
        return {
            "enabled": self.enabled,
            "directory": str(self.directory),
            "in_memory": len(self._memory),
            "written": self._written,
            "save_errors": self._save_errors,
            "today_file": str(today_path) if today_path.exists() else None,
            "today_bytes": today_path.stat().st_size if today_path.exists() else 0,
        }


__all__ = ["TraceStore", "DEFAULT_TRACE_DIR"]
