# -*- coding: utf-8 -*-
"""
定时任务的调度循环 —— 照抄 `rag/watcher.py` 的四条纪律，一条不多一条不少：

1. **先 sleep 再干活**：启动时把 CPU/网络让给 lifespan 的其他初始化；
2. **每轮单独 try/except，绝不 raise**：asyncio 里没人 await 的异常
   只会留一行日志，而循环静默终止 —— 之后看起来还在跑，实际再也不触发；
3. **连续失败次数与最近错误暴露到状态接口**：让「它坏了」是可见的；
4. **CancelledError 记完就 re-raise**：关闭流程的正常路径，不吞。

★ 为什么是「扫表 + 比对时间戳」而不是把每个任务算好 timer 挂上去

挂 timer 的版本要在每次编辑/停用/删除时精确撤换对应的 `loop.call_later`，
漏撤一处就是「用户明明停掉了它，到点还是烧了一次 token」。
扫表版本里内存没有任何调度状态，`next_run_at` 落盘即真相 ——
编辑、停用、重启、reload 双进程窗口，全都退化成同一句判断：
`enabled && next_run_at <= now`。单用户桌面场景几十个任务，
每 30 秒扫一遍的代价是零。

★ 错过就错过（`SCHEDULED_CATCHUP_MINUTES`）

进程睡了/关了 N 小时，醒来面对一堆「过期」的触发点。默认只补跑
窗口内错过的最近一次，更早的直接推进到下一班 ——
自动重放一条「今早 8 点该生成的日报」到下午 3 点，产出的是过期内容
加一份白烧的 token。宁可少跑，不可瞎跑。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_consecutive_failures = 0
_last_error: Optional[str] = None
_dispatched = 0


def _parse_iso(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


async def _tick() -> None:
    """一轮扫描：把到点且可触发的任务逐个派发（派发本身立即返回）。"""
    global _dispatched
    from app.schedules import runner
    from app.schedules.store import schedule_store

    now = datetime.now().astimezone()
    catchup = timedelta(minutes=max(0, int(getattr(settings, "SCHEDULED_CATCHUP_MINUTES", 30))))
    for schedule in schedule_store.list(enabled=True):
        due = _parse_iso(str(schedule.get("next_run_at") or ""))
        if due is None or due > now:
            continue
        if now - due > catchup:
            # 过期太久：不补跑，只推进。不推进的话它会每个 tick 都撞进这个分支，
            # 日志刷成噪音，而且永远停在「看起来马上要跑」的状态。
            logger.info("定时任务错过执行窗口（超过补跑上限），推进到下一班: %s due=%s",
                        schedule.get("schedule_id"), due.isoformat(timespec="minutes"))
            schedule_store.advance_next_run(
                str(schedule.get("schedule_id")),
                runner._advance_next_run(schedule),
            )
            continue
        run = await runner.dispatch(schedule, trigger="scheduled")
        if run is not None:
            _dispatched += 1
            logger.info("定时任务已触发: %s → run=%s", schedule.get("title") or schedule.get("schedule_id"),
                        run.get("run_id"))


async def _loop(interval: float) -> None:
    global _consecutive_failures, _last_error
    while True:
        try:
            await asyncio.sleep(interval)
            await _tick()
            _consecutive_failures = 0
            _last_error = None
        except asyncio.CancelledError:
            logger.info("定时任务调度循环已停止")
            raise
        except Exception as e:  # noqa: BLE001
            _consecutive_failures += 1
            _last_error = str(e)
            logger.warning(
                "定时任务扫描失败（连续第 %s 次，将在下个周期重试）: %s",
                _consecutive_failures, e,
            )


async def start_scheduler() -> None:
    """按配置启动调度循环（lifespan 调用，幂等）。"""
    global _task
    if not settings.SCHEDULED_ENABLED:
        logger.info("定时任务：调度未启用（SCHEDULED_ENABLED=false）——接口与手动「立即运行」仍可用")
        return
    if _task is not None and not _task.done():
        return
    interval = max(5.0, float(settings.SCHEDULED_TICK_SECONDS))
    _task = asyncio.create_task(_loop(interval), name="xjms-scheduler")
    logger.info("✓ 定时任务调度循环已启动 | 间隔=%.0fs", interval)


async def stop_scheduler() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None


def scheduler_status() -> Dict[str, Any]:
    """调度器状态（供 GET /api/schedules/status 展示「它坏了」）。"""
    running = _task is not None and not _task.done()
    out: Dict[str, Any] = {
        "enabled": bool(settings.SCHEDULED_ENABLED),
        "running": running,
        "dispatched": _dispatched,
        "consecutive_failures": _consecutive_failures,
    }
    if _last_error:
        out["last_error"] = _last_error
    return out
