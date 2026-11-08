"""
中断任务的「能不能接着跑」评估（见 ADR 0019 的恢复一节）。

★ 为什么单开一个模块，而不是塞进 store 或 executor

三个东西必须同时读到才能回答这个问题：

    checkpointer 里剩下的图状态   → 还能不能从断点继续
    事件流里最后几步发生了什么     → 最后一步有没有已经执行
    任务记录                      → 该落到哪个状态

store 只懂文件、executor 只懂跑图，把它们焊在一起等于让持久层反向依赖
Agent 层。所以这里做唯一一次「对账」：输入 task_id，输出并落库一个明确结论。

★ 三种结论（写进 `resume_mode`）

    checkpoint  状态在、最后一步也没悬着 → 直接续
    approval    停在等确认                → 续的时候重新问一次
    reconcile   最后一步可能已有副作用    → 续之前必须先核对
    ""          没有可恢复的进度          → 落 orphaned（真没救）

★ 为什么 reconcile 不自动改成「跳过那一步」

跳过意味着由我们替用户决定「那次写入没发生」—— 而判断依据只是
「没看到回执」。把不确定说成确定，比多问一句更糟。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.tasks.models import (
    RESUME_MODE_APPROVAL,
    RESUME_MODE_CHECKPOINT,
    RESUME_MODE_RECONCILE,
    STATUS_INTERRUPTED,
    STATUS_ORPHANED,
)
from app.tasks.store import task_store

logger = logging.getLogger(__name__)


async def reconcile_interrupted(limit: int = 200) -> Dict[str, int]:
    """
    启动时调用：评估所有 `interrupted` 任务，并把结论落库。

    包含两类来源：本次启动 `sweep_orphans` 刚标出来的（进程崩了），
    以及上次**正常关停**时就标成中断的（`executor.shutdown` 的礼貌取消）。
    两者用的是同一条恢复路径 —— 对用户来说「为什么断的」不影响「能不能接着跑」。
    """
    summary = {"resumed_ready": 0, "needs_reconcile": 0, "finished": 0, "orphaned": 0}
    if not task_store.enabled:
        return summary
    try:
        records, _ = task_store.list(status=STATUS_INTERRUPTED, limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取中断任务失败（不影响其它功能）: %s", e)
        return summary

    for record in records:
        task_id = str(record.get("task_id") or "")
        try:
            outcome = await reconcile_one(task_id)
        except Exception as e:  # noqa: BLE001
            # 评估失败不等于不可续：保持 interrupted，让用户自己决定。
            # 判死才是不可逆的，而这里没有证据支持判死。
            logger.warning("任务可续性评估失败（保持中断状态）: %s %s", task_id, e)
            continue
        if outcome in summary:
            summary[outcome] += 1

    if any(summary.values()):
        logger.info(
            "中断任务评估: 可续 %s / 需核对 %s / 已补结算 %s / 不可续 %s",
            summary["resumed_ready"],
            summary["needs_reconcile"],
            summary["finished"],
            summary["orphaned"],
        )
    return summary


async def reconcile_one(task_id: str) -> str:
    """
    评估单个任务并落库。返回值是结果分类（见模块头），失败时抛异常。

    ★ 只有真的查不到任何图状态时才判 `orphaned`。
    「Redis 连不上」这种情况 `aget_state` 会抛异常，调用方保持 interrupted —
    把可恢复的任务判死，比晚一点恢复它严重得多。
    """
    record = task_store.get(task_id)
    if record is None:
        return ""
    if str(record.get("status") or "") != STATUS_INTERRUPTED:
        return ""

    snapshot = await _load_snapshot(task_id)
    state: Dict[str, Any] = dict(getattr(snapshot, "values", None) or {}) if snapshot else {}
    next_nodes = tuple(getattr(snapshot, "next", ()) or ())

    # ---------- ① 图已经跑完，只是没来得及结算 ----------
    # 进程在「子图返回」与「mark_succeeded」之间被杀。这里补上，避免一次
    # 已经完成的活被当成中断再跑一遍（那会重复生成文件）。
    if state and not next_nodes:
        answer = str(state.get("final_answer") or state.get("sub_agent_result") or "")
        if answer.strip():
            files = _collect_files(state)
            task_store.mark_succeeded(task_id, result=answer, files=files)
            return "finished"
        task_store.patch(
            task_id,
            status=STATUS_ORPHANED,
            error="服务重启时中断，没有留下可以继续的进度",
        )
        return "orphaned"

    # ---------- ② 没有任何图状态 ----------
    if not state:
        task_store.patch(
            task_id,
            status=STATUS_ORPHANED,
            error="服务重启时中断，没有留下可以继续的进度（可能是状态存储被清空）",
        )
        return "orphaned"

    # ---------- ③ 停在等确认 / 等回答 ----------
    interrupts = _snapshot_interrupts(snapshot)
    if interrupts:
        payload = interrupts[0] if isinstance(interrupts[0], dict) else {}
        from app.questions.models import is_question_payload

        if is_question_payload(payload):
            questions = payload.get("questions") or []
            first = questions[0] if isinstance(questions, list) and questions else {}
            header = str(first.get("header") or first.get("question") or "提问")
            task_store.patch(
                task_id,
                resume_mode=RESUME_MODE_APPROVAL,
                resume_note=f"停在「{header}」的提问上，继续后会重新问一次",
                # 重启后旧卡片的 broker 已经没了，留着只会让界面挂一张
                # 点不动的问卷；续跑会重新生成一张新的。
                pending_question=None,
            )
            return "resumed_ready"

        tool_name = str(payload.get("tool_label") or payload.get("tool_name") or "危险操作")
        task_store.patch(
            task_id,
            resume_mode=RESUME_MODE_APPROVAL,
            resume_note=f"停在「{tool_name}」的确认上，继续后会重新问一次",
        )
        return "resumed_ready"

    # ---------- ④ 还有待执行的工具调用 ----------
    pending = list(state.get("pending_calls") or [])
    if pending:
        head = pending[0] if isinstance(pending[0], dict) else {}
        call_id = str(head.get("call_id") or "")
        label = _describe_call(head)
        has_result, was_approved, was_awaiting = _scan_events(
            task_store.read_progress_lines(task_id), call_id
        )

        if has_result:
            note = f"「{label}」已经执行过，但进度没来得及保存；继续会重做这一步"
        elif was_approved:
            note = f"「{label}」已获批准并可能已经执行；继续会重做这一步，请先核对"
        elif was_awaiting:
            # 弹窗还没人点 —— 这跟普通中断没有区别，续跑会重新问
            task_store.patch(
                task_id,
                resume_mode=RESUME_MODE_APPROVAL,
                resume_note=f"停在「{label}」的确认上，继续后会重新问一次",
            )
            return "resumed_ready"
        else:
            note = f"「{label}」可能已经执行了；继续前请先核对它有没有生效"

        task_store.patch(task_id, resume_mode=RESUME_MODE_RECONCILE, resume_note=note)
        return "needs_reconcile"

    # ---------- ⑤ 停在两个节点之间 ----------
    task_store.patch(task_id, resume_mode=RESUME_MODE_CHECKPOINT, resume_note="")
    return "resumed_ready"


async def _load_snapshot(task_id: str) -> Any:
    """
    取子图在该 task 的 checkpoint 快照；取不到就抛（调用方保持 interrupted）。

    ★ 每次评估都重新编译子图是刻意的
    图对象本身无状态（状态全在 checkpointer 里），编译成本远小于「为了省一次编译
    而缓存一张与当前代码不同版本的图」—— 后者会让重启后的恢复跑在旧拓扑上。
    """
    from app.agents.subgraph import build_subgraph, subtask_thread_id
    from app.core.redis_client import get_checkpointer

    checkpointer = get_checkpointer()
    if checkpointer is None:
        raise RuntimeError("checkpointer 未就绪")
    subgraph = build_subgraph(checkpointer=checkpointer)
    return await subgraph.aget_state(
        {"configurable": {"thread_id": subtask_thread_id(task_id)}}
    )


def _snapshot_interrupts(snapshot: Any) -> list[Any]:
    """
    从快照里取挂起的 interrupt。

    ★ 与 `executor._collect_interrupts` 同一件事但输入不同
    那边处理的是 `ainvoke` 的返回值，这边是 `aget_state` 的快照。
    两处都试 `.tasks[*].interrupts` 与 `.interrupts`，因为版本间这两条路径
    至少有一条可用；都取不到就当作「没有挂起」。
    """
    found = getattr(snapshot, "interrupts", None)
    if found:
        return list(found)
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts = getattr(task, "interrupts", None)
        if interrupts:
            return list(interrupts)
    return []


def _scan_events(entries: list[Dict[str, Any]], call_id: str) -> tuple[bool, bool, bool]:
    """
    扫一遍进度行，回答「这个 call 有没有回执 / 有没有被批准 / 是不是还在等人点」。

    ★ 为什么按 `kind` / `status` / `call_id` 判断，而不是原始事件的 `type`
    `append_event` 落盘的是**折叠后**的进度条目（见 `store._entry_from_event`），
    原始事件的 `type` 不进文件。好在这三个字段足够区分：
        tool_result            kind=tool_result（带 call_id）
        interrupt              kind=approval + status=awaiting（带 call_id）
        approval_auto          kind=approval + status=ok（带 call_id）
        审批决策               kind=approval + **不带** call_id
    最后一种没有 call_id，只能按落盘顺序归给它前面最近的那次等待
    （`approval_flow` 的产出顺序就是「interrupt → 决策」）。
    """
    results: set[str] = set()
    approved: set[str] = set()
    awaiting: set[str] = set()
    last_waiting: Optional[str] = None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        status = str(entry.get("status") or "")
        cid = str(entry.get("call_id") or "")
        if kind == "tool_result" and cid:
            results.add(cid)
        elif kind == "approval":
            if cid and status == "awaiting":
                awaiting.add(cid)
                last_waiting = cid
            elif cid:
                approved.add(cid)
                awaiting.discard(cid)
            else:
                target = last_waiting
                if target:
                    awaiting.discard(target)
                    if status == "ok":
                        approved.add(target)
                last_waiting = None

    return call_id in results, call_id in approved, call_id in awaiting


def _describe_call(call: Dict[str, Any]) -> str:
    """把一次工具调用说成界面上能读的一句话（尽量带上目标文件/命令）"""
    name = str(call.get("tool_label") or call.get("tool_name") or "工具")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    for key in ("path", "file_path", "filename", "cmd", "command", "query", "url"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name} {value.strip()[:60]}"
    return name


def _collect_files(state: Dict[str, Any]) -> Optional[list[dict[str, str]]]:
    """从补结算的图状态里收集产出的文件（与会话历史复用同一套派生逻辑）"""
    from app.conversations.files import collect_written_files

    calls = state.get("tool_calls") or []
    if not calls:
        return None
    return collect_written_files(calls, str(state.get("workdir_root") or ""))
