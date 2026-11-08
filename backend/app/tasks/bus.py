"""
进程内任务事件总线 —— 子任务进度的**实时推送通道**（路径 B）。

★ 它解决什么

上一轮把「子任务进度实时推送」判定为「做不到」，理由是子任务跨轮次存活、
主对话 SSE 一轮就结束，两者生命周期不匹配。**前半句对，后半句错**：
不匹配是真的，但解法不是「放弃实时推送」，而是**给进度单开一条长连接**
（见 `app/api/tasks.py` 的 `GET /api/tasks/stream`）——
连接由前端按会话持有，与主对话 SSE 各走各的，互不干扰。

★ 为什么是进程内，而不是 Redis Pub/Sub

Redis Pub/Sub 解决的是「**多实例扇出**」：N 个 API 进程各持有一些 SSE 连接，
一个 worker 发出的事件要让「恰好持有该连接的那个进程」收到，才需要跨进程中转。

本项目是**单机单进程**（同一个 uvicorn 进程既跑 Supervisor 又跑子任务），
所以一个进程内的订阅表就够了。真要上多实例（ADR 0019 的演进路径）时，
把这里的 `publish` / `subscribe` 换成 Redis Pub/Sub 即可 —— **调用方一行不用改**，
这正是「B 是 A 的地基」的具体含义。

★ 背压：慢消费者绝不阻塞子任务

订阅队列是**有界**的。满时 `publish` **丢弃最旧的一条**而不是阻塞 ——
`publish` 跑在子任务的 emit 闭包（执行路径）上，堵住它就等于拖慢子任务本身。
丢事件是安全的：前端连接建立时先收**快照**（真相源是任务表），
丢几条增量不会让状态错，只是那几步进度要等下次快照/轮询补齐。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)

#: 单个订阅者队列的上限。满了丢最旧 —— 见模块注释的「背压」一节。
#: 256 足够容纳一个任务的全部进度（实测工具循环几十步），
#: 又不至于让一个卡死的订阅者吃光内存。
_QUEUE_MAXSIZE = 256


class TaskEventBus:
    """
    进程内任务事件广播（按 `thread_id` 分频道）。

    ★ 为什么频道是 `thread_id` 而不是 `task_id`
    前端订阅的是「**我这个会话**的后台任务」。一个新派发的任务也要能被立刻收到，
    而那时前端**还不知道它的 task_id** —— 按 task_id 订阅的话，
    「派发」这条事件永远送不到。按 thread_id 订阅，同会话内所有任务的事件都推给它。
    """

    def __init__(self) -> None:
        self._channels: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, thread_id: str) -> asyncio.Queue:
        """订阅一个会话的进度事件，返回该订阅者的队列"""
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        async with self._lock:
            self._channels.setdefault(thread_id, set()).add(q)
        return q

    async def unsubscribe(self, thread_id: str, q: asyncio.Queue) -> None:
        """退订（订阅者断开时调用 —— 漏了会泄漏队列）"""
        async with self._lock:
            subs = self._channels.get(thread_id)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                self._channels.pop(thread_id, None)

    def publish(self, thread_id: str, event: Dict[str, Any]) -> None:
        """
        广播一条事件（**同步、非阻塞**）。

        ★ 为什么同步
        调用点在子任务的 emit 闭包（执行路径）上。发事件是**旁路**，
        绝不能因为某个订阅者慢而拖慢子任务 —— 那会把「观测」变成「依赖」
        （不变量 18 的反面）。所以 `put_nowait`，满了就丢最旧。

        ★ 单线程无锁是安全的
        本方法不 `await`，而 asyncio 是单线程 —— 执行期间不会有别的协程
        改 `_channels`，所以迭代 `set` 不会撞上「迭代中被修改」。
        """
        if not thread_id:
            return
        subs = self._channels.get(thread_id)
        if not subs:
            return
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 丢最旧的一条再放新的：进度是「最新最重要」，
                # 且前端有快照兜底。
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001
                    pass

    def channels(self) -> List[str]:
        """当前有订阅者的会话 id（诊断用）"""
        return list(self._channels.keys())

    def reset(self) -> None:
        """
        清空所有订阅（**只在服务关停 / 测试**里调用）。

        与 `approval_gate.reset` 同理：留着旧订阅会让重启后的第一个事件
        白推给一批已经没人读的队列。生产关停时必须清 ——
        否则订阅队列随进程泄漏（虽然进程结束也会释放，但显式清更干净）。
        """
        self._channels.clear()


#: 进程级单例（与 `task_store` / `approval_broker` 同理：单进程部署）
task_bus = TaskEventBus()


__all__ = ["TaskEventBus", "task_bus"]
