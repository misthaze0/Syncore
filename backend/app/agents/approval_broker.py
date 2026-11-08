"""
HITL 审批协调器 (Phase 2)

解决的问题:
危险工具执行前需要暂停 Graph，等前端弹窗拿到用户决策后再继续。
SSE 连接必须保持打开，因此审批等待不能靠「断开重连」实现。

方案:
Graph 的 tool_executor 节点调用 `interrupt()` 挂起后，
SSE 驱动层先 `register()` 占位、再推送 interrupt 事件、最后 `await wait()`；
用户点击「批准/拒绝」触发 `POST /api/approval`，由接口调用 `resolve()` 唤醒。

两处竞态防护（都用测试锁住了）:
1. **先 register 再 yield 事件**：否则前端即便「秒回」审批，也会因为等待者尚未就绪而丢单
2. **`_results` 暂存**：允许 resolve 早于 wait 到达

设计取舍:
- `register()` 不创建 Future，只打标记 —— 不依赖 event loop，也避免 Future 被绑定到
  与真正 await 时不同的 loop 上；Future 统一在 `wait()` 中按需创建。
- `wait()` 里必须先判断「Future 是否已 done 并直接取结果」，
  否则已 resolve 的 Future 会走进「新建 Future 空等」的死路。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

Key = tuple[str, str, str, str]

#: 决策来源（与 `app.approval.records.BY_*` 对齐，但不 import 它 ——
#: broker 是 Phase 2 的纯内存协调器，不该依赖 Phase 6 的留痕模块）。
REASON_USER = "user"
REASON_TIMEOUT = "timeout"


@dataclass
class ApprovalOutcome:
    """
    一次等待的结果。

    ★ 为什么需要它，而不是只返回 `bool`

    对**执行**而言「用户拒绝」与「等待超时」是一回事（都拒绝执行），
    所以 `wait()` 返回 bool 就够了。但对**留痕**来说两者完全不同：
    超时意味着"没人管，请求卡住了"，拒绝意味着"人看了并明确说不"，
    这两种记录对应完全不同的运维动作（一个是加人/加提醒，一个是查为什么会被拒）。

    折叠成一个 `False` 之后就无法区分了 —— 而这正是审批留痕存在的意义。
    """

    approved: bool = False
    reason: str = REASON_USER

    @property
    def timed_out(self) -> bool:
        return self.reason == REASON_TIMEOUT


class ApprovalBroker:
    """
    按 `(thread_id, call_id, run_id, employee_id)` 维度协调审批。

    ★ 后两维是护栏 6「审批不可委托」的落点（施工图 §5.7）

    改之前 key 只有前两项，而**工作空间里 `thread_id` 就是群组的 id**
    （`orchestrator` 派发时传 `thread_id=group_id`）。于是三个员工同时在飞时：

    - `call_id` 是模型返回的短 id（`call_ab12…`），并发下**会**撞上 ——
      撞上的症状不是报错，是「甲点了批准，跑起来的却是乙的 `delete`」。
      这是本项目里唯一一处「A 的操作由 B 的同意放行」，正是护栏 6 要禁的事。
    - 就算 id 不撞，三条弹窗也长得一模一样，人类无从判断自己在批谁的。

    所以 key 必须带上「哪一次协作、哪个员工」。两维缺省时为空串 ——
    主对话（非工作空间）本来就没有这两个概念，空串之间仍能互相匹配，
    这条改动因此是**附加式**的：老路径一个行为都没变。
    """

    def __init__(self) -> None:
        self._registered: set[Key] = set()               # 已占位（等待方即将就绪）
        self._futures: dict[Key, asyncio.Future] = {}    # 正在等待的唤醒句柄
        self._results: dict[Key, bool] = {}              # 先于 wait 到达的审批结果
        self._payloads: dict[Key, dict[str, Any]] = {}   # 审批内容（供观测）

    @staticmethod
    def _key(
        thread_id: str, call_id: str, run_id: str = "", employee_id: str = ""
    ) -> Key:
        return (thread_id, call_id, run_id, employee_id)

    # ---------- 等待方（SSE 驱动层） ----------
    def register(
        self,
        thread_id: str,
        call_id: str,
        payload: dict[str, Any] | None = None,
        *,
        run_id: str = "",
        employee_id: str = "",
    ) -> None:
        """
        在推送 interrupt 事件之前占位，宣告「这个审批即将有人等待」。

        不创建 Future —— 不依赖 event loop，可在任意上下文调用。
        """
        key = self._key(thread_id, call_id, run_id, employee_id)
        self._registered.add(key)
        if payload:
            self._payloads[key] = payload

    async def wait(
        self,
        thread_id: str,
        call_id: str,
        timeout: int | None = None,
        *,
        run_id: str = "",
        employee_id: str = "",
    ) -> bool:
        """
        阻塞等待用户审批结果。

        Args:
            timeout: 等待秒数；传 None 使用配置默认值，传 0 表示立即超时（不等待）

        Returns:
            True=批准执行；False=拒绝（含超时）

        ⚠️ 拒绝与超时在这里都返回 False（对执行而言确实是同一件事）。
        需要区分两者时用 `wait_for_decision()`。
        """
        outcome = await self.wait_for_decision(
            thread_id, call_id, timeout, run_id=run_id, employee_id=employee_id
        )
        return outcome.approved

    async def wait_for_decision(
        self,
        thread_id: str,
        call_id: str,
        timeout: int | None = None,
        *,
        run_id: str = "",
        employee_id: str = "",
    ) -> ApprovalOutcome:
        """
        与 `wait()` 行为完全一致，但额外返回**为什么**是这个结果。

        留痕需要它（见 `ApprovalOutcome` 的说明），链路执行不需要。
        """
        key = self._key(thread_id, call_id, run_id, employee_id)

        # 1) 审批结果先于等待到达（前端「秒回」）
        if key in self._results:
            result = self._results.pop(key)
            self._cleanup(key)
            return ApprovalOutcome(approved=result, reason=REASON_USER)

        loop = asyncio.get_running_loop()
        future = self._futures.get(key)
        # 暂停/取消可能发生在 register 与 wait 之间，不得重新创建等待槽位。
        if future is None and key not in self._registered:
            return ApprovalOutcome(approved=False, reason=REASON_USER)

        # 2) Future 已就绪：直接取结果，切勿新建 Future 空等
        if future is not None and future.done():
            result = bool(future.result())
            self._cleanup(key)
            return ApprovalOutcome(approved=result, reason=REASON_USER)

        # 3) 按需创建，并确保绑定在当前运行中的 loop 上
        if future is None or future.get_loop() is not loop:
            future = loop.create_future()
            self._futures[key] = future

        # 注意：必须用 `is None` 判断。写成 `timeout or 默认值` 会把调用方
        # 显式传入的 0（表示「不等待」）静默改成 300 秒。
        if timeout is None:
            timeout = settings.APPROVAL_TIMEOUT_SECONDS

        try:
            approved = bool(await asyncio.wait_for(future, timeout=timeout))
            return ApprovalOutcome(approved=approved, reason=REASON_USER)
        except asyncio.TimeoutError:
            logger.warning(
                "审批超时（%ss），自动拒绝: thread=%s call=%s run=%s emp=%s",
                timeout, thread_id, call_id, run_id, employee_id,
            )
            return ApprovalOutcome(approved=False, reason=REASON_TIMEOUT)
        finally:
            self._cleanup(key)

    def _cleanup(self, key: Key) -> None:
        self._registered.discard(key)
        self._futures.pop(key, None)
        self._payloads.pop(key, None)
        self._results.pop(key, None)

    # ---------- 响应方（/api/approval 接口） ----------
    def resolve(
        self,
        thread_id: str,
        call_id: str,
        approved: bool,
        *,
        run_id: str = "",
        employee_id: str = "",
    ) -> bool:
        """
        提交审批结果。

        Returns:
            True  = 审批被受理（唤醒了等待者，或已占位待等待者取用）
            False = 没有对应的等待或占位，属于超时后的迟到请求 / 伪造请求，直接忽略

        ★ 四元组里少给一维就是 `False`（不是「宽松匹配」）
        这里刻意不做前缀匹配或「取最近一条」：那种便利会把「甲的同意」送到
        乙的请求上（护栏 6）。宁可让前端拿不到匹配、弹窗报「该审批已过期」，
        也不能让一次批准放行另一个人的危险操作。
        """
        key = self._key(thread_id, call_id, run_id, employee_id)
        future = self._futures.get(key)

        # 一次请求只接收第一个决策，秒回暂存与 Future 完成两条路都一样。
        if key in self._results or (future is not None and future.done()):
            return False

        if future is not None and not future.done():
            loop = future.get_loop()
            if loop.is_running():
                future.set_result(approved)
                logger.info(
                    "审批已交付: call=%s emp=%s approved=%s", call_id, employee_id, approved
                )
                return True
            # loop 已停止：退化为暂存，避免 set_result 静默失效
            logger.warning("等待中的 loop 已停止，审批结果转为暂存: call=%s", call_id)

        # 既没有等待中的 Future，也没有 register 占位 → 不予受理。
        # 这里不能「先暂存着」：否则一个过期的审批请求可能在很久之后
        # 被另一个请求消费掉（陈旧审批串台）。
        if future is None and key not in self._registered:
            logger.warning(
                "审批无对应等待者，已忽略: thread=%s call=%s run=%s emp=%s",
                thread_id, call_id, run_id, employee_id,
            )
            return False

        self._results[key] = approved
        logger.info("审批结果已暂存，等待方就绪后取用: call=%s approved=%s", call_id, approved)
        return True

    # ---------- 观测 ----------
    def discard(self, thread_id: str, call_id: str, *, run_id: str = "", employee_id: str = "") -> None:
        key = self._key(thread_id, call_id, run_id, employee_id)
        future = self._futures.get(key)
        if future is not None and not future.done():
            future.cancel()
        self._cleanup(key)

    def pending_payload(self, thread_id: str, call_id: str, *, run_id: str = "",
                        employee_id: str = "") -> dict[str, Any] | None:
        key = self._key(thread_id, call_id, run_id, employee_id)
        future = self._futures.get(key)
        if key in self._results or (future is not None and future.done()):
            return None
        payload = self._payloads.get(key)
        return dict(payload) if payload is not None else None

    def list_pending(self) -> list[dict[str, Any]]:
        """列出所有等待中的审批（供调试/运维接口使用）"""
        keys = set(self._registered) | set(self._futures) | set(self._results)
        return [
            {
                "thread_id": tid,
                "call_id": cid,
                "run_id": rid,
                "employee_id": eid,
                "staged": (tid, cid, rid, eid) in self._results,
                **self._payloads.get((tid, cid, rid, eid), {}),
            }
            for (tid, cid, rid, eid) in keys
        ]

    def cancel_all(self) -> None:
        """取消全部等待（应用关闭时调用）"""
        for future in self._futures.values():
            if not future.done():
                future.set_result(False)
        self._futures.clear()
        self._registered.clear()
        self._results.clear()
        self._payloads.clear()


# 全局单例（单进程部署，SSE 与审批接口共享同一实例）
approval_broker = ApprovalBroker()
