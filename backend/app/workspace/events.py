"""
群组事件 —— 看板与 run 的状态变化**怎么被界面知道**。

★ 为什么需要一个专门的广播模块，而不是在各处 `group_bus.publish`

`group_bus` 已经存在，但它只被编排器用来推**群组消息**。任务板成实体之后，
界面上要动的东西从「一条时间线」变成三类状态：run 状态机、卡片状态、待审批。
如果让每个写入点各自 `publish` 一份字典，会同时得到两个毛病：

1. **键名漂移**。前端 reducer 读 `card["card_id"]`，后端某处发的是 `id` ——
   不报错，只是那一行永远刷不出来。
2. **漏广播**。漏的那一处症状是「界面上卡片还停在上一状态，直到用户刷新」，
   而这恰好是最难复现、最容易被当成网络问题的 bug。

所以这里收三件事：**事件名白名单**、**每类事件的载荷形状**、**run_id → group_id 的解析**。
`GROUP_EVENT_TYPES` 与前端 `workspaceStore.applyGroupEvent` 的分支、
以及 `test_group_events.py` 里的清单是同一份约定的三处副本 ——
测试钉住它们不漂移（与 kind 白名单三端一致同一手法）。

★ 广播点为什么落在 Board / RunStore 的写入方法里

「先落库再广播」这条纪律（`executor.py:399-401` 论证过：反了会产生
「库里没有、前端却见过」的幽灵事件）靠人自觉是守不住的。
而卡片状态的唯一写入点本来就是 `claim()` / `advance_card()`，
run 状态的唯一写入点本来就是 `advance()` ——
把广播放进这两个方法内部，「改了状态但没广播」就**不是「不该做」，而是「做不到」**。

★ 广播失败绝不打挂主流程（不变量 18）

事件推不出去只是界面慢一拍，用户刷新就对了；
让它变成「卡片写成功却抛异常」的话，一次推送故障会把整次协作判死。
所以这里所有路径都是 try/except + debug 日志。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from app.workspace.bus import group_bus

logger = logging.getLogger(__name__)

#: 群组通道上合法的事件名。前端只认这些；发不出去的名字直接丢弃并告警。
GROUP_EVENT_TYPES = frozenset(
    {
        # 既有两类
        "snapshot",
        "message",
        # 阶段 2 新增
        "run_status",
        "card_created",
        "card_claimed",
        "card_status",
        "approval_required",
        "approval_resolved",
        # 模型主动提问（与审批是两条路）：群组里的任务停下来等回答时，
        # 群聊时间线必须看得见这张卡片 —— 看不见就没人能回答。
        "question_required",
        "question_resolved",
        "guardrail_triggered",
    }
)

#: 阶段 3/4 会补上的两类（现在发出来也没有产生者，列在这里是为了别让人以为已上线）
DEFERRED_EVENT_TYPES = frozenset({"artifact_added", "budget_warning"})

#: `run_id -> group_id`。run 一生只属于一个群，所以这个映射不会变，
#: 而卡片事件是高频路径（每张卡至少两次），每次都查一遍库不划算。
_group_cache: dict[str, str] = {}


def publish(
    group_id: str,
    event_type: str,
    data: Optional[Mapping[str, Any]] = None,
    **payload: Any,
) -> None:
    """
    向一个群组频道广播一条事件。

    `event_type` 不在白名单里时**丢弃并告警**，不抛 ——
    打错事件名是发布方的 bug，让它把业务打挂是错上加错；
    而告警加一条 `GROUP_EVENT_TYPES` 的测试足以在 CI 里抓到它。

    ★ `data` 这个形参是给「整份载荷已经是字典」的调用方留的（见 `message()`）
    一条群组消息的行里本来就带 `group_id` 这个键，用 `**msg` 展开会撞上这里的同名形参，
    报的是 `got multiple values for argument 'group_id'` —— 那种写法在第一次
    广播互聊消息时才炸，而它本该是白屏之外最难查的一类。
    """
    if not group_id:
        return
    if event_type not in GROUP_EVENT_TYPES:
        logger.warning("未知的群组事件名，已丢弃: %s（白名单见 workspace/events.py）", event_type)
        return
    try:
        group_bus.publish(group_id, {"type": event_type, **(dict(data or {})), **payload})
    except Exception as e:  # noqa: BLE001
        logger.debug("群组事件广播失败（界面靠快照兜底）: group=%s %s", group_id, e)


def _group_of_run(run_id: str, db: Any = None) -> str:
    """
    卡片只带 `run_id`，而频道键是 `group_id` —— 这一层把前者翻译成后者。

    ★ `db` 由调用方给，而不是在这里取进程级单例

    `Board` 支持注入实例（测试用 `tmp_path` 的库，避免写进用户的真实库）。
    如果这一句查的是全局单例，症状会是「测试里怎么都不广播」，
    然后有人为了让它过而把广播断言删掉 —— 所以让 `Board` 把自己的库递进来，
    查的就是那张卡真实存在的那份数据。

    查不到（run 被删了 / 库不可用）就返回空串，`publish` 会静默跳过：
    一次协作的记录都没了，界面上该看到的是「这次协作不存在」，
    而不是凭空收到一张卡。
    """
    if not run_id:
        return ""
    cached = _group_cache.get(run_id)
    if cached is not None:
        return cached
    group_id = ""
    try:
        if db is None:
            from app.workspace.db import workspace_db

            db = workspace_db
        if db.enabled:
            row = db.query_one("SELECT group_id FROM runs WHERE id = ?", (run_id,))
            group_id = str((row or {}).get("group_id") or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("run 的群组归属查询失败（本次不广播）: run=%s %s", run_id, e)
    _group_cache[run_id] = group_id
    return group_id


# ---------- run ----------


def message(msg: Mapping[str, Any]) -> None:
    """
    时间线上新长了一条消息（含同事之间的定向话）。

    ★ 广播点在写入方那里，而不是在调用方
    协调者原先只写库、不广播，症状是「互聊的消息要刷新才看得见」——
    而这一屏的约定是「快照为真、SSE 只是加速」，两者都得有。
    `store.append_message` 是唯一写入口，所以凡是走它的地方都叫得到这一个函数。
    """
    publish(str(msg.get("group_id") or ""), "message", msg)


def run_status(run: Mapping[str, Any]) -> None:
    """run 状态机动了一下 —— 界面上的 spinner 与「取消」按钮只看这个。"""
    publish(
        str(run.get("group_id") or ""),
        "run_status",
        run=run_summary(run),
    )


def run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    from app.tasks.store import task_store
    pending = [task_store.get(tid) for tid in run.get("pending") or []]
    return {
        "run_id": str(run.get("id") or ""),
        "group_id": str(run.get("group_id") or ""),
        "status": str(run.get("status") or ""),
        "status_label": str(run.get("status_label") or run.get("status") or ""),
        "terminal": bool(run.get("terminal")),
        "mode": str(run.get("mode") or ""),
        "instruction": str(run.get("instruction") or ""),
        "error": str(run.get("error") or ""),
        "pending": list(run.get("pending") or []),
        "stall_count": int(run.get("stall_count") or 0),
        "claim_rounds": int(run.get("claim_rounds") or 0),
        "updated_at": str(run.get("updated_at") or ""),
        "budget": dict(run.get("budget") or {}),
        "resume_available": any(t and t.get("status") == "interrupted" for t in pending),
    }


# ---------- 卡片 ----------


def card_created(card: Mapping[str, Any], *, db: Any = None) -> None:
    _card_event(card, "card_created", db=db)


def card_claimed(card: Mapping[str, Any], *, db: Any = None) -> None:
    """CAS 抢成功那一刻（此时 `task_id` 可能还是空的 —— 派发是下一步）。"""
    _card_event(card, "card_claimed", db=db)


def card_status(card: Mapping[str, Any], previous_status: str = "", *, db: Any = None) -> None:
    """
    卡片变了 —— 载荷是**整份快照**，所以 `attach_task()` 这类「状态没动但链接变了」
    也走这里，用 `previous_status == status` 表示「这不是一次迁移」。
    """
    _card_event(card, "card_status", db=db, previous_status=previous_status)


def _card_event(card: Mapping[str, Any], event_type: str, *, db: Any = None, **extra: Any) -> None:
    publish(
        _group_of_run(str(card.get("run_id") or ""), db),
        event_type,
        card=card_summary(card),
        **extra,
    )


def card_summary(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "card_id": str(card.get("id") or ""),
        "run_id": str(card.get("run_id") or ""),
        "title": str(card.get("title") or ""),
        "status": str(card.get("status") or ""),
        "status_label": str(card.get("status_label") or card.get("status") or ""),
        "terminal": bool(card.get("terminal")),
        "batch": int(card.get("batch") or 0),
        "priority": str(card.get("priority") or "normal"),
        "owner_employee_id": str(card.get("owner_employee_id") or ""),
        "task_id": str(card.get("task_id") or ""),
        "depends_on": list(card.get("depends_on") or []),
        "artifact_ids": list(card.get("artifact_ids") or []),
        "blocked_reason": str(card.get("blocked_reason") or ""),
        "created_by": str(card.get("created_by") or ""),
        "updated_at": str(card.get("updated_at") or ""),
    }


# ---------- 待审批（护栏 6 的界面半边） ----------


def approval_required(
    *,
    thread_id: str,
    call_id: str,
    tool_name: str,
    args: Mapping[str, Any],
    reason: str,
    run_id: str = "",
    employee_id: str = "",
    requestor: str = "",
    tool_label: str = "",
) -> None:
    """
    任务板派发的执行挂在审批上 —— 推给**群组**频道。

    ★ 为什么频道键用 `thread_id`

    工作空间里执行器传的就是 `thread_id=group_id`，所以「这个审批属于哪个群」
    在这里不需要第二次查询；员工私聊路径传的是员工线程 id，那个频道上
    没有群组订阅者，`publish` 直接跳过 —— 不会串到别人的时间线里。

    缺了这条事件的后果不是「界面少一块」：任务板派发的危险工具**根本没有弹窗**，
    用户看不见、也就永远批不了，300 秒后被自动拒绝，
    而卡片显示成「卡住了」—— 看上去像员工干砸了。
    """
    publish(
        thread_id,
        "approval_required",
        thread_id=thread_id,
        call_id=call_id,
        tool_name="",
        tool_label=tool_label,
        args={},
        reason="",
        run_id=run_id,
        employee_id=employee_id,
        requestor=requestor,
    )


def approval_resolved(
    *,
    thread_id: str,
    call_id: str,
    approved: bool,
    decided_by: str = "user",
    run_id: str = "",
    employee_id: str = "",
) -> None:
    """决策已下 —— 前端据此把那张审批卡收掉（超时也要发，否则弹窗会挂着）。"""
    publish(
        thread_id,
        "approval_resolved",
        thread_id=thread_id,
        call_id=call_id,
        approved=bool(approved),
        decided_by=decided_by,
        run_id=run_id,
        employee_id=employee_id,
    )


# ---------- 待回答（模型主动提问的界面半边） ----------


def question_required(
    *,
    thread_id: str,
    call_id: str,
    question_id: str,
    questions: Any,
    run_id: str = "",
    employee_id: str = "",
    requestor: str = "",
    timeout_seconds: int = 0,
) -> None:
    """
    子任务停下来等用户回答 —— 推给群组频道。

    ★ 缺了这条事件的后果与审批完全相同
    任务板派发的 `ask_user` 没有卡片可点，用户看不见、也就永远答不了；
    10 分钟后按超时继续，任务基于假设跑完，而用户从未被告知她被问过。
    """
    publish(
        thread_id,
        "question_required",
        thread_id=thread_id,
        call_id=call_id,
        question_id=question_id,
        questions=questions if isinstance(questions, list) else [],
        run_id=run_id,
        employee_id=employee_id,
        requestor=requestor,
        timeout_seconds=int(timeout_seconds or 0),
    )


def question_resolved(
    *,
    thread_id: str,
    call_id: str,
    question_id: str,
    status: str,
    answers: Any = None,
    run_id: str = "",
    employee_id: str = "",
) -> None:
    """回答/跳过/超时 —— 前端据此把卡片收掉（超时也要发）。"""
    publish(
        thread_id,
        "question_resolved",
        thread_id=thread_id,
        call_id=call_id,
        question_id=question_id,
        status=status,
        answers=answers if isinstance(answers, dict) else {},
        run_id=run_id,
        employee_id=employee_id,
    )


# ---------- 护栏 ----------


def guardrail_triggered(
    group_id: str,
    *,
    code: str,
    message: str,
    run_id: str = "",
    card_id: str = "",
    guardrail: int = 0,
) -> None:
    """
    某条护栏真的动了：把「哪条护栏、在哪个环节点的、接下来会怎样」推给界面。

    ★ 为什么这条必须存在，而不是只写进群消息

    六护栏的共同要求是「必须明说」。少了它，用户看到的是任务莫名失败；
    有了它，`GuardrailNotice` 能直接回答「为什么这件事停在这儿」。
    `message` 是给人读的成品句子，不是 `reason=stall_limit` 这种机器码 ——
    后者到了界面上还得再翻译一遍，而翻译处总会漏掉几种情形。
    """
    publish(
        group_id,
        "guardrail_triggered",
        code=code,
        message=message,
        run_id=run_id,
        card_id=card_id,
        guardrail=int(guardrail or 0),
    )


def reset_event_cache() -> None:
    """清空 `run_id → group_id` 缓存（测试用；进程重启天然为空）。"""
    _group_cache.clear()


__all__ = [
    "DEFERRED_EVENT_TYPES",
    "GROUP_EVENT_TYPES",
    "approval_required",
    "approval_resolved",
    "card_claimed",
    "card_created",
    "card_status",
    "card_summary",
    "guardrail_triggered",
    "message",
    "publish",
    "reset_event_cache",
    "run_status",
    "run_summary",
]
