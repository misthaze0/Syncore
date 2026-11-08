"""
审批流程 —— 留痕的**唯一**实现（不变量 36，见 ADR 0019）。

★ 为什么把这段逻辑从 `graph.py` 抽出来

不变量 36 写着：「审批留痕的写入点**只有一处**：
`agents/graph.py::get_agent_event_stream`」。

Supervisor 架构下子图跑在后台执行器里，**不在那个生成器内** ——
子图的 danger 级工具同样要留痕。于是有两个选择：

- ❌ 两处都写 → 正是这条不变量禁止的「两边都写」。
  后果不是「重复」这么轻：重载时**后写的那条赢**，
  于是记录内容取决于竞态 —— 而审计数据最不能容忍这种不确定性。
- ✅ 抽成一个函数、两处**调用** → 写入点仍然唯一
  （`approval_flow.run_pending_approval`），变的只是「谁在调用它」。

唯一性反而比原来更强了：原来「唯一」靠的是「那个函数恰好在那儿」，
现在靠的是「所有留痕都必须经过这个模块」。

★ 与子图流程的唯一差异：并发闸门

主图直接 `await`；子图外面先套一层 `ApprovalGate.wait_turn` / `release`
（见 `approval_gate.py` 的模块注释 —— 不变量 40 在并行子图下失效）。

⚠️ 因此本函数**不持有闸门**，它只管「留痕 + 推事件 + 等决策 + 结算留痕」。
闸门的获取与释放由调用方负责，且必须在 `finally` 里释放。
这样闸门与留痕两件事的职责不会缠在一起 ——
缠在一起的话，「闸门卡住」与「留痕写失败」会互相掩盖。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, AsyncGenerator

from app.agents.approval_broker import approval_broker
from app.approval import (
    BY_GRANT,
    BY_HOOK,
    BY_MODE,
    BY_RULE,
    BY_TIMEOUT,
    BY_USER,
    DECISION_APPROVED,
    DECISION_REJECTED,
    DECISION_TIMEOUT,
    approval_store,
    grant_registry,
)
from app.hooks import NOTIFICATION, build_payload, fire
from app.permissions.policy import (
    SOURCE_HOOK,
    SOURCE_ASSISTANT,
    SOURCE_MODE,
    SOURCE_RULE,
    PermissionDecision,
)
from app.workspace import events as group_events

logger = logging.getLogger(__name__)

#: 判定来源 → 留痕里的「由谁决定」。
#: 三个自动来源必须各自独立（见 `approval/records.py` 的注释）：
#: 「权限规则」和「权限模式」在事后追责时完全不是一回事 ——
#: 一条是用户点名写的例外，一档是她对整个会话的笼统授权。
_POLICY_DECIDED_BY = {
    SOURCE_RULE: BY_RULE,
    SOURCE_MODE: BY_MODE,
    SOURCE_HOOK: BY_HOOK,
    SOURCE_ASSISTANT: "assistant",
}


def record_policy_outcome(
    *,
    thread_id: str,
    call_id: str,
    tool_name: str,
    args: dict[str, Any],
    danger_level: str,
    decision: PermissionDecision,
    run_id: str = "",
    employee_id: str = "",
) -> None:
    """
    给「没有人点过按钮」的判定留痕（Phase 5）。

    ★ 为什么安全工具不记
    `read`、`web_search` 这类一轮能跑十几次，记进审批留痕会把面板变成噪音，
    而审批面板的价值在于「一眼看到所有需要人负责的时刻」。
    只有本来**该问人**的操作（moderate / danger）被规则、模式或钩子代答时，
    才需要在审计里留下「是谁替我答的」。

    ★ 写入点仍然只有一个模块
    不变量 36 要求留痕写入集中在 `approval_flow` —— 本函数就住在这个模块里，
    两条 agent 循环都只是**调用**它。把 `approval_store` 直接 import 进节点是破坏那条不变量。
    """
    decided_by = _POLICY_DECIDED_BY.get(decision.source)
    if decided_by is None:
        return
    record = approval_store.open_request(
        thread_id=thread_id,
        call_id=call_id,
        tool_name=tool_name,
        args=args,
        danger_level=danger_level,
        reason=decision.reason,
        run_id=run_id,
        employee_id=employee_id,
    )
    approval_store.settle(
        record.record_id,
        decision=DECISION_APPROVED if decision.allowed else DECISION_REJECTED,
        decided_by=decided_by,
    )


async def _run_pending_approval(
    *,
    thread_id: str,
    call_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    danger_reason: str,
    danger_level: str = "danger",
    run_id: str = "",
    employee_id: str = "",
    requestor: str = "",
    decision_source: str = "danger_level",
    permission_mode: str = "",
) -> AsyncGenerator[dict[str, Any], None]:
    """
    处理一次待审批的危险操作：留痕 → 推事件 → 等决策 → 结算留痕。

    这是一个**异步生成器**：调用方消费它推出来的消息
    （`approval_auto` 或 `interrupt` + `state_update`，
    最后一条是 `{"_internal": ...}` 的**决策结果**）。
    拿到决策后用它构造 `Command(resume=...)` 恢复图执行。

    ★ `run_id` / `employee_id` / `requestor` —— 护栏 6「审批不可委托」（§5.7）

    三个都必须由调用方给（工作空间路径给得出，主对话给不出就留空）：
    前两个进 broker 的 key 与留痕，让「一次批准」在数据结构上就绑死到
    「哪一次协作、哪个员工」；`requestor` 是给人看的那句「邮件专员 想执行 …」，
    进 interrupt 事件与界面弹窗。

    缺了这些会发生什么（改之前的真实状态）：群组里 `thread_id == group_id`，
    三个员工并发时 `call_id` 是模型给的短 id 会撞 ——
    甲点「批准」唤醒的可能是乙的等待，于是乙的 `delete` 由甲的同意放行。
    界面上下方三条弹窗也完全一样，人类其实是在盲签。

    ★ 决策怎么拿（这里踩过一个语言限制）
    Python 不允许 async generator 带 return 值，
    而这个返回值是调用方**必需**的。所以决策走一条**内部**消息：
    `read_decision(event)` 返回 None 表示「不是决策，继续推给前端」，
    返回 bool 表示「这是决策，别再往下推了」。

    Args:
        thread_id: 归属会话（审批 key 的一半）
        call_id:   工具调用 id（另一半；与 thread_id + 两维组成 broker 的 key）
        tool_name / tool_args / danger_reason / danger_level: interrupt payload 里的字段

    Yields:
        `approval_auto`（预授权命中，不挂起）
        `interrupt`（常规路径，挂起等审批）
        `state_update`（审批决策的机器可读回执）
        `{"_internal": "_approval_decision", "approved": bool}`（**最后一条**）
    """
    from app.agents.tools.registry import tool_registry

    tool_label = tool_registry.display_name_for(tool_name)
    # ---------- Phase 6：先看预授权（命中则不打扰用户） ----------
    #
    # ★ 为什么这才是「批量审批」的正解（不变量 38 / 40）
    #
    # 不变量 6 让同一轮的多个 interrupt 前后相继、从不同时挂起，
    # 所以「多选后一起批」这种 UI 在单轮里根本无从实现。
    # 真正能减少打扰的是预授权：作用域绑死 (thread_id, employee_id, tool_name)、
    # 带次数与 TTL 双上限、**每次放行照常留痕**（不变量 38 三条边界）。
    from app.permissions.policy import decide, effective_mode
    from app.config import settings

    current = decide(tool_name, tool_args, danger_level=danger_level,
                     mode=effective_mode(permission_mode))
    if current.denied:
        record_policy_outcome(thread_id=thread_id, call_id=call_id, tool_name=tool_name,
                              args=tool_args, danger_level=danger_level, decision=current,
                              run_id=run_id, employee_id=employee_id)
        yield _decision(False)
        return
    # 用户写下的逐次询问与钩子要求不能被临时授权跳过。
    grant_allowed = decision_source not in (SOURCE_RULE, SOURCE_HOOK) and current.source not in (SOURCE_RULE, SOURCE_HOOK)
    grant = grant_registry.match(thread_id, tool_name, employee_id=employee_id,
                                 args=tool_args) if grant_allowed else None
    if grant is not None:
        record = approval_store.open_request(
            thread_id=thread_id,
            call_id=call_id,
            tool_name=tool_name,
            args=tool_args,
            danger_level=danger_level,
            reason=danger_reason,
            run_id=run_id,
            employee_id=employee_id,
        )
        grant_registry.consume(grant.grant_id)
        approval_store.settle(
            record.record_id,
            decision=DECISION_APPROVED,
            decided_by=BY_GRANT,
            grant_id=grant.grant_id,
        )

        # 仍然必须告知用户：静默执行危险操作是不可接受的。
        # ⚠️ 但这里**不能**复用 `interrupt` 事件 —— 那个事件在前端意味着
        # 「弹窗等你点」，而此刻没有任何东西在等。复用它就等于要求前端
        # 从 `auto_approved` 标志去猜「该不该弹窗」，那类隐式约定迟早会错；
        # 独立事件类型让「不可能被误渲染成弹窗」成为结构上的保证（不变量 39）。
        yield {
            "type": "approval_auto",
            "call_id": call_id,
            "tool_name": tool_name,
            "tool_label": tool_label,
            "args": tool_args,
            "reason": danger_reason,
            "run_id": run_id,
            "employee_id": employee_id,
            "requestor": requestor,
            "grant_id": grant.grant_id,
            "remaining": grant.remaining,
            "content": (
                f"「{tool_label}」已按预授权自动放行"
                f"（该授权还可自动放行 {grant.remaining} 次）"
            ),
        }
        yield {
            "type": "state_update",
            "content": "已批准执行（预授权自动放行）",
            "state_key": "approval",
            "value": True,
            "decided_by": BY_GRANT,
        }
        yield _decision(True)
        return

    # ---------- 常规路径：落痕 → 推送弹窗 → 等待 ----------
    #
    # ⚠️ 顺序：先落痕再推事件。这样「前端看到了弹窗」必然意味着
    # 「记录已经存在」。反过来写的话，进程在两者之间被杀就会出现
    # 「用户说弹过窗、系统里查无此事」。
    record = approval_store.open_request(
        thread_id=thread_id,
        call_id=call_id,
        tool_name=tool_name,
        args=tool_args,
        danger_level=danger_level,
        reason=danger_reason,
        run_id=run_id,
        employee_id=employee_id,
    )

    # ★ 先注册等待槽位，再推事件：避免前端「秒回」审批时丢单
    # （`ApprovalBroker._results` 那层兜底是第二道保险）
    interrupt_payload = {
        "call_id": call_id,
        "tool_name": tool_name,
        "tool_label": tool_label,
        "args": tool_args,
        "reason": danger_reason,
        # ★ 这三项是弹窗上「是谁在请求」的数据来源，也是前端回传的定位键。
        #   少了它们，一个群组里的三条审批弹窗长得一模一样（护栏 6 的界面症状）。
        "run_id": run_id,
        "employee_id": employee_id,
        "requestor": requestor,
        "request_id": uuid.uuid4().hex,
        "grant_allowed": grant_allowed,
        "grant_max_uses": min(100, max(1, settings.APPROVAL_GRANT_MAX_USES)),
        "grant_ttl_seconds": min(3600, max(1, settings.APPROVAL_GRANT_TTL_SECONDS)),
        "expires_at": time.time() + settings.APPROVAL_TIMEOUT_SECONDS,
    }
    approval_broker.register(
        thread_id, call_id, interrupt_payload, run_id=run_id, employee_id=employee_id
    )

    yield {"type": "interrupt", **interrupt_payload}
    # ★ 群组通道同步推一发（护栏 6 的界面半边，见 `workspace/events.py`）。
    # 频道键就是 `thread_id`：工作空间路径下它是 group_id（执行器派发时传的），
    # 于是群聊里看得见「某某专员想执行 X」；员工私聊与主对话的 thread 上
    # 没有群组订阅者，`publish` 自己跳过 —— 私聊的审批不会串进别人的时间线。
    # 少了这一发的真实后果不是「界面少一块」：任务板派发的危险工具**没有弹窗**，
    # 用户看不见就永远批不了，超时后自动拒绝，卡片显示成「卡住了」——
    # 看上去像员工干砸了，其实是没人被问过。
    group_events.approval_required(
        thread_id=thread_id,
        call_id=call_id,
        tool_name=tool_name,
        tool_label=tool_label,
        args=tool_args,
        reason=danger_reason,
        run_id=run_id,
        employee_id=employee_id,
        requestor=requestor,
    )

    # ---------- Notification 钩子：弹窗已经出现在界面上，通知该发出去了 ----------
    #
    # ★ 为什么放在等决策**之前**
    # 钩子的用途是「人不在电脑前也能知道她在等批准」。等完再发就是马后炮。
    # 这里是 `await` 而不是后台任务：宁可弹窗晚两秒出现，也不要一个
    # 「没人看着、异常没人收」的裸 task —— 那种写法出事时连日志都对不上号。
    note_outcome = await fire(
        NOTIFICATION,
        build_payload(
            NOTIFICATION,
            message=f"{requestor or '助手'} 想执行「{tool_label}」，正在等你批准。",
            reason=danger_reason,
            tool_name=tool_name,
            session={
                "thread_id": thread_id,
                "run_id": run_id,
                "employee_id": employee_id,
                "agent": requestor,
            },
        ),
        tool_name=tool_name,
    )
    for notice in note_outcome.notice:
        yield {
            "type": "hook_notice",
            "content": notice,
            "event": NOTIFICATION,
            "call_id": call_id,
            "tool_name": tool_name,
            "tool_label": tool_label,
        }

    outcome = await approval_broker.wait_for_decision(
        thread_id, call_id, timeout=max(0, interrupt_payload["expires_at"] - time.time()),
        run_id=run_id, employee_id=employee_id
    )
    approved = outcome.approved
    logger.info(
        "审批结果: thread=%s call=%s run=%s emp=%s approved=%s reason=%s",
        thread_id, call_id, run_id, employee_id, approved, outcome.reason,
    )

    # ★ 留痕结算的**唯一**写入点（不变量 36）。
    #
    # `/api/approval` 只负责唤醒等待者（`resolve`），不碰留痕：
    # 一个决策只该有一条记录，两个地方都写会得到重复事件，
    # 而重载时「后写的那条赢」会让记录内容取决于竞态。
    #
    # ⚠️ 超时与用户拒绝在**执行上等价**（都是不执行），
    # 在**留痕上完全不是一回事** —— 所以这里用 `outcome.timed_out` 区分，
    # 而不是把 bool 直接映射成 approved/rejected（不变量 37）。
    if outcome.timed_out:
        approval_store.settle(
            record.record_id, decision=DECISION_TIMEOUT, decided_by=BY_TIMEOUT,
        )
    else:
        approval_store.settle(
            record.record_id,
            decision=DECISION_APPROVED if approved else DECISION_REJECTED,
            decided_by=BY_USER,
        )

    # ★ 结算完立刻收掉界面上那张审批卡。
    # 超时也必须发：少了这一发，用户后来才打开页面时会看到一条挂着的「等你确认」，
    # 点下去只会得到「未找到等待中的审批」—— 那是最容易让人以为系统坏了的假象。
    group_events.approval_resolved(
        thread_id=thread_id,
        call_id=call_id,
        approved=approved,
        decided_by=BY_TIMEOUT if outcome.timed_out else BY_USER,
        run_id=run_id,
        employee_id=employee_id,
    )

    yield {
        "type": "state_update",
        "content": "已批准执行" if approved else "已拒绝执行",
        "state_key": "approval",
        "value": approved,
        "decided_by": BY_TIMEOUT if outcome.timed_out else BY_USER,
    }

    yield _decision(approved)


#: 审批决策的**内部**传递方式（不是 SSE 事件）
async def run_pending_approval(**kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
    """取消、断流和生成器关闭时也释放审批槽位，避免留下可批准的旧请求。"""
    from app.agents.approval_gate import approval_gate
    thread_id, call_id = kwargs["thread_id"], kwargs["call_id"]
    run_id, employee_id = kwargs.get("run_id", ""), kwargs.get("employee_id", "")
    gate_key = f"direct:{uuid.uuid4().hex}" if kwargs.pop("serialize", False) else ""
    if gate_key:
        await approval_gate.wait_turn(gate_key)
    completed = False
    request_id = ""
    try:
        async for event in _run_pending_approval(**kwargs):
            if event.get("type") == "interrupt":
                request_id = event["request_id"]
            if read_decision(event) is not None:
                completed = True
                if gate_key:
                    await approval_gate.release(gate_key)
            yield event
    finally:
        if gate_key:
            await approval_gate.release(gate_key)
        if not completed and request_id:
            payload = approval_broker.pending_payload(thread_id, call_id, run_id=run_id, employee_id=employee_id)
            if payload is None or payload.get("request_id") == request_id:
                approval_broker.discard(thread_id, call_id, run_id=run_id, employee_id=employee_id)
                record = approval_store.find_by_call(thread_id, call_id, run_id=run_id, employee_id=employee_id)
                if record is not None and not record.is_settled:
                    approval_store.settle(record.record_id, decision=DECISION_REJECTED, decided_by="system")
                    group_events.approval_resolved(thread_id=thread_id, call_id=call_id, approved=False,
                                                   decided_by="system", run_id=run_id, employee_id=employee_id)


DECISION_KEY = "_approval_decision"


def _decision(approved: bool) -> dict[str, Any]:
    """
    构造「决策结果」这条内部消息。

    ★ 为什么用 yield 传而不用 `return`
    Python **不允许** async generator 带 return 值
    （`SyntaxError: 'return' with value in async generator`）。
    而这个函数的返回值是调用方**必需**的（要拿它构造 `Command(resume=...)`），
    不能让调用方去「从事件里反推」——
    那种做法会把「工具执行成功」与「审批通过」混在一起（见 executor 的注释）。

    所以走这个**内部**通道：调用方取到它就 `break`，不推给前端。
    用 `_` 前缀标记 + 独立 key，与「它不是 SSE 事件」同构
    （同 `_internal: usage` 的做法，见 `runner.py::_emit_usage`）。
    """
    return {"_internal": DECISION_KEY, "approved": approved}


def read_decision(event: dict[str, Any]) -> bool | None:
    """
    从一条消息里读出审批决策；不是决策消息则返回 None。

    调��方的标准写法：
        async for event in run_pending_approval(...):
            if (decision := read_decision(event)) is not None:
                break
            yield event
    """
    if event.get("_internal") == DECISION_KEY:
        return bool(event.get("approved"))
    return None


def backfill_execution_result(
    thread_id: str,
    call_id: str,
    *,
    success: bool,
    result: str,
    run_id: str = "",
    employee_id: str = "",
) -> None:
    """
    工具真正执行完，把结果**回填**到审批留痕上（Phase 6）。

    ⚠️ 调用方必须先用 decision 判断「到底执行了没有」，不能只看 success。
    拒绝与超时两条路径**也会**发 `tool_result`（`success=False`，
    `result="用户已拒绝执行该操作"`）—— 那是「根本没执行」，
    不是「执行失败」。混为一谈的话，审批面板上会出现
    「已拒绝但执行失败」这种自相矛盾的记录，而审计恰恰最不能容忍这种含混。

    ★ `run_id` / `employee_id` 参与定位（护栏 6）
    群组里 thread_id 共用，只按 (thread_id, call_id) 找会挑到别人的记录 ——
    回填错了不报错，症状是「甲的执行结果写进了乙的审批留痕」。

    失败一律吞掉：留痕回填是**旁路**，它挂掉不该让工具执行变成失败
    （不变量 18：观测层永远不能把主流程打挂）。
    """
    try:
        record = approval_store.find_by_call(
            thread_id, call_id, run_id=run_id, employee_id=employee_id
        )
        if record is not None and record.decision == DECISION_APPROVED:
            approval_store.mark_executed(
                record.record_id,
                success=success,
                result=result,
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("审批留痕回填执行结果失败（已忽略）: %s", e)


def backfill_on_event(
    event: dict[str, Any],
    *,
    thread_id: str,
    run_id: str = "",
    employee_id: str = "",
) -> None:
    """
    见到 `tool_result` 事件时回填审批留痕的执行结果。

    ★ 为什么要抽出来而不是让两个驱动层各写一遍
    主图（`graph.py::record_event`）与子图执行器（`tasks/executor.py::emit`）
    都要做这件事 —— 它们是**两个**驱动层，而留痕的写入点必须唯一（不变量 36）。
    各写一遍的话，第二次有人改其中一处就会漂移；
    而漂移的那份症状是「审批面板上那条记录永远停在『已批准』，
    看不出工具到底执行成功没有」—— 不报错，只是审计信息缺了一角。

    不是 `tool_result` 事件就什么都不做（由调用方无条件调用即可）。
    """
    if event.get("type") != "tool_result":
        return
    call_id = str(event.get("call_id") or "")
    if not call_id:
        return
    backfill_execution_result(
        thread_id,
        call_id,
        success=bool(event.get("success")),
        result=str(event.get("result", "")),
        run_id=run_id,
        employee_id=employee_id,
    )


__all__ = [
    "DECISION_KEY",
    "backfill_execution_result",
    "backfill_on_event",
    "read_decision",
    "record_policy_outcome",
    "run_pending_approval",
]
