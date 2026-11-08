"""按用户请求分组汇报后台任务，等待依赖链结束后统一交付。

结果状态、实际文件和稳定消息 ID 同时用于持久化与 SSE；断线可补回终版。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from app.config import settings
from app.tasks.bus import task_bus
from app.tasks.models import SOURCE_SUPERVISOR, STATUS_SUCCEEDED, TaskRecord, is_terminal
from app.tasks.store import task_store

logger = logging.getLogger(__name__)

#: 汇报提示词 —— 只做「汇报」，不做别的。
#: 明确禁止派发 / 提问 / 执行：这是后台旁路，没有 SSE 连接在等它的审批，
#: 它若触发审批或提问，等待队列里没有任何人。
_REPORT_PROMPT = """【任务完成汇报】
你之前派给后台专员的任务刚刚完成了。请向用户做一份**总体汇报**：
- 先说结论：任务完成了什么、关键结果是什么（用你自己的话概括，不要逐字复述）。
- 如果结果里有产出文件，提一句文件名。**「产出文件」只能引用上面任务信息里列出的文件**；列表写着「产出文件：无」时必须如实说没有产出文件，禁止自己编造文件名或路径。
- 本次是同一个用户请求的最终汇报，不是第二次尝试。已经要求写报告时，不要再询问是否写报告。
- 以任务状态和实际文件为准；仍未交付时明确说未完成，不能用成功的检索步骤代替交付。
- 结果若标记为预览截断，表示汇报输入不完整，不表示原任务缺少章节；不要猜测完成百分比。
- 不要添加工具记录里没有发生过的重试、换源或执行动作。
- 不要重新派发任务，不要向用户提问，不要执行任何工具操作 —— 只汇报。
- 如果某个任务的结果看起来不完整或有问题，如实说明，不要假装一切完美。"""

#: 生成失败时的兜底文案模板（不依赖模型）。永远比「什么都不说」强。
_FALLBACK_TEMPLATE = "后台任务「{title}」{status}。{detail}"

#: 防抖计时器与领取锁（进程内；与 task_bus / approval_gate 同一套单进程假设）
_pending: Dict[str, asyncio.Task] = {}
_map_lock = asyncio.Lock()
_delivering: set[str] = set()
_reschedule: set[str] = set()


def _enabled() -> bool:
    return bool(getattr(settings, "TASK_REPORT_ENABLED", True))


def _debounce() -> float:
    try:
        return max(0.0, float(getattr(settings, "TASK_REPORT_DEBOUNCE_SECONDS", 2.5)))
    except (TypeError, ValueError):
        return 2.5


def _max_attempts() -> int:
    try:
        return max(1, int(getattr(settings, "TASK_REPORT_MAX_ATTEMPTS", 2)))
    except (TypeError, ValueError):
        return 2


def _result_chars() -> int:
    try:
        return max(200, int(getattr(settings, "TASK_REPORT_RESULT_CHARS", 1500)))
    except (TypeError, ValueError):
        return 1500


def schedule_report(thread_id: str, *, delay: Optional[float] = None) -> None:
    """
    任务终态到达时调用：安排一次汇报（防抖，**同步返回**）。

    ★ 为什么防抖
    supervisor 一轮可能派 2~4 个任务，它们几乎同时结束。每个终态都立刻
    汇报一次，用户会收到连珠炮式的插话；等 2.5 秒把它们并成**一次**。

    ★ 为什么同步
    调用点在 executor 的 `finally` 里，那里要尽量少 await；
    create_task 是安全的（asyncio 单线程，task 由调用方的事件循环持有）。

    `delay=None` 用配置的防抖值；0 = 立刻（启动补扫用）。
    """
    if not _enabled() or not thread_id:
        return
    wait = _debounce() if delay is None else max(0.0, delay)
    loop = asyncio.get_event_loop()
    existing = _pending.get(thread_id)
    if existing is not None and not existing.done():
        if thread_id in _delivering:
            _reschedule.add(thread_id)
            return
        # 又有任务结束：重置计时器，把两批并成一批
        existing.cancel()
    task = loop.create_task(_run_after(thread_id, wait), name=f"xjms-report-{thread_id[:12]}")
    _pending[thread_id] = task

    def _discard(done: asyncio.Task) -> None:
        if _pending.get(thread_id) is done:
            _pending.pop(thread_id, None)

    task.add_done_callback(_discard)


async def _run_after(thread_id: str, wait: float) -> None:
    """防抖计时 + 真正派送。计时器被取消（新终态重置）时安静退出。"""
    try:
        if wait > 0:
            await asyncio.sleep(wait)
        await deliver_reports(thread_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("汇报派送异常（任务保持未汇报，等待下次触发）: thread=%s %s", thread_id, e)


async def deliver_pending_reports() -> int:
    """
    启动补扫：把所有「成功了但还没汇报」的任务安排汇报。

    ★ 为什么需要它
    汇报发生在任务终态**之后**的几秒内 —— 进程若在那个窗口被杀，
    任务已经是 succeeded，不会再有任何终态事件触发它，汇报就永远没人送。
    启动时扫一遍，把漏掉的补上（attempts 上限仍生效，防永久失败的任务
    在每次重启时都重试）。

    返回安排了多少个会话。
    """
    if not _enabled() or not task_store.enabled:
        return 0
    try:
        records, _ = task_store.list(limit=500)
    except Exception as e:  # noqa: BLE001
        logger.warning("启动补扫读取任务失败: %s", e)
        return 0

    threads: set[str] = set()
    for record in records:
        if not _needs_report(record):
            continue
        thread_id = str(record.get("thread_id") or "")
        if thread_id:
            threads.add(thread_id)
    for thread_id in threads:
        schedule_report(thread_id, delay=0)
    if threads:
        logger.info("启动补扫安排汇报: %s 个会话", len(threads))
    return len(threads)


def _needs_report(record: TaskRecord) -> bool:
    """
    这个任务要不要走汇报。

    ★ 三条边界（少一条都会把汇报发给不该发的人）
    - 员工/群组任务（employee_id 非空）有自己的群组汇报通道（`orchestrator.on_employee_task_done`），
      再往个人对话写一遍等于同一件事通知两次。
    - 看板 / 员工私聊派发的任务（source 非 supervisor / 空）面向的是任务板，
      不是主对话。
    - 已经汇报过（reported_at 有值）或尝试次数已满的不再送。
    """
    if not is_terminal(str(record.get("status") or "")):
        return False
    if record.get("status") != STATUS_SUCCEEDED and not record.get("parent_turn_id"):
        return False  # 旧的独立失败任务已有即时失败通知，避免启动时重复发送。
    if str(record.get("employee_id") or ""):
        return False
    if str(record.get("source") or "") not in ("", SOURCE_SUPERVISOR):
        return False
    if str(record.get("reported_at") or ""):
        return False
    return int(record.get("report_attempts") or 0) < _max_attempts()


def _claim_tasks(thread_id: str) -> List[TaskRecord]:
    """
    领取这个会话里「成功但未汇报」的任务，并把尝试次数 +1。

    ★ 领取（attempts+1）先于送达（reported_at）
    两个会话级并发者（终态触发 / 启动补扫）同时跑时，先领取的那个
    会带走任务；另一个拿到空列表直接返回 —— 不会生成两份汇报。
    """
    try:
        records, _ = task_store.list(
            thread_id=thread_id,
            limit=200,
            sources=("", SOURCE_SUPERVISOR),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("汇报领取失败（本次不汇报）: thread=%s %s", thread_id, e)
        return []

    groups: Dict[str, List[TaskRecord]] = {}
    for record in records:
        key = str(record.get("parent_turn_id") or record.get("task_id"))
        groups.setdefault(key, []).append(record)
    # 必须等同一请求的全部步骤结束，不能对尚在执行的请求作最终结论。
    ready = [group for group in groups.values()
             if all(is_terminal(str(r.get("status"))) for r in group)]
    claimed: List[TaskRecord] = []
    for record in [r for group in ready for r in group]:
        if not _needs_report(record):
            continue
        task_id = str(record.get("task_id") or "")
        try:
            updated = task_store.patch(
                task_id,
                report_attempts=int(record.get("report_attempts") or 0) + 1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("汇报领取落盘失败: task=%s %s", task_id, e)
            continue
        if updated is not None:
            claimed.append(updated)
    return claimed


def _recent_dialogue(turns: List[Any]) -> str:
    """最近两轮压成几行（照抄 checkin 的 `_recent_dialogue`：只用于「接得上话」）"""
    lines: List[str] = []
    for turn in turns[-2:]:
        user_input = getattr(turn, "user_input", "") or ""
        answer = getattr(turn, "answer", "") or ""
        if user_input:
            lines.append(f"用户：{' '.join(user_input.split())[:120]}")
        if answer:
            lines.append(f"你：{' '.join(answer.split())[:160]}")
    if not lines:
        return ""
    return "【最近的对话】\n" + "\n".join(lines)


def _tasks_brief(tasks: List[TaskRecord]) -> str:
    """把任务结果压成提示词里的一段（每条按 `TASK_REPORT_RESULT_CHARS` 截断并说明）"""
    # 常规结果完整传递。特别长的结果有明确预览标识，不把截断当作缺失。
    limit = max(12000, _result_chars())
    parts: List[str] = []
    for record in tasks:
        title = str(record.get("title") or "未命名任务").strip()
        result = str(record.get("result") or record.get("error") or "").strip()
        files = record.get("files") or []
        lines = [f"【{title}】"]
        lines.append("状态：" + ("已完成" if record.get("status") == STATUS_SUCCEEDED else "未完成"))
        if result:
            shown = result if len(result) <= limit else result[: limit - 1] + "…"
            lines.append(f"结果：{shown}")
            if len(result) > limit:
                lines.append(f"【仅预览：原始结果共{len(result)}字，输入在此截断，不能据此判断原任务缺少章节。】")
        if files:
            names = "、".join(str(f.get("path") or "") for f in files[:5])
            if names:
                lines.append(f"产出文件：{names}")
        else:
            # 明示「无」而不是省略：汇报里「产出文件」四个字必须只从事实里来，
            # 省略会让模型以为「没提」等于「随便说」—— 事实是空，就说无。
            lines.append("产出文件：无")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def _generate_report(thread_id: str, tasks: List[TaskRecord], turn_id: Optional[str] = None) -> Optional[str]:
    """
    流式生成汇报正文：token 增量实时推给前端（`assistant_turn_token`）。

    返回**干净**的完整正文（已切推理）；任何失败返回 None（调用方走兜底文案）。

    ★ 为什么要流式推
    用户在主对话里看到的就是「总 Agent 正在打字」—— 与普通回答同一个
    渲染效果。不推的话，几秒的生成时间里界面上什么都没有，
    消息是「啪」一下整条出现的。
    """
    from app.agents.think import split_think
    from app.conversations import conversation_store
    from app.core.llm_factory import create_llm
    from app.persona import get_persona_manager

    loaded = conversation_store.get_conversation(thread_id)
    turns = loaded[1] if loaded else []

    stable, dynamic = get_persona_manager().build_prompt_parts(thread_id)
    prompt = "\n\n".join(
        part
        for part in (stable, dynamic, _recent_dialogue(turns), _tasks_brief(tasks), _REPORT_PROMPT)
        if part
    )

    turn_id = turn_id or uuid.uuid4().hex[:16]
    raw_parts: List[str] = []
    try:
        llm = create_llm(
            streaming=True,
            max_tokens=int(getattr(settings, "TASK_REPORT_MAX_TOKENS", 1200)),
            max_retries=0,
            timeout=float(getattr(settings, "TASK_REPORT_TIMEOUT_SECONDS", 90.0)),
        )
        async for chunk in llm.astream(prompt):
            piece = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
            if not piece:
                continue
            raw_parts.append(piece)
            task_bus.publish(
                thread_id,
                {
                    "type": "assistant_turn_token",
                    "turn_id": turn_id,
                    "content": piece,
                    # 流式期间就让前端知道「这是一次完成汇报」—— 完成标记
                    # 要在打字开始时就出现，而不是等终版事件才补上。
                    "task_count": len(tasks),
                    "task_status": _outcome(tasks),
                },
            )
    except Exception as e:  # noqa: BLE001
        logger.info("汇报生成失败（走兜底文案）: thread=%s %s", thread_id, e)
        return None

    _reasoning, clean = split_think("".join(raw_parts))
    clean = clean.strip()
    return clean or None


def _fallback_text(tasks: List[TaskRecord]) -> str:
    """生成失败时的固定文案：标题 + 结果摘要。永远可用，永远比沉默强。"""
    limit = _result_chars()
    parts: List[str] = []
    for record in tasks:
        title = str(record.get("title") or "未命名任务").strip()
        result = str(record.get("result") or record.get("error") or "").strip()
        if result:
            shown = " ".join(result.split())
            if len(shown) > limit:
                shown = shown[: limit - 1] + "…"
            detail = f"结果摘要：{shown}"
        else:
            detail = "你可以在任务面板里查看完整进度。"
        parts.append(_FALLBACK_TEMPLATE.format(title=title, detail=detail,
                     status="已完成" if record.get("status") == STATUS_SUCCEEDED else "未完成"))
    return "\n\n".join(parts)


async def deliver_reports(thread_id: str) -> None:
    if thread_id in _delivering:
        _reschedule.add(thread_id)
        return
    _delivering.add(thread_id)
    try:
        await _deliver_reports(thread_id)
    finally:
        _delivering.discard(thread_id)
        if thread_id in _reschedule:
            _reschedule.discard(thread_id)
            asyncio.get_running_loop().call_soon(schedule_report, thread_id)


def _outcome(tasks: List[TaskRecord]) -> str:
    succeeded = sum(r.get("status") == STATUS_SUCCEEDED for r in tasks)
    return "succeeded" if succeeded == len(tasks) else "partial" if succeeded else "failed"


async def _deliver_reports(thread_id: str) -> None:
    """
    领取同一请求的终态结果 → 流式生成 → 落盘和广播 → 标记已汇报。
    一个 turn_id 贯穿全程；用户在等待时发新消息不会丢掉已经完成的交付。
    """
    tasks = _claim_tasks(thread_id)
    if not tasks:
        return

    turn_id = uuid.uuid4().hex[:16]
    text = await _generate_report(thread_id, tasks, turn_id)
    if text is None:
        text = _fallback_text(tasks)

    # 后台交付不应因为用户在等待时发了新消息而消失；任务标题和请求分组提供上下文。
    _deliver_text(thread_id, turn_id, text, tasks)


def _deliver_text(thread_id: str, turn_id: str, text: str, tasks: List[TaskRecord]) -> None:
    """
    把汇报正文写进对话并广播（落盘成功才算送达）。

    ★ 顺序：先落盘 → 再广播 → 再销账
    落盘失败就什么都不发（用户刷新时也看不到的东西，实时推了反而是谎报）；
    广播的 `assistant_turn` 是**终版**，前端用它把流式中的那条消息收尾
    （断线没收到增量时，它就是整条消息本身）。
    """
    from app.conversations import INITIATED_BY_ASSISTANT, conversation_store
    from app.conversations.models import STATUS_OK, now_iso

    # 把这次收尾的**所有任务**的产物合并进汇报轮次：任务在它自己的记录里
    # 已经派生过一次 files（TaskRecord.files），这里直接引用那份真相 ——
    # 汇报消息因此能像普通回答一样渲染「本轮产出的文件」卡片。
    files: List[Dict[str, str]] = []
    seen: set[str] = set()
    for record in tasks:
        for f in record.get("files") or []:
            key = str(f.get("path") or "")
            if key and key not in seen:
                seen.add(key)
                files.append({"path": key, "tool": str(f.get("tool") or "")})

    summary = conversation_store.append_turn(
        thread_id,
        user_input="",
        answer=text,
        # `task_report` 是给重放路径的标记：重载历史后，这条消息的
        # 顶部同样要显示「N 项后台任务已完成」（实时路径靠广播的 task_count）。
        events=[
            {"type": "task_report", "count": len(tasks), "status": _outcome(tasks)},
            {"type": "token", "content": text},
        ],
        status=STATUS_OK,
        turn_id=turn_id,
        initiated_by=INITIATED_BY_ASSISTANT,
        # 显式传入（而不是让 append_turn 从事件流派生）：这里的事件是
        # task_report + token，派生只会得到空列表。None 时同样显式——
        # 让「这轮确实没有产物」也落成事实，重放路径不显示文件行。
        files=files or None,
    )
    if summary is None:
        logger.warning("汇报落盘失败（任务保持未汇报）: thread=%s", thread_id)
        task_bus.publish(thread_id, {"type": "assistant_turn_cancelled", "turn_id": turn_id})
        return

    task_bus.publish(
        thread_id,
        {
            "type": "assistant_turn",
            "turn_id": turn_id,
            "text": text,
            "task_count": len(tasks),
            "task_status": _outcome(tasks),
            # 实时路径没有重放那一步：文件卡片的渲染信息必须随终版广播走。
            "files": files,
        },
    )

    try:
        from app.channels.wechat_worker import wechat_channel

        wechat_channel.mirror_turn(thread_id, text)
    except Exception:  # noqa: BLE001 - 微信不可用仍照常展示桌面消息
        logger.warning("汇报同步微信失败: thread=%s", thread_id)

    for record in tasks:
        task_id = str(record.get("task_id") or "")
        try:
            task_store.patch(task_id, reported_at=now_iso())
        except Exception as e:  # noqa: BLE001
            logger.warning("汇报销账失败（可能重复汇报一次）: task=%s %s", task_id, e)

    logger.info("任务汇报已送达: thread=%s tasks=%s 字=%s", thread_id, len(tasks), len(text))


__all__ = [
    "deliver_pending_reports",
    "deliver_reports",
    "schedule_report",
]
