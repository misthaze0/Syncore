"""
增量索引的后台监听 —— 周期性触发 `incremental.scan_and_apply()`

★ 为什么默认关闭（`INDEX_WATCH_ENABLED=false`）

它会**主动产生成本**：目录里一旦有变更，后台就会调 Embedding 重新入库。
本项目里同类开关（`LANGFUSE_ENABLED`、答案层评估）一律 opt-in ——
「替用户做决定」和「替用户花钱」是两件事，后者必须显式同意。

关掉并不等于功能不可用：
- `POST /api/index/scan` 可随时手动触发一次
- `python scripts/watch_knowledge.py --once` 适合放进 cron / CI
- `python scripts/watch_knowledge.py --loop` 适合不想开后端但想跑监听时

★ 后台任务的异常必须自己接住

`asyncio` 里没人 await 的异常只会打一行 "Task exception was never retrieved"，
而**循环本身会静默终止** —— 之后监听看起来还在（进程没挂、状态接口也正常），
实际上再也不会扫描了。所以每一轮都单独 try/except，
并把连续失败次数暴露到状态接口里，让「它坏了」这件事可见。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from app.config import settings
from app.rag import incremental

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_consecutive_failures = 0
_last_error: str | None = None
_scans_done = 0


async def _loop(interval: float) -> None:
    """周期扫描循环。

    先 sleep 再扫描：启动时把 CPU/网络让给 lifespan 里的其他初始化
    （Milvus 建表、ES 建索引），也避免「刚启动就发现所有文件都没索引过」
    时和用户的第一批请求抢 Embedding 网关。
    """
    global _consecutive_failures, _last_error, _scans_done
    while True:
        try:
            await asyncio.sleep(interval)
            report = await incremental.scan_and_apply()
            if report.get("busy"):
                continue
            _scans_done += 1
            if report.get("ok"):
                _consecutive_failures = 0
                _last_error = None
            else:
                _consecutive_failures += 1
                failures = report.get("failed") or []
                warnings = report.get("warnings") or []
                _last_error = (failures[0].get("error") if failures else warnings[0] if warnings else "扫描未完整完成")
            if report.get("has_changes"):
                logger.info(
                    "增量索引完成：新增 %s / 重建 %s / 删除 %s，共写入 %s 块（%sms）",
                    len(report.get("added") or []),
                    len(report.get("modified") or []),
                    len(report.get("deleted") or []),
                    report.get("indexed_chunks", 0),
                    report.get("duration_ms"),
                )
        except asyncio.CancelledError:
            # 关闭流程会 cancel 这个任务，属于正常路径，不要吞
            logger.info("增量索引监听已停止")
            raise
        except Exception as e:  # noqa: BLE001
            _consecutive_failures += 1
            _last_error = str(e)
            # 不 raise：循环必须活下去，否则一次网络抖动就永久停摆
            logger.warning(
                "增量索引扫描失败（连续第 %s 次，将在下个周期重试）: %s",
                _consecutive_failures,
                e,
            )


async def start_watcher() -> None:
    """按配置启动后台监听（应用 lifespan 调用，幂等）"""
    global _task
    if not settings.INDEX_WATCH_ENABLED:
        logger.info(
            "增量索引：后台监听未启用（INDEX_WATCH_ENABLED=false）——"
            "需要时用 POST /api/index/scan 手动触发"
        )
        return
    if _task is not None and not _task.done():
        return

    interval = max(5.0, float(settings.INDEX_WATCH_INTERVAL_SECONDS))
    _task = asyncio.create_task(_loop(interval), name="xjms-index-watcher")
    logger.info(
        "✓ 增量索引监听已启动 | 目录=%s | 间隔=%.0fs",
        [str(p) for p in settings.index_watch_dirs_list],
        interval,
    )


async def stop_watcher() -> None:
    """停止后台监听（应用关闭时调用）"""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None


def watcher_status() -> Dict[str, Any]:
    """监听运行状态（供 /api/index/status 与健康检查展示）"""
    running = _task is not None and not _task.done()
    return {
        "running": running,
        "scans_done": _scans_done,
        "consecutive_failures": _consecutive_failures,
        "last_error": _last_error,
    }


__all__ = ["start_watcher", "stop_watcher", "watcher_status"]
