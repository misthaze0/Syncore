"""
提问协调器 —— 把「前端提交回答」送到「图里正在等待的那一次 `ask_user`」。

设计与 `ApprovalBroker` 同构（同样的两处竞态防护、同样的四元组定位），
但 key 多一维 `question_id` 的校验：同一个 `call_id` 在恢复失败后可能被
重新发起，旧卡片上的回答绝不能唤醒新一次提问。

★ 为什么先 register 再推事件
前端完全可能在弹窗出现的同一帧就点「发送」。等待槽位必须先就绪，
否则那次回答会因为「找不到等待者」被判过期 —— 用户看到的是「点了没反应」。

★ 为什么允许 resolve 早于 wait（`_results` 暂存）
`run_pending_question` 里 register → yield 事件 → await wait 是三步，
而事件被前端消费、用户点击、HTTP 请求回来可能发生在第三步入队之前。
没有暂存，这个窗口里的回答会被丢弃；而它恰恰是最快的用户。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.questions.models import (
    REASON_CANCELLED,
    REASON_TIMEOUT,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    QuestionOutcome,
)

logger = logging.getLogger(__name__)

Key = tuple[str, str, str, str]


@dataclass
class _Pending:
    question_id: str
    payload: dict[str, Any]
    #: 登记时刻（epoch 秒）—— 快照重画的卡片要靠它还原倒计时，
    #: 否则每刷新一次倒计时就从头开始，用户看到的时限是假的
    asked_at: float = field(default_factory=time.time)


class QuestionBroker:
    """按 `(thread_id, call_id, run_id, employee_id)` 维度等待用户回答。"""

    def __init__(self) -> None:
        self._registered: dict[Key, _Pending] = {}
        self._futures: dict[Key, asyncio.Future] = {}
        self._results: dict[Key, QuestionOutcome] = {}

    @staticmethod
    def _key(
        thread_id: str, call_id: str, run_id: str = "", employee_id: str = ""
    ) -> Key:
        return (thread_id, call_id, run_id, employee_id)

    # ---------- 等待方（驱动层） ----------
    def register(
        self,
        thread_id: str,
        call_id: str,
        payload: dict[str, Any],
        *,
        run_id: str = "",
        employee_id: str = "",
    ) -> None:
        key = self._key(thread_id, call_id, run_id, employee_id)
        self._registered[key] = _Pending(
            question_id=str(payload.get("question_id") or ""),
            payload=dict(payload),
        )

    async def wait_for_answer(
        self,
        thread_id: str,
        call_id: str,
        timeout: int | None = None,
        *,
        run_id: str = "",
        employee_id: str = "",
    ) -> QuestionOutcome:
        key = self._key(thread_id, call_id, run_id, employee_id)

        # 1) 回答先于等待到达
        if key in self._results:
            outcome = self._results.pop(key)
            self._cleanup(key)
            return outcome

        loop = asyncio.get_running_loop()
        future = self._futures.get(key)

        # 2) Future 已就绪：直接取结果，切勿新建 Future 空等
        if future is not None and future.done():
            outcome = future.result()
            self._cleanup(key)
            return outcome if isinstance(outcome, QuestionOutcome) else QuestionOutcome()

        # 3) 按需创建，并确保绑定在当前运行中的 loop
        if future is None or future.get_loop() is not loop:
            future = loop.create_future()
            self._futures[key] = future

        if timeout is None:
            timeout = int(getattr(settings, "QUESTION_TIMEOUT_SECONDS", 600))

        try:
            outcome = await asyncio.wait_for(future, timeout=timeout)
            return outcome if isinstance(outcome, QuestionOutcome) else QuestionOutcome()
        except asyncio.TimeoutError:
            logger.warning(
                "提问超时（%ss），按跳过继续: thread=%s call=%s run=%s emp=%s",
                timeout, thread_id, call_id, run_id, employee_id,
            )
            return QuestionOutcome(
                status=STATUS_TIMEOUT,
                reason=REASON_TIMEOUT,
            )
        finally:
            self._cleanup(key)

    def _cleanup(self, key: Key) -> None:
        self._registered.pop(key, None)
        self._futures.pop(key, None)
        self._results.pop(key, None)

    # ---------- 响应方（/api/questions/answer） ----------
    def resolve(
        self,
        thread_id: str,
        call_id: str,
        question_id: str,
        outcome: QuestionOutcome,
        *,
        run_id: str = "",
        employee_id: str = "",
    ) -> bool:
        """
        提交回答。

        ★ `question_id` 必须与登记时一致
        用户手里的卡片可能来自一次已经超时的提问；而 `call_id` 是模型给的、
        重试时很可能重复。只按四元组匹配会把旧卡片的答案喂给新提问 ——
        模型会基于一个早已不成立的选择继续干活，且没有任何报错。
        """
        key = self._key(thread_id, call_id, run_id, employee_id)
        pending = self._registered.get(key)
        if pending is None:
            logger.warning(
                "提问无对应等待者，已忽略: thread=%s call=%s qid=%s run=%s emp=%s",
                thread_id, call_id, question_id, run_id, employee_id,
            )
            return False
        if question_id and pending.question_id and question_id != pending.question_id:
            logger.warning(
                "提问 id 不匹配，已忽略: call=%s 期望=%s 收到=%s",
                call_id, pending.question_id, question_id,
            )
            return False

        future = self._futures.get(key)
        if future is not None and not future.done():
            loop = future.get_loop()
            if loop.is_running():
                future.set_result(outcome)
                logger.info(
                    "提问回答已交付: call=%s qid=%s status=%s emp=%s",
                    call_id, pending.question_id, outcome.status, employee_id,
                )
                return True
            logger.warning("等待中的 loop 已停止，回答转为暂存: call=%s", call_id)

        self._results[key] = outcome
        logger.info(
            "提问回答已暂存，等待方就绪后取用: call=%s status=%s",
            call_id, outcome.status,
        )
        return True

    def get_pending(
        self,
        thread_id: str,
        call_id: str,
        *,
        run_id: str = "",
        employee_id: str = "",
    ) -> dict[str, Any] | None:
        """取某次提问的登记内容（接口层用它校验回答的选项）。"""
        pending = self._registered.get(self._key(thread_id, call_id, run_id, employee_id))
        if pending is None:
            return None
        return {
            "question_id": pending.question_id,
            "questions": list(pending.payload.get("questions") or []),
        }

    # ---------- 观测 ----------
    def list_pending(self) -> list[dict[str, Any]]:
        keys = set(self._registered) | set(self._futures) | set(self._results)
        out: list[dict[str, Any]] = []
        for (thread_id, call_id, run_id, employee_id) in keys:
            pending = self._registered.get((thread_id, call_id, run_id, employee_id))
            item: dict[str, Any] = {
                "thread_id": thread_id,
                "call_id": call_id,
                "run_id": run_id,
                "employee_id": employee_id,
                "staged": (thread_id, call_id, run_id, employee_id) in self._results,
            }
            if pending is not None:
                item["question_id"] = pending.question_id
                item["questions"] = pending.payload.get("questions") or []
                # ★ 快照要能**重画出整张卡片**，不是只知道「在等」。
                # requestor / tool_label / timeout_seconds 都在登记载荷里，
                # 漏给它们，刷新后的卡片就只剩题干、没有「是谁在问」和倒计时。
                for extra in ("tool_name", "tool_label", "requestor", "timeout_seconds"):
                    value = pending.payload.get(extra)
                    if value not in (None, "", 0):
                        item[extra] = value
                # 毫秒时间戳：前端据此从「提问发生的那一刻」继续倒数
                item["asked_at"] = int(pending.asked_at * 1000)
            out.append(item)
        return out

    def cancel_all(self) -> None:
        """应用关闭时取消全部等待（按跳过处理，避免挂死）。"""
        outcome = QuestionOutcome(status=STATUS_SKIPPED, reason=REASON_CANCELLED)
        for future in self._futures.values():
            if not future.done():
                future.set_result(outcome)
        self._futures.clear()
        self._registered.clear()
        self._results.clear()


question_broker = QuestionBroker()


__all__ = ["QuestionBroker", "question_broker"]
