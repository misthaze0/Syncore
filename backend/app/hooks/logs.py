"""
钩子执行留痕 —— append-only JSONL

★ 为什么要单独留一份

钩子是用户写的脚本，跑在对话的关键路径上。它出问题时的症状是
「她今天反应好慢」或「这个操作莫名其妙被拦了」，而界面什么都不会说。
没有留痕，用户只能猜；有了留痕，「退出码 2、耗时 4000ms、stderr 第一行是 X」
就是能直接读出来的事实。

★ 与审批留痕（`approval/records.py`）的取舍相同：
文件是唯一事实来源，不依赖 Redis；读接口只取尾部若干行，
所以文件长到几万行也不会把接口拖慢。清理不在这里做 ——
钩子日志是排障材料，删它等于销毁现场，需要时由用户自己删文件。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List

from app.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
#: 内存里给接口用的尾部（重启即空，历史走文件）
_recent: Deque[Dict[str, Any]] = deque(maxlen=500)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def append_run(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    记一次钩子执行。

    失败一律吞掉：留痕是**旁路**，它写不下去不该让工具执行变成失败
    （与 `approval_flow.backfill_execution_result` 同一条原则）。
    """
    row = {"logged_at": _now_iso(), **entry}
    try:
        line = json.dumps(row, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        logger.debug("钩子留痕序列化失败（已忽略）: %s", e)
        return row
    with _lock:
        _recent.appendleft(row)
        try:
            with settings.hooks_run_log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as e:
            logger.debug("钩子留痕写入失败（已忽略）: %s", e)
    return row


def tail(limit: int = 50) -> List[Dict[str, Any]]:
    """最近 N 条（新的在前）。内存为空时回读文件尾部。"""
    with _lock:
        if _recent:
            return list(_recent)[:limit]
    return _read_tail(limit)


def _read_tail(limit: int) -> List[Dict[str, Any]]:
    path = settings.hooks_run_log_path
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.debug("钩子留痕读取失败: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    for line in reversed(lines[-max(limit, 1) * 4 :]):  # 留点余量：坏行会被跳过
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
        if len(out) >= limit:
            break
    return out


def reset() -> None:
    with _lock:
        _recent.clear()


__all__ = ["append_run", "reset", "tail"]
