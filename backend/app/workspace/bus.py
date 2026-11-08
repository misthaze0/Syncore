"""
进程内群组事件总线 —— 群组消息的实时推送通道。

★ 与 `tasks/bus.py::TaskEventBus` 的关系

完全同构，只是频道键从 `thread_id` 换成 `group_id`：
- 单机单进程，进程内订阅表够用（多实例再换 Redis Pub/Sub，调用方不用改）；
- 背压：订阅队列有界，满了丢最旧（群组消息有快照兜底）；
- `publish` 同步非阻塞 —— 它跑在 orchestrator / 自动汇报 hook 的执行路径上。

★ 为什么频道是 `group_id` 而不是某次编排的 run_id

前端订阅的是「**这个群**的消息流」。一次编排里的系统消息、成员汇报、结论
都要能推给同一个订阅者 —— 按 run_id 订阅的话，前端还得先知道 run_id。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Set

#: 单个订阅者队列上限（与 task_bus 同口径）。
_QUEUE_MAXSIZE = 256


class GroupEventBus:
    """进程内群组消息广播（按 `group_id` 分频道）。"""

    def __init__(self) -> None:
        self._channels: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, group_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        async with self._lock:
            self._channels.setdefault(group_id, set()).add(q)
        return q

    async def unsubscribe(self, group_id: str, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._channels.get(group_id)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                self._channels.pop(group_id, None)

    def publish(self, group_id: str, event: Dict[str, Any]) -> None:
        if not group_id:
            return
        subs = self._channels.get(group_id)
        if not subs:
            return
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001
                    pass

    def reset(self) -> None:
        self._channels.clear()


#: 进程级单例
group_bus = GroupEventBus()


__all__ = ["GroupEventBus", "group_bus"]
