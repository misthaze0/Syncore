"""
跨子图的审批排队闸门（Supervisor 架构，见 ADR 0019）。

★ 这个问题是怎么来的

不变量 40 原本写着：「同一轮的多个危险调用是**前后相继**的 interrupt，
**从不同时挂起**」。这个结论建立在不变量 6 上 ——
「一次 superstep 只消费一个工具调用」，所以模型一次要调 5 次危险工具时，
它们变成 5 次前后相继的挂起。

**Supervisor 打破了这个前提**：子图之间是**并行**的，
3 个子 Agent 可以在同一墙钟时刻各自挂在 `interrupt()` 上。

而前端的 `pendingApproval` 在 store 里是**单个对象**
（`frontend/src/stores/agentStore.ts:282-300`）——
后一个会**静默覆盖**前一个，用户的第一次点击批的是哪个任务，
只有鬼知道。而且界面上只会看到一���弹窗，另一个任务就这么干等着，
用户完全不知道自己还有一个操作在等批准。

所以必须在架构上把审批**串行化**：同一时刻全局只暴露一个待审批。

★ 为什么是显式 FIFO 队列而不是 `asyncio.Lock`

Lock 不保证公平：谁先拿到不可预测，出了问题也**不可测**。
显式队列让「谁在等、排在第几位」成为**可查询的事实**（`list_waiters` 暴露给
`/api/approval/pending` 与 `/api/tasks/stats`），顺序也能被单测钉住。
一个死锁的闸门必须能被诊断 —— 它是唯一「不报错但功能全停」的故障。

★ 死锁防护：三条

1. `release` 必须在调用方的 `finally` 里（代码结构上不可能漏）。
2. `release` 幂等：非 holder 调用直接 return，
   重复 release 不会把队列放乱（否则会把两个任务同时放进闸门）。
3. 审批**超时**由 `approval_broker.wait_for_decision` 兜底
   （`APPROVAL_TIMEOUT_SECONDS`），所以等待不会无限持续。

⚠️ 但「三条都对」仍不足以让人放心 —— 所以有一条专门的门禁测试：
三个子任务依次挂 danger，**第二个故意超时**，断言第三个仍能进。
这条测试专门盯「异常/超时路径上的 release」这个最容易漏的位置。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


class ApprovalGate:
    """
    跨子图的审批排队闸门（进程内 FIFO）。

    ★ 单进程假设
    与 `ApprovalBroker` 同源（它的模块注释里明写「单进程部署」）：
    队列在内存里，多副本部署会让每个副本各有一份队列，
    于是「同时只有一个人在等审批」这个保证会失效。
    真要横向扩展，得把这个队列换成 Redis 列表。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        #: 等着的任务（task_id → 唤醒事件）。deque 保证 FIFO。
        self._waiters: deque[tuple[str, asyncio.Event]] = deque()
        #: 当前持有闸门的 task_id（None = 无人持有）
        self._holder: str | None = None

    # ---------- 对外 ----------

    async def wait_turn(self, task_id: str) -> None:
        """
        排队等审批（**不**持有闸门时立即返回）。

        ⚠️ 这个函数在 `ApprovalGate` 内部不记日志 ——
        「等了很久」这件事由 `wait_queued_seconds` 报出去，
        否则正常的排队会在日志里变成噪音，真卡住时反而被淹没。
        """
        ev = asyncio.Event()
        async with self._lock:
            if self._holder is None and not self._waiters:
                # ★ 快路径：无人持有且无人排队 —— 直接进，不排自己
                self._holder = task_id
                ev.set()
            else:
                self._waiters.append((task_id, ev))
        try:
            await ev.wait()
        except asyncio.CancelledError:
            async with self._lock:
                self._waiters = deque((tid, event) for tid, event in self._waiters if event is not ev)
                if self._holder == task_id:
                    self._holder = None
                    if self._waiters:
                        nxt, event = self._waiters.popleft()
                        self._holder = nxt
                        event.set()
            raise

    async def release(self, task_id: str) -> None:
        """
        释放闸门（**必须**在 finally 里调用）。

        幂等：不是 holder 就直接 return。
        为什么必须幂等 —— 重复 release 会把队列放乱：
        正常路径 release 一次、异常路径的 finally 又 release 一次，
        第二次如果把队列推进一格，就会有**两个**任务同时以为自己持有闸门，
        于是又回到「同时弹两个窗」的原点。
        """
        async with self._lock:
            if self._holder != task_id:
                return
            self._holder = None
            if self._waiters:
                nxt, ev = self._waiters.popleft()
                self._holder = nxt
                ev.set()

    def list_waiters(self) -> list[dict[str, Any]]:
        """
        谁在等、排第几位（供 `/api/approval/pending` 与 `/api/tasks/stats` 展示）。

        ★ 必须可查
        闸门卡死是这个系统里唯一「界面完全看不出来」的故障：
        没有报错、任务状态是 `waiting_approval`、审批接口返回 `matched=false`，
        用户只看到「点了没反应」。不变量 23 的精神：元数据说错比没有元数据更糟，
        但「元数据缺失」同样让人无从下手。
        """
        return [
            {
                "task_id": tid,
                "position": i + 1,
                "waiting": True,
            }
            for i, (tid, _ev) in enumerate(self._waiters)
        ]

    def snapshot(self) -> dict[str, Any]:
        """闸门整体状态（运维用）"""
        return {
            "available": True,
            "holder": self._holder,
            "waiting": len(self._waiters),
            "waiters": self.list_waiters(),
        }

    async def reset(self) -> None:
        """
        重置闸门（**只在服务启动时**调用）。

        用途：进程被强杀时可能有任务卡在 `waiting_approval`，
        而队列本身随内存一起没了 ——
        但如果闸门没被重置（新进程里它是干净的，本来就没事），
        真正的问题是**任务状态**的结算，那件事由
        `TaskStore.sweep_orphans` 负责，不归这里。

        所以这个方法只做一件防御性的事：确保内部状态自洽。
        保留它是因为「万一有人手工调过 _waiters」时有一条干净的复位路径，
        而直接改私有属性去复位会让那条路径没人敢用。
        """
        async with self._lock:
            self._holder = None
            self._waiters.clear()

    @property
    def timeout_seconds(self) -> float:
        """审批等待的超时（与 ApprovalBroker 用同一个配置）"""
        return float(getattr(settings, "APPROVAL_TIMEOUT_SECONDS", 120.0))


#: 进程级单例（与 `approval_broker` 同理：单进程部署）
approval_gate = ApprovalGate()


__all__ = ["ApprovalGate", "approval_gate"]
