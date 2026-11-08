# -*- coding: utf-8 -*-
"""
定时任务的数据契约与「下一次什么时候跑」的计算。

★ 与后台子任务（`tasks/models.py`）的关系：两级对象，各一套状态机

- **ScheduleRecord（定义）**：用户配的「每天 09:00 做 X」。可编辑、可停用，
  形态上更接近 `hooks/spec.py`（用户配置），所以它没有状态机 ——
  `enabled` 是布尔而不是状态。
- **RunRecord（一次触发）**：到点之后发生的事。它的生命周期
  （running → succeeded/failed/cancelled/orphaned）复用子任务那套词表，
  因为**一次运行就是一个后台子任务**（`tasks/executor.py`），
  两边状态天然同构。词表在这里独立定义而不是 import 子任务的 ——
  `pending` / `waiting_approval` 属于子任务自己，运行记录不该出现它们。

★ 为什么「下一次时间」是落盘字段（`next_run_at`）而不是每次现算

调度循环每个 tick 只做一件事：`next_run_at <= now` 就触发。
如果每个 tick 都要解析 cron / 星期 / 月份再算「现在到点没有」，
解析逻辑就成了调度器正确性的依赖 —— 一个表达式解析抛错，
表现是**这个任务永远不再跑**（每个 tick 都算不出它到点）。
落盘 `next_run_at` 之后，解析错误只会在**创建/编辑的那一次**暴露出来
（接口层直接报 400），调度循环本身笨到不会坏。

代价是要守住一个不变量：**任何改动 freq / start_date / end_date / enabled
的路径都必须重算 `next_run_at`**。重算收在 `store` 的写方法里，
调用方拿不到「改了 freq 但没重算」的口子。

★ 为什么自己写 cron 而不加 croniter

这个项目的依赖纪律（见 pyproject 里 PyYAML 那段注释）：自己 import 的
必须自己声明，但**能不 import 就不 import**。cron 的「五分字段 +
`*/a-b/列表` 子集」是 ~60 行的纯计算，而引入 croniter 换来的是
dow=0/7 双语义、`#`/`L`/`?` 这些**界面根本配不出来**的语法。
调度语义以界面能配出的 freq 种类为准，cron 只是其中一种输入。
"""

from __future__ import annotations

import calendar
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional, TypedDict

# ---------- id ----------


def make_schedule_id() -> str:
    """生成 schedule_id。16 位 hex：与 task/turn id 同一风格，
    它直接变成文件名（`data/schedules/<id>.jsonl`）与 URL 路径段。"""
    return uuid.uuid4().hex[:16]


def is_valid_schedule_id(value: str) -> bool:
    """校验 schedule_id。★ 复用会话的正则而不是复制一份
    （与 `tasks/models.py::is_valid_task_id` 同一条理由：两份正则必然漂移）。"""
    from app.conversations.store import is_valid_thread_id

    return is_valid_thread_id(value)


def make_run_id() -> str:
    return uuid.uuid4().hex[:16]


# ---------- 运行记录的状态 ----------

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_ORPHANED = "orphaned"  # 进程重启时还没结算的运行（当时的协程已随旧进程没了）

RUN_ALL_STATUSES = frozenset({RUN_RUNNING, RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED, RUN_ORPHANED})
RUN_TERMINAL_STATUSES = frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED, RUN_ORPHANED})

#: 合法迁移表 —— 理由与 `tasks/models.py::_ALLOWED_TRANSITIONS` 完全相同：
#: 非法迁移要在写它的那一行炸出来，而不是在界面上表现为「这次运行永远在转圈」。
_RUN_ALLOWED: dict[str, frozenset[str]] = {
    RUN_RUNNING: frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED, RUN_ORPHANED}),
    RUN_SUCCEEDED: frozenset(),
    RUN_FAILED: frozenset(),
    RUN_CANCELLED: frozenset(),
    RUN_ORPHANED: frozenset(),
}


class IllegalRunTransition(RuntimeError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"非法的运行状态迁移: {current} → {target}")
        self.current = current
        self.target = target


def run_is_terminal(status: str) -> bool:
    return status in RUN_TERMINAL_STATUSES


#: 触发方式。`manual` = 界面上的「立即运行一次」—— 它同样消耗模型，
#: 但值得单独标出来：用户事后翻记录时要能分清「到点自己跑的」和「我点的」。
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"

#: 定时触发跑什么：`chat` = 派一个后台子任务给专员（Phase 9 原有行为），
#: `workflow` = 跑一次用户画好的工作流。两种运行共用本文件的调度/状态/运行记录，
#: 分叉只在 `schedules/runner.dispatch` 一处 —— 调度时间语义没有理由跟着变。
KIND_CHAT = "chat"
KIND_WORKFLOW = "workflow"
SCHEDULE_KINDS = (KIND_CHAT, KIND_WORKFLOW)

#: 派发给子任务时写入 `TaskRecord.source` 的出处标记。
#: 后台任务页按 source 分屏（chat/workspace），定时任务派生的子任务是第三种 ——
#: 它的默认屏归属见 `api/schedules` 与前端 TasksPage 的 scope 口径。
SOURCE_SCHEDULE = "schedule"

#: 运行记录里结果摘要的字符上限。
#: 全文在落盘的会话轮次里（`thread_id` 能翻出来），这里只留列表页够读的一截。
RUN_SUMMARY_CHARS = 2000


# ---------- 调度规格（freq） ----------

FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
FREQ_MONTHLY = "monthly"
FREQ_INTERVAL = "interval"
FREQ_CRON = "cron"

FREQ_KINDS = (FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY, FREQ_INTERVAL, FREQ_CRON)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_hhmm(text: str) -> Optional[tuple[int, int]]:
    m = _TIME_RE.match((text or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _parse_date(text: str) -> Optional[date]:
    if not text or not _DATE_RE.match(text.strip()):
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def validate_freq(freq: dict[str, Any]) -> str:
    """校验一份调度规格；返回错误信息（空串 = 合法）。

    ★ 校验只在这一个函数里做。接口层、存储层都调它 ——
    「每天 09:00」配错成「每天 25:00」如果能在落盘时溜过去，
    症状是两年后有人发现这个任务从来没跑过。
    """
    if not isinstance(freq, dict):
        return "调度规格必须是对象"
    kind = str(freq.get("kind") or "")
    if kind not in FREQ_KINDS:
        return f"不支持的调度类型: {kind or '（空）'}"
    if kind == FREQ_CRON:
        try:
            parse_cron(str(freq.get("expr") or ""))
        except ValueError as e:
            return str(e)
        return ""
    if kind == FREQ_INTERVAL:
        try:
            n = int(freq.get("every_days") or 0)
        except (TypeError, ValueError):
            return "间隔天数必须是整数"
        if not 1 <= n <= 366:
            return "间隔天数需在 1~366 之间"
        if _parse_hhmm(str(freq.get("time") or "")) is None:
            return "时间格式应为 HH:MM"
        return ""
    # daily / weekly / monthly 都要有合法的 time
    if _parse_hhmm(str(freq.get("time") or "")) is None:
        return "时间格式应为 HH:MM"
    if kind == FREQ_WEEKLY:
        raw = freq.get("days")
        if not isinstance(raw, list) or not raw:
            return "每周至少要选一天"
        try:
            days = {int(d) for d in raw}
        except (TypeError, ValueError):
            return "星期取值非法"
        if not days or not all(0 <= d <= 6 for d in days):
            return "星期取值需在 0~6（周一到周日）"
        return ""
    if kind == FREQ_MONTHLY:
        try:
            dm = int(freq.get("day") or 0)
        except (TypeError, ValueError):
            return "日期必须是整数"
        if not 1 <= dm <= 31:
            return "日期需在 1~31 之间（当月没有该日时顺延到当月最后一天）"
        return ""
    return ""


def parse_cron(expr: str) -> tuple[frozenset[int], frozenset[int], frozenset[int], frozenset[int], frozenset[int]]:
    """解析五分字段 cron（分 时 日 月 周），返回五个取值集合。

    支持子集：`*` `*/n` `a` `a-b` `a-b/n` 以及逗号列表；数值按 **0=周一**
    （与界面词表一致，见 `describe_freq`）。不认 `?` `L` `#` ——
    界面配不出它们，认了只会多一条「表达式合法但没人能配出来」的裂缝。
    """
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError("cron 表达式需要 5 个字段（分 时 日 月 周）")
    bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    out: list[frozenset[int]] = []
    for raw, (lo, hi) in zip(parts, bounds):
        vals: set[int] = set()
        for token in raw.split(","):
            if not token:
                raise ValueError(f"cron 字段里有空项: {expr!r}")
            step = 1
            body = token
            if "/" in token:
                body, _, step_s = token.partition("/")
                try:
                    step = int(step_s)
                except ValueError as e:
                    raise ValueError(f"cron 步长非法: {token!r}") from e
                if step < 1:
                    raise ValueError(f"cron 步长非法: {token!r}")
            if body in ("*", ""):
                a, b = lo, hi
            elif "-" in body:
                a_s, _, b_s = body.partition("-")
                try:
                    a, b = int(a_s), int(b_s)
                except ValueError as e:
                    raise ValueError(f"cron 区间非法: {token!r}") from e
            else:
                try:
                    a = int(body)
                except ValueError as e:
                    raise ValueError(f"cron 取值非法: {token!r}") from e
                b = a if step == 1 else hi
            if a < lo or b > hi or a > b:
                raise ValueError(f"cron 取值越界: {token!r}（应在 {lo}~{hi}）")
            vals.update(range(a, b + 1, step))
        if not vals:
            raise ValueError(f"cron 字段解析为空: {expr!r}")
        out.append(frozenset(vals))
    return out[0], out[1], out[2], out[3], out[4]


def _next_cron(expr: str, after: datetime) -> Optional[datetime]:
    minutes, hours, doms, months, dows = parse_cron(expr)
    cursor = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    # 上限 ~ 两年（525600+ 分钟）：`0 0 30 2 *` 这类永不成立的表达式会走到这里返回 None，
    # 由调用方（validate_freq 已挡过语法，剩下的只有语义性不可能）报「算不出下一次」。
    for _ in range(525_600):
        if cursor.month not in months:
            cursor = _start_of_next_month(cursor)
            continue
        dom_hit = cursor.day in doms
        dow_hit = _cron_dow(cursor) in dows
        dom_restricted = doms != frozenset(range(1, 32))
        dow_restricted = dows != frozenset(range(0, 7))
        if dom_restricted and dow_restricted:
            day_ok = dom_hit or dow_hit  # 标准 cron：日/周都收窄时取**并集**
        else:
            day_ok = dom_hit and dow_hit
        if not day_ok:
            cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cursor.hour not in hours:
            cursor = (cursor + timedelta(hours=1)).replace(minute=0)
            continue
        if cursor.minute not in minutes:
            cursor = cursor + timedelta(minutes=1)
            continue
        # 整条搜索都是「墙上时钟」语义：直接贴上起算点的 tz 即可，
        # 跨夏令时的一小时偏移对分钟级调度不构成错误（桌面单机也不切时区）。
        return cursor.replace(tzinfo=after.tzinfo)
    return None


def _start_of_next_month(dt: datetime) -> datetime:
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0)


def _cron_dow(dt: datetime) -> int:
    """datetime.weekday()（0=周一）就是本项目的 cron 周序，收在一处防止漂移。"""
    return dt.weekday()


def _next_by_kind(freq: dict[str, Any], after: datetime) -> Optional[datetime]:
    """算「本类型下 strictly 晚于 `after` 的下一个时刻」。

    ★ 返回值一律带 `after` 的 tzinfo：naive/aware 混比抛 TypeError，
    而这个错只会在「跨月/顺延/清窗」这类低频分支里炸 —— 全模块统一 aware，
    从根上不给它出现的机会。
    """
    tz = after.tzinfo
    kind = str(freq.get("kind") or "")
    if kind == FREQ_CRON:
        return _next_cron(str(freq.get("expr") or ""), after)
    hm = _parse_hhmm(str(freq.get("time") or "09:00")) or (9, 0)
    if kind == FREQ_DAILY:
        cand = after.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if cand <= after:
            cand += timedelta(days=1)
        return cand
    if kind == FREQ_WEEKLY:
        days = {int(d) for d in (freq.get("days") or [])}
        for i in range(8):
            cand = (after + timedelta(days=i)).replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if cand > after and cand.weekday() in days:
                return cand
        return None
    if kind == FREQ_MONTHLY:
        want = int(freq.get("day") or 1)
        for i in range(25):  # 最多向后找 24 个月，覆盖「2 月没有 30 日」的顺延
            y = after.year + (after.month - 1 + i) // 12
            m = (after.month - 1 + i) % 12 + 1
            day = min(want, calendar.monthrange(y, m)[1])  # ★ 顺延到当月最后一天
            cand = datetime(y, m, day, hm[0], hm[1], tzinfo=tz)
            if cand > after:
                return cand
        return None
    if kind == FREQ_INTERVAL:
        every = max(1, int(freq.get("every_days") or 1))
        anchor = date.fromisoformat(str(freq.get("anchor_date") or "")) if freq.get("anchor_date") else after.date()
        delta_days = (after.date() - anchor).days
        k = max(0, delta_days // every)
        for bump in range(3):  # 今天到点但已过时刻 → 下一档；循环兜住边界
            cand = datetime.combine(anchor + timedelta(days=(k + bump) * every), datetime.min.time())
            cand = cand.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0, tzinfo=tz)
            if cand > after:
                return cand
        return None
    return None


def compute_next_run(
    freq: dict[str, Any],
    *,
    start_date: str = "",
    end_date: str = "",
    after: Optional[datetime] = None,
) -> str:
    """算下一次执行时间（ISO 字符串）；窗口内不再有则返回空串（=「暂无后续执行」）。

    ⚠️ 空串是**合法终态**而不是错误：任务没被删、也没被停用，只是日期窗走完了。
    界面上要如实显示「暂无后续执行」，而不是悄悄不再跑。
    """
    now = (after or datetime.now().astimezone())
    if now.tzinfo is None:
        now = now.astimezone()
    tz = now.tzinfo
    start_d = _parse_date(start_date)
    end_d = _parse_date(end_date)
    start_at = (
        datetime.combine(start_d, datetime.min.time(), tzinfo=tz) - timedelta(microseconds=1)
        if start_d else None
    )
    cursor = now if (start_d is None or now.date() >= start_d) else start_at
    for _ in range(400):  # 跳过落在非法日的候选（正常 freq 用不到第二轮，防御死循环）
        nxt = _next_by_kind(freq, cursor)
        if nxt is None:
            return ""
        if end_d and nxt.date() > end_d:
            return ""
        if start_at is not None and nxt <= start_at:
            cursor = start_at
            continue
        return nxt.isoformat(timespec="seconds")
    return ""


_WEEK_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def describe_freq(freq: dict[str, Any]) -> str:
    """把调度规格压成界面上一眼能读的人话（「每天 09:00」「每周一、周三 08:30」）。

    ★ 由后端派生而不是前端拼：中文语序里「每月最后一天」这类规则放前端，
    就出现「后端加一种 kind、前端显示成 kind 原名」的静默劣化。
    前端只渲染这一句 + 日期窗。
    """
    if not isinstance(freq, dict):
        return ""
    kind = str(freq.get("kind") or "")
    time_s = str(freq.get("time") or "")
    if kind == FREQ_DAILY:
        return f"每天 {time_s}"
    if kind == FREQ_WEEKLY:
        try:
            days = sorted({int(d) for d in (freq.get("days") or [])})
        except (TypeError, ValueError):
            days = []
        names = "、".join(_WEEK_CN[d] for d in days if 0 <= d <= 6)
        return f"每周{('（' + names + '）') if names else ''} {time_s}".strip()
    if kind == FREQ_MONTHLY:
        return f"每月 {freq.get('day', '?')} 日 {time_s}"
    if kind == FREQ_INTERVAL:
        return f"每 {freq.get('every_days', '?')} 天 {time_s}"
    if kind == FREQ_CRON:
        return f"cron: {freq.get('expr', '')}".strip()
    return str(freq.get("kind") or "")


# ---------- 记录形状 ----------


class ScheduleRecord(TypedDict, total=False):
    """一个定时任务的完整记录（index.json 里的一项）。

    ★ 可选字段「没有就是键不存在」（与 TaskRecord 同一条铁律）：
    `next_run_at` 缺 = 还没算；空串 = 算过了且窗口内不再执行 —— 两者不同义。
    """

    schedule_id: str
    title: str
    #: `chat`（缺省，旧记录都是它）/ `workflow`
    kind: str
    #: kind=workflow 时绑定的工作流 id
    workflow_id: str
    #: 到点喂给专员的完整指令。与子任务的 instruction 同一取舍：
    #: 界面列表只渲染 title，但详情/编辑要能看回原文，所以接口**带**它。
    #: kind=workflow 时它是工作流的输入（`{{input}}`），允许为空。
    instruction: str
    freq: dict[str, Any]
    #: 日期窗（YYYY-MM-DD，含当天）。空 = 不限。
    start_date: str
    end_date: str
    enabled: bool
    created_at: str
    updated_at: str
    #: ---- 运行时选项（派发时冻结进子任务，理由同 TaskRecord 的同名字段） ----
    #: 能力名（可空 = 兜底专员）。见 `agents/registry.resolve_capability`。
    agent: str
    model: str
    web_enabled: bool
    graph_enabled: bool
    permission_mode: str
    #: 绑定的授权工作区 id（空 = 沙箱默认目录）。存 id 而不是路径：
    #: 目录被改名后按 id 还能找回新路径，按路径只能报「目录不存在」。
    workdir_id: str
    #: ---- 调度与最近一次运行的派生快照（列表页一次读全，不逐条翻运行文件） ----
    next_run_at: str
    last_run_at: str
    last_status: str
    last_thread_id: str
    last_task_id: str


class RunRecord(TypedDict, total=False):
    """定时任务的一次运行记录（`<schedule_id>.jsonl` 里的一条 run 事件）。"""

    run_id: str
    schedule_id: str
    #: 本次运行新建的会话 —— ★ 这是文件卡片能下载的前提：
    #: 运行结果会落一轮进会话历史，产出的 .md 走
    #: `/api/conversations/{thread_id}/files/*` 现成的四个接口，不另开下载路径。
    thread_id: str
    task_id: str
    #: kind=workflow 时对应的 `workflows` 运行 id（task_id 为空）
    workflow_run_id: str
    trigger: str
    status: str
    started_at: str
    finished_at: str
    result: str
    error: str
    files: list[dict[str, str]]
    usage: dict[str, int]


def new_schedule_record(
    *,
    schedule_id: str,
    title: str,
    instruction: str,
    freq: dict[str, Any],
    start_date: str = "",
    end_date: str = "",
    enabled: bool = True,
    kind: str = KIND_CHAT,
    workflow_id: str = "",
    agent: str = "",
    model: str = "",
    web_enabled: bool = True,
    graph_enabled: bool = True,
    permission_mode: str = "",
    workdir_id: str = "",
) -> ScheduleRecord:
    from app.conversations.models import now_iso

    now = now_iso()
    return ScheduleRecord(
        schedule_id=schedule_id,
        title=(title or "").strip(),
        kind=kind if kind in SCHEDULE_KINDS else KIND_CHAT,
        workflow_id=(workflow_id or "").strip(),
        instruction=(instruction or "").strip(),
        freq=dict(freq),
        start_date=(start_date or "").strip(),
        end_date=(end_date or "").strip(),
        enabled=bool(enabled),
        created_at=now,
        updated_at=now,
        agent=(agent or "").strip(),
        model=(model or "").strip(),
        web_enabled=bool(web_enabled),
        graph_enabled=bool(graph_enabled),
        permission_mode=(permission_mode or "").strip(),
        workdir_id=(workdir_id or "").strip(),
        next_run_at=(
            compute_next_run(freq, start_date=start_date, end_date=end_date) if enabled else ""
        ),
    )


def record_from_dict(data: dict[str, Any]) -> Optional[ScheduleRecord]:
    """从索引还原一条记录；**不信任磁盘上的内容**（与 tasks 同一约定）。"""
    sid = str(data.get("schedule_id") or "")
    if not is_valid_schedule_id(sid):
        return None
    freq = data.get("freq")
    if not isinstance(freq, dict) or validate_freq(freq):
        return None
    kind = str(data.get("kind") or KIND_CHAT)
    if kind not in SCHEDULE_KINDS:
        return None
    workflow_id = str(data.get("workflow_id") or "").strip()
    if kind == KIND_WORKFLOW:
        from app.workflows.models import is_valid_workflow_id

        if not is_valid_workflow_id(workflow_id):
            return None
    record: ScheduleRecord = ScheduleRecord(
        schedule_id=sid,
        title=str(data.get("title") or ""),
        kind=kind,
        instruction=str(data.get("instruction") or ""),
        freq=freq,
        enabled=bool(data.get("enabled", True)),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
    )
    if workflow_id:
        record["workflow_id"] = workflow_id
    for key in ("start_date", "end_date", "agent", "model", "permission_mode",
                "workdir_id", "next_run_at", "last_run_at", "last_status",
                "last_thread_id", "last_task_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            record[key] = value  # type: ignore[literal-required]
    for key in ("web_enabled", "graph_enabled"):
        value = data.get(key)
        if isinstance(value, bool):
            record[key] = value  # type: ignore[literal-required]
    return record


def run_from_dict(data: dict[str, Any]) -> Optional[RunRecord]:
    """从 jsonl 的一行还原一次运行；id 不合法直接丢弃（它会进 URL 与文件名）。"""
    run_id = str(data.get("run_id") or "")
    sid = str(data.get("schedule_id") or "")
    if not is_valid_schedule_id(run_id) or not is_valid_schedule_id(sid):
        return None
    status = str(data.get("status") or "")
    if status not in RUN_ALL_STATUSES:
        return None
    record: RunRecord = RunRecord(
        run_id=run_id,
        schedule_id=sid,
        status=status,
        trigger=str(data.get("trigger") or TRIGGER_SCHEDULED),
        started_at=str(data.get("started_at") or ""),
    )
    for key in ("thread_id", "task_id", "workflow_run_id", "finished_at", "result", "error"):
        value = data.get(key)
        if isinstance(value, str) and value:
            record[key] = value  # type: ignore[literal-required]
    files = data.get("files")
    if isinstance(files, list) and files:
        record["files"] = [f for f in files if isinstance(f, dict)]  # type: ignore[literal-required]
    usage = data.get("usage")
    if isinstance(usage, dict) and usage:
        clean = {str(k): int(v) for k, v in usage.items() if isinstance(v, (int, float))}
        if clean:
            record["usage"] = clean  # type: ignore[literal-required]
    return record


def truncate_summary(text: str, limit: int = RUN_SUMMARY_CHARS) -> str:
    if len(text or "") <= limit:
        return text or ""
    return (text or "")[:limit] + "…"
