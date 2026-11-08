"""
后台子任务的编排工具（Supervisor 架构，见 ADR 0019）：派发、查询、取消。

★ 为什么是工具而不是「把后台任务塞进 prompt」

两者**都要，但职责不重叠**（这是本架构最关键的一个取舍）：

- **工具 = 真相来源。** 要某个任务的结果、要它的进度，只能调工具拿。
  结果正文可能几 KB，塞进 system prompt 会挤爆上下文。
- **prompt 注入 = 存在性提示。** 只注入「有 2 个任务在跑」这种一行摘要，
  目的**只是**让模型知道「有事可以问」。它带硬上限（5 条 / 500 字），
  而且必带「以本次查询为准」的出口（同不变量 27 的记忆注入约定）。

为什么不能只有工具：模型得先猜「该不该调这个工具」，
而一个正在跑的任务在 prompt 里**没有任何痕迹**时，它根本不会想到去查 ——
用户问「我那个任务怎么样了」，模型会直接说「我不知道」。

为什么不能只有 prompt 注入：
① 注入的内容每轮都在，不可追溯，且会污染每一次调用的上下文；
② 长结果会撑爆上下文窗口；
③ 用户问「结果里写了什么」时，注入里没有正文。

★ 注入块的唯一位置

`build_prompt_block()` 是**唯一**的注入点（照抄 `runner.py:377-385` 的
长期记忆注入模式）。将来有人想「再加一处」时，
`test_prompt_block_single_injection_point` 会失败。
"""

from __future__ import annotations

import logging

from app.agents.tools.context import ToolContext
from app.agents.tools.decorator import agent_tool
from app.tasks.bus import task_bus
from app.tasks.models import SOURCE_SUPERVISOR, STATUS_SUCCEEDED, is_active
from app.tasks.store import task_store

# ⚠️ 上面的 import **在导入时就绑定了对象**，不是每次调用去查 `store.task_store`。
# 运行期没问题（三处都指向同一个单例），但测试里要替换存储时
# 必须同时 patch `app.tasks.store.task_store`、`app.tasks.executor.task_store`
# **和这一处的 `task_store`** —— 只 patch 前两处的话，本模块仍读旧实例，
# 症状是 `build_prompt_block` 恒返回空串而 store 里明明有任务。
# 这个坑我踩过一次，所以留在这里。

logger = logging.getLogger(__name__)

#: 注入块里的任务条数上限与字符上限（同不变量 27 的硬上限约定）
MAX_INJECTED_TASKS = 5
MAX_BLOCK_CHARS = 500

#: 单个会话允许同时存在的未结束任务数（`task_dispatch` 的闸门）
MAX_ACTIVE_PER_THREAD = 5

#: 状态 → 人话。**基础界面禁术语**（产品的既有口径），所以这里给的是人话。
_STATUS_TEXT: dict[str, str] = {
    "pending": "排队中",
    "running": "进行中",
    "waiting_approval": "等你确认",
    "waiting_input": "等你回答",
    # ★ 不是「已中断」这种终态说法：它还能接着跑，界面上有「继续」按钮
    "interrupted": "中断了，可以继续",
    "succeeded": "已完成",
    "failed": "失败了",
    "cancelled": "已取消",
    "orphaned": "服务重启时中断了",
}


def _health() -> bool:
    """工具健康检查：任务存储能读即为健康"""
    try:
        task_store.stats()
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("任务存储健康检查失败: %s", e)
        return False


@agent_tool(
    display_name="查看后台任务",
    description=(
        "列出后台子任务（哪些在跑、哪些已完成、哪些失败了）。"
        "用户问「我那个任务怎么样了」「有几个在跑」时调用。"
    ),
    category="编排",
    danger_level="safe",
    read_only=True,
    allowed_agents=["supervisor"],
    health_handler=_health,
)
def tasks_list(status: str = "", ctx: ToolContext | None = None) -> str:
    """列出本会话的后台子任务。

    Args:
        status: 逗号分隔的状态过滤（pending/running/waiting_approval/interrupted/succeeded/failed/cancelled），留空表示全部
    """
    # ★ 会话归属从 `ctx` 拿，不做成模型可填的参数：
    # 让它填 thread_id 等于给了一个「查别人会话的任务列表」的口子，
    # 而模型本来就不知道正确答案（它没看过 thread_id）。
    # 上下文里没有会话身份时（脚本直调）才退回「全部」。
    thread_id = (ctx.thread_id if ctx else "") or ""
    try:
        records, total = task_store.list(thread_id=thread_id, status=status, limit=20)
    except Exception as e:  # noqa: BLE001
        # ★ 降级必须说清：返回一个「查不到」而不是空列表 ——
        # 空列表会被模型理解成「没有任务」，而真相是「查不了」。
        return f"（子任务状态暂时查不到：{e}）"

    scope = "本会话" if thread_id else "全部会话"
    if not records:
        return f"{scope}当前没有后台任务。"

    lines = [f"{scope}共 {total} 个任务："]
    for r in records[:20]:
        st = str(r.get("status") or "")
        title = str(r.get("title") or "未命名任务")
        text = _STATUS_TEXT.get(st, st)
        line = f"- {title}（{text}）"
        if is_active(st):
            line += f" · id={r.get('task_id')}"
        if r.get("status") == "succeeded" and r.get("result"):
            line += f"\n  结果摘要：{str(r['result'])[:120]}"
        lines.append(line)
    return "\n".join(lines)


@agent_tool(
    display_name="查看任务进度",
    description=(
        "取一个后台子任务的进度与最终结果。"
        "task_id 用 tasks_list 拿。用户问「查资料那个好了吗」时调用。"
    ),
    category="编排",
    danger_level="safe",
    read_only=True,
    allowed_agents=["supervisor"],
    health_handler=_health,
)
def task_status(task_id: str, ctx: ToolContext | None = None) -> str:
    """
    取一个后台子任务的进度与最终结果。

    Args:
        task_id: 任务 id（用 tasks_list 拿）
    """
    try:
        record = task_store.get(task_id)
    except Exception as e:  # noqa: BLE001
        return f"（任务 {task_id} 的状态暂时查不到：{e}）"

    if record is None:
        return f"找不到任务 {task_id}。"

    # ★ 跨会话的 task_id 不给读：task_id 只有 16 位，但它是**可猜**的，
    # 而协作群组里别人的会话也在同一份索引里。模型没有理由知道别的会话的 id，
    # 所以这条判定不会影响任何正当用法，只挡住越权。
    thread_id = ctx.thread_id if ctx else ""
    if thread_id and str(record.get("thread_id") or "") != thread_id:
        return f"任务 {task_id} 不属于当前会话，我不能读它的进度。"

    st = str(record.get("status") or "")
    title = str(record.get("title") or "未命名任务")
    text = _STATUS_TEXT.get(st, st)
    out = [f"任务：{title}（{text}）"]

    # ---------- 进度 ----------
    try:
        view = task_store.progress(task_id, limit=10)
        entries = view.get("entries") or []
        if entries:
            out.append("进度：")
            for e in entries:
                detail = e.get("detail")
                suffix = f"（{detail}）" if detail else ""
                out.append(f"  - [{e.get('status', '')}] {e.get('label', '')}{suffix}")
            if view.get("total_steps"):
                out.append(f"（共 {view['total_steps']} 步，只显示最近 {len(entries)} 步）")
        else:
            out.append("进度：还没有可显示的步骤。")
    except Exception as e:  # noqa: BLE001
        out.append(f"（进度读取失败：{e}）")

    # ---------- 结果 ----------
    # ⚠️ 没结果时**不说**「结果：空」—— 区分「没有」与「还没好」：
    # 后者才是不说结果的原因。
    if st == "succeeded" and record.get("result"):
        out.append(f"最终结果：\n{record['result']}")
    elif st == "failed":
        out.append(f"失败原因：{record.get('error') or '未记录'}")
    elif is_active(st):
        out.append("还在进行中，稍后再问一次。")
    return "\n".join(out)


@agent_tool(
    display_name="派发后台任务",
    description=(
        "把一个需要多步、耗时长的活派给后台专员去做（立即返回，不等它跑完）。"
        "适合「查一批资料并整理成纪要」「把这份数据做成报告」这类"
        "**单轮对话做不完**的事；一步就能答的问题别派任务，直接自己答。"
        "派发后结果不会出现在这一轮里，要稍后用 tasks_list / task_status 查。"
    ),
    category="编排",
    danger_level="moderate",
    allowed_agents=["supervisor"],
    health_handler=_health,
)
def task_dispatch(
    title: str,
    instruction: str,
    capability: str = "",
    ctx: ToolContext | None = None,
) -> str:
    """把一件事派给后台专员执行。

    Args:
        title: 任务名，几个字，会显示在任务列表里（例：「整理季度纪要」）
        instruction: 给专员的完整任务说明 —— 它看不到这轮对话，必须自包含：目标、涉及的文件或资料、交付形式、判断标准
        capability: 能力名（用系统提示里列出的那些）；留空由系统按任务内容挑选
    """
    from app.agents.registry import get_registry
    from app.tasks.executor import executor

    clean_title = (title or "").strip()
    clean_instruction = (instruction or "").strip()
    if not clean_title or not clean_instruction:
        return "title 和 instruction 都不能为空 —— 说清「做什么、交付什么」。"

    thread_id = (ctx.thread_id if ctx else "") or ""
    if not thread_id:
        # 没有会话归属的任务在系统里无处安放：进度通道按 thread_id 广播，
        # 审批 key 也按它定位。这里明确拒绝，而不是派出去让它变成孤儿。
        return "这次派发缺少会话归属（thread_id），已中止 —— 请直接在这里把话说清楚。"

    # ★ 并发上限按「这个会话还有几个在跑」算，而不是全局：
    # 用户一口气说五件事时，第六件应该在这轮讲清楚，而不是悄悄全派出去 ——
    # 每个任务都在烧一次模型，而他自己也看不过来。
    try:
        running = task_store.active(thread_id=thread_id, limit=MAX_ACTIVE_PER_THREAD + 1)
    except Exception as e:  # noqa: BLE001
        return f"（暂时读不到任务列表，先不派发：{e}）"
    if len(running) >= MAX_ACTIVE_PER_THREAD:
        return (
            f"这个会话已经有 {len(running)} 个任务在跑（上限 {MAX_ACTIVE_PER_THREAD}）。"
            "先等其中一个结束，或用 task_cancel 停掉不需要的，再派新的。"
        )

    registry = get_registry()
    # 与 delegate 那一路同一套收敛（见 `supervisor.dispatch_node`）：
    # 认不出的能力名退兜底专员，已停用的专员派不到。
    agent = registry.resolve_capability(capability)
    unknown = bool(capability) and capability.strip() not in registry.capability_map()

    record = executor.start(
        thread_id=thread_id,
        agent=agent,
        title=clean_title,
        instruction=clean_instruction,
        # 本轮选项从上下文继承（主对话那条路带着；缺省按「都开着」）
        web=bool(ctx.extras.get("web_enabled", True)) if ctx else True,
        graph=bool(ctx.extras.get("graph_enabled", True)) if ctx else True,
        model=str(ctx.extras.get("model") or "") if ctx else "",
        employee_id=(ctx.employee_id if ctx else "") or "",
        # ★ 权限档同样要继承。`task_dispatch` 正是「这一步需要批准 → 派到后台
        # 走审批」那条指路的目标（见 `supervisor.respond_node` 的拒绝话术）：
        # 如果派出去的任务不带着本轮的档位，「只读模式」就会变成一个
        # 只要绕道后台就能出得去的门 —— 而那道门是模型自己开的。
        permission_mode=(ctx.permission_mode if ctx else "") or "",
        # 工作区也继承派发那一轮的根目录（`ToolContext.workspace_root` 就是它）。
        # 不继承的话，「把这个目录里的文件都过一遍」派到后台就换成了沙箱 ——
        # 前台看得到、后台看不到，而两边都觉得自己做对了。
        workdir_root=str(ctx.workspace_root or "") if ctx else "",
        # ★ 来源必须写实：`/api/tasks` 的默认口径按它筛（施工图 §4.3）。
        # 少这一维，任务会落进「旧记录」那一档，界面上看不出任何异常。
        source=SOURCE_SUPERVISOR,
    )
    if record is None:
        # ★ 谎报「已派发」是这里最坏的结局：用户会等一个永远不会出现的成果。
        return "派发没有成功（后台任务功能不可用或已关闭）。请直接在这里处理这件事，或如实告诉用户稍后再试。"

    task_id = str(record.get("task_id") or "")
    event = {"type": "subtask", "task_id": task_id, "agent": agent, "title": clean_title, "total": 1}
    # 两条通道各管一件事：SSE 只在这轮对话活着时有效，
    # 进度通道负责用户关掉页面再回来时仍然看得见这次派发。
    if ctx:
        ctx.emit(event)
    task_bus.publish(thread_id, event)

    lines = [f"已派发：「{clean_title}」→ 专员 {agent}（task_id={task_id}）"]
    if unknown:
        # 不在这里重抄能力清单：那份表在系统提示里（`planner_capability_lines`），
        # 再复制一遍就会有两份各说各话的版本。
        lines.append(f"（能力名「{capability}」系统不认识，已交给兜底专员 {agent}；可用能力见你的能力清单。）")
    lines.append("★ 结果**不会**出现在这一轮对话里 —— 它还在后台跑。")
    lines.append("现在告诉用户这件事已经开始做了，稍后用 task_status(task_id) 或 tasks_list 查进度与结果。")
    return "\n".join(lines)


@agent_tool(
    display_name="取消后台任务",
    description="取消一个还在跑的后台任务（用户说「别跑了」「停掉那个」时）。只能取消本会话的任务。",
    category="编排",
    danger_level="moderate",
    allowed_agents=["supervisor"],
    health_handler=_health,
)
def task_cancel(task_id: str, ctx: ToolContext | None = None) -> str:
    """
    Args:
        task_id: 要取消的任务 id（用 tasks_list 拿）
    """
    from app.tasks.executor import executor

    tid = (task_id or "").strip()
    if not tid:
        return "需要 task_id。先用 tasks_list 看是哪个任务。"
    try:
        record = task_store.get(tid)
    except Exception as e:  # noqa: BLE001
        return f"（暂时读不到任务 {tid}：{e}）"
    if record is None:
        return f"找不到任务 {tid}。"

    thread_id = ctx.thread_id if ctx else ""
    if thread_id and str(record.get("thread_id") or "") != thread_id:
        return f"任务 {tid} 不属于当前会话，不能替你取消它。"

    status = str(record.get("status") or "")
    # ★ 先落状态、再断协程（`executor.abort` 的注释解释了为什么两个都要调）：
    # 只改索引 = 界面显示已取消，而模型继续被调用、token 继续烧。
    if not task_store.cancel(tid):
        return f"任务 {tid} 已经是终态了（{_STATUS_TEXT.get(status, status)}），无需取消。"
    aborted = False
    try:
        aborted = executor.abort(tid)
    except Exception as e:  # noqa: BLE001
        logger.warning("终止任务协程失败（状态已置为 cancelled）: %s", e)
    return (
        f"已取消「{record.get('title') or tid}」。"
        + ("（正在跑的那条已终止。）" if aborted else "（它已经不在运行了，只把状态改成已取消。）")
    )


def build_prompt_block(thread_id: str = "", *, limit: int = MAX_INJECTED_TASKS) -> str:
    """
    构造注入 system prompt 的「当前后台任务」块。

    ★ 三条硬约束（与长期记忆注入同源，见不变量 27）
    1. 只注入**未结束**的 + 最近 5 条（已完成的只在用户问起时用工具查）
    2. 硬截断 500 字符
    3. 必带「以本次查询为准」的出口 —— 注入块可能已经过时了，
       而模型会当真。**没有出口的话，模型会把过时的状态当成事实说出来。**

    ★ 例外：**未汇报**的已完成任务也注入（最多 3 条，只给标题）
    任务跑完了、汇报还没送达（或送达前用户先开口）时，用户追问
    「我那个报告好了吗」—— 如果 prompt 里只有「未结束」的任务，
    模型会直接说「没有查到」。给一行存在性提示，正文仍靠 task_status 查
    （正文进 prompt 会挤爆上下文 —— 与「工具是真相来源」同一条取舍）。

    空任务 / 存储不可用 → 返回空字符串（调用方直接拼接即可）。
    """
    try:
        records = task_store.active(thread_id=thread_id, limit=limit)
        finished = task_store.list(
            thread_id=thread_id,
            status=STATUS_SUCCEEDED,
            limit=3,
            sources=("", SOURCE_SUPERVISOR),
        )[0]
    except Exception as e:  # noqa: BLE001
        logger.debug("读取后台任务失败（已忽略）: %s", e)
        return ""

    finished = [
        r for r in finished if not str(r.get("reported_at") or "") and not str(r.get("employee_id") or "")
    ]
    if not records and not finished:
        return ""

    lines = ["【当前后台任务】"]
    for r in records:
        st = str(r.get("status") or "")
        lines.append(f"- {r.get('title') or '未命名任务'}（{_STATUS_TEXT.get(st, st)}）")
    for r in finished:
        lines.append(f"- {r.get('title') or '未命名任务'}（已完成，结果用 task_status 查看）")
    lines.append("以上信息可能已过时，以 tasks_list / task_status 的返回为准。")

    block = "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:
        # ★ 截断时**必须**保留出口那一行 ——
        # 把它截掉的话，模型会把「以为还有 3 个任务在跑」当成事实说出来。
        tail = "…（已截断）\n以上信息可能已过时，以 tasks_list / task_status 的返回为准。"
        block = block[: MAX_BLOCK_CHARS - len(tail)] + tail
    return block


__all__ = ["build_prompt_block", "task_cancel", "task_dispatch", "task_status", "tasks_list"]
