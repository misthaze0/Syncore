"""
内置工具 —— Shell 执行 (shell)

⚠️ danger 级工具：执行前必须经 HITL 人工审批。

★ 分层防御（审批不是唯一的防线）

    第 1 层：HITL 审批        —— 人来判断「这条命令该不该跑」
    第 2 层：命令黑名单        —— 审批通过也拒绝明显破坏性的命令
    第 3 层：路径判定          —— 命令里出现的每个路径都必须落在授权根内
    第 4 层：环境变量隔离      —— 子进程拿不到 .env 里的密钥
    第 5 层：超时 + 输出落盘   —— 防止挂死与上下文爆炸

为什么审批之外还要有第 2~5 层：
用户点「批准」时看到的是一行命令字符串，而模型完全可以在一个看似无害的命令里
夹带副作用（比如 `python -c "import os; os.remove(...)"`）。
审批的正确语义是「授权执行这条命令」，而不是「把机器的控制权交给模型」。
所以真正的边界必须由系统强制，不能外包给用户的一次点击。

★ 第 3 层为什么从正则改成「解析路径再判定」

旧实现用 `../` / `cd ..` / `~/` 这类文本特征拦越界。它和新的目录授权模型
天然冲突：授权目录内的合法命令常常带 `..`（比如 `cd ..\\docs`），而真正危险的
`python -c "open('../.env')"` 反而不含这些特征。现在改为把命令里**看起来像路径
的 token** 抽出来，相对会话 cwd 解析成绝对路径，逐个比对授权根 —— 判定跟着
`AGENT_ALLOWED_DIRS` 走，不再靠猜写法。

★ 关于 `create_subprocess_shell`

它会把命令交给系统 shell 执行，因此支持 `&&`、管道、重定向 —— 这是 Agent
跑脚本的真实需求，但也是注入面的来源。这里的选择是
「保留能力 + 用白名单/黑名单/环境隔离把风险压到可控」，而不是禁用 shell。

★ 会话状态：cwd 与后台任务

主流 Agent 的 shell 工具是**有状态的**（`cd` 之后下一条命令仍在同一目录，
长命令能丢后台跑再回来收）。原先每条命令一个全新进程 + 固定 cwd，模型必须
每轮重写完整路径，长任务里极易出错。会话状态按 `thread_id` 隔离，
所以两个对话不会互相踩对方的 cwd 与后台任务。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.agents.tools.context import ToolContext
from app.agents.tools.decorator import agent_tool
from app.config import settings
from app.core.sandbox import boundary_exempt, writable_roots
from app.core.subproc import safe_env

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30

# 超时上限：即使调用方（或模型）传入更大的值也不放行，防止长时间占住一个 worker
HARD_TIMEOUT_MAX = 300
#: 后台任务最长存活时间，超时后输出仍可读取但进程被回收
BACKGROUND_TTL_SECONDS = 1800

# ---------- 第 2 层：高危命令黑名单（即便审批通过也拒绝执行） ----------
_BLOCKED_PATTERNS = [
    # ---------- 破坏性删除 / 磁盘操作 ----------
    r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rf][a-zA-Z]*\s+/(?:\s|$)",
    r"\brm\s+-rf\b",
    r"\bformat\b\s+[a-zA-Z]:",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+if=.*of=/dev/",
    r":\(\)\s*\{.*\}\s*;\s*:",                    # fork bomb
    r"\bdel\s+/[sfq]\b.*[a-zA-Z]:\\",
    r"\bRemove-Item\b.*-Recurse.*\b[a-zA-Z]:\\",
    # ---------- 系统控制 ----------
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\binit\s+0\b",
    r"\bdiskpart\b",
    # ---------- 权限 / 账户篡改 ----------
    r"\bchmod\s+(-R\s+)?777\s+/",
    r"\bchown\s+(-R\s+)?\S+\s+/",
    r"\bnet\s+user\b.*\/add",
    r"\buseradd\b",
    # ---------- 凭据窃取 ----------
    r"\bcat\s+.*\.env\b",
    r"\btype\s+.*\.env\b",
    r"\bprintenv\b",
    r"\benv\s*\|\s*grep\b",
]
_BLOCKED_RE = re.compile("|".join(_BLOCKED_PATTERNS), re.IGNORECASE)

# 系统敏感目录（Windows + POSIX 常见路径）
_SYSTEM_PATH_RE = re.compile(
    r"(?:[a-zA-Z]:\\?(?:windows|program files(?: \(x86\))?|programdata|appdata)\b)"
    r"|(?:/(?:etc|bin|sbin|boot|sys|proc|dev|root|var/lib)\b)",
    re.IGNORECASE,
)

# ---------- 第 3 层：命令里的路径 token ----------
# 看起来像路径：含分隔符、以点/波浪号开头、或带盘符。纯单词（python、git）不在此列。
_PATHISH_RE = re.compile(
    r"""^[~./\\]|[A-Za-z]:[\\/]|[/\\]|[.][.]/|\.env$|/\w+/\w+""",
)
#: shell 语法本身会用到这些 token，别把它们当路径
_TOKEN_NOISE = {"|", "||", "&&", ";", ">", ">>", "<", "2>&1", "$", "(", ")", "{", "}"}

# ---------- 第 4 层：子进程环境变量白名单 ----------
# 定义搬到了 `app/core/subproc.py`：钩子与终端要用同一份，
# 两份白名单迟早漂移，而漂移的代价是密钥进了子进程。


# ---------- 会话状态（按 thread 隔离） ----------
@dataclass
class ShellJob:
    """一个后台任务"""

    job_id: str
    command: str
    proc: asyncio.subprocess.Process
    started_at: float
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    exit_code: int | None = None
    finished_at: float | None = None
    #: 环形缓冲从头部丢弃的字节数（输出超过上限时发生，读取时如实告知）
    dropped_bytes: int = 0


@dataclass
class ShellSession:
    cwd: Path = field(default_factory=lambda: Path(settings.sandbox_path).resolve())
    #: 这个 `cwd` 是从哪一个根目录起算的（`None` = 沙箱默认根）。
    #: 单独记一位是为了能认出「用户换了工作区」—— 见 `_session`。
    anchored_to: Path | None = None
    jobs: dict[str, ShellJob] = field(default_factory=dict)


_SESSIONS: dict[str, ShellSession] = {}


def _session(ctx: ToolContext | None) -> ShellSession:
    key = (ctx.session_key if ctx else "") or "default"
    sess = _SESSIONS.get(key)
    root = Path(ctx.workspace_root).resolve() if ctx and ctx.workspace_root else None
    if sess is None:
        sess = ShellSession()
        sess.anchored_to = root
        sess.cwd = (root or Path(settings.sandbox_path).resolve()) if ctx else sess.cwd
        _SESSIONS[key] = sess
        return sess

    # ★ 换工作区要把 shell 拽回新根目录
    # 不重锚的症状是「用户选了 B 项目，说『看看目录里有什么』，shell 还在 A 项目里 ls」，
    # 而它给出的那份清单看起来完全真实 —— 这是最难发现的一类错。
    # 只在**根变了**的时候动，不在每次调用时动：`cd` 是用户/模型自己走的位置，
    # 一个会话里连着几条命令必须停在同一个目录里（不变量：shell 的会话级状态）。
    if sess.anchored_to != root:
        sess.anchored_to = root
        sess.cwd = root or Path(settings.sandbox_path).resolve()
    return sess


def _in_roots(target: Path, roots: list[Path]) -> bool:
    return any(target == root or root in target.parents for root in roots)


def check_command(command: str, cwd: Path | None = None) -> str | None:
    """
    命令安全校验。

    Returns:
        None 表示通过；否则返回拒绝原因（可直接作为工具结果返回给模型）
    """
    cmd = (command or "").strip()
    if not cmd:
        return "命令为空"

    if _BLOCKED_RE.search(cmd):
        return f"该命令命中高危黑名单，禁止执行：{command}"

    if match := _SYSTEM_PATH_RE.search(cmd):
        return (
            f"该命令引用了系统目录 '{match.group(0)}'，已被拦截：{command}"
        )

    # `full` 权限模式解除目录限制，但**不解除**上面两层：
    # 破坏性命令黑名单与系统目录仍然拦 —— 「完全访问」说的是
    # 她的电脑就是你的电脑，不包括把系统格式化。
    if not boundary_exempt():
        anchor = cwd or Path(settings.sandbox_path).resolve()
        roots = writable_roots()
        for token in _path_tokens(cmd):
            candidate = _resolve_token(token, anchor)
            if candidate is None:
                continue
            if not _in_roots(candidate, roots):
                return (
                    f"该命令涉及授权目录之外的路径 '{token}'（解析为 {candidate}），已被拦截。\n"
                    "允许的范围：\n" + "\n".join(f"  - {r}" for r in roots)
                    + "\n（需要更多目录请让用户加进 AGENT_ALLOWED_DIRS，"
                    "或把 PERMISSION_MODE 配置为 full；命令里请用相对路径）"
                )
    return None


def _path_tokens(cmd: str) -> list[str]:
    """从命令里抽出看起来像路径的 token（尽力而为，配合第 1~2 层与审批兜底）"""
    try:
        parts = shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError:
        parts = cmd.split()

    out: list[str] = []
    for tok in parts:
        if tok in _TOKEN_NOISE or tok.startswith("-"):
            continue
        # 去掉常见的重定向/引号残留
        cleaned = tok.strip("'\"`").lstrip(">><")
        if not cleaned or "=" in cleaned.split("/")[0]:      # VAR=value 不是路径
            continue
        if _PATHISH_RE.search(cleaned):
            out.append(cleaned)
    return out


def _resolve_token(token: str, anchor: Path) -> Path | None:
    try:
        p = Path(os.path.expandvars(os.path.expanduser(token)))
        resolved = (p if p.is_absolute() else anchor / p).resolve()
    except (OSError, RuntimeError):
        return None
    # 通配符不是单个路径（`*.md` 会解析成目录本身），跳过
    if any(ch in token for ch in "*?[]"):
        return None
    return resolved


def _clamp_timeout(timeout: object) -> int:
    try:
        value = int(timeout)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(1, min(value, HARD_TIMEOUT_MAX))


def _format_output(command: str, code: int | None, out: str, err: str, elapsed: float) -> str:
    parts = [f"$ {command}", f"退出码: {code if code is not None else '未知'}  耗时: {elapsed:.1f}s"]
    if out.strip():
        parts.append(f"--- stdout ---\n{out.strip()}")
    if err.strip():
        parts.append(f"--- stderr ---\n{err.strip()}")
    if not out.strip() and not err.strip():
        parts.append("（无输出）")
    return "\n".join(parts)


# ---------- bash —— danger ----------
@agent_tool(
    display_name="执行本机命令",
    description=(
        "执行 shell 命令并返回输出（危险操作，需要用户审批）。"
        "同一个会话里 cwd 会保持，`cd` 之后下一条命令仍在该目录；"
        "耗时长的命令用 run_in_background=true 丢后台，之后用 bash_output 收结果。"
    ),
    category="终端",
    danger_level="danger",
    allowed_agents=["pc_agent", "software_agent"],
)
async def bash(
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
    workdir: str = "",
    run_in_background: bool = False,
    ctx: ToolContext | None = None,
) -> str:
    """在授权目录内执行 shell 命令，返回退出码与输出。

    Args:
        command: 要执行的命令（支持管道与 &&）
        timeout: 前台等待的超时秒数，上限 300；后台任务不受它约束
        workdir: 本条命令的工作目录，留空则沿用会话当前目录
        run_in_background: 是否丢后台执行，立即返回 job_id
    """
    sess = _session(ctx)
    rejection = check_command(command, sess.cwd)
    if rejection:
        logger.warning("bash 拒绝执行: %s", rejection)
        return f"[已拒绝] {rejection}"

    anchor = sess.cwd
    if workdir:
        try:
            from app.core.sandbox import resolve_agent_path

            anchor = resolve_agent_path(workdir, for_write=True)
        except (PermissionError, ValueError) as e:
            return f"[已拒绝] 工作目录不可用：{e}"

    # `cd X` 的语义是「切换会话目录」，交给 shell 跑只会得到一个无输出的进程
    if (cd_target := _leading_cd_target(command)) is not None:
        return _apply_cd(sess, cd_target, anchor)

    if run_in_background:
        return await _start_background(sess, command, anchor)

    secs = _clamp_timeout(timeout)
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(anchor),
        env=safe_env(),
    )
    start = time.perf_counter()
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=secs)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[超时] 命令执行超过 {secs}s 已被终止：{command}"

    out = (stdout or b"").decode("utf-8", errors="replace")
    err = (stderr or b"").decode("utf-8", errors="replace")
    return _format_output(command, proc.returncode, out, err, time.perf_counter() - start)


def _leading_cd_target(command: str) -> str | None:
    """`cd x` / `cd x && ...` 的首个目标目录；不是以 cd 开头则返回 None"""
    stripped = (command or "").strip()
    if not stripped.startswith("cd ") and not stripped.startswith("cd\t"):
        return None
    rest = stripped[2:].strip()
    head = re.split(r"&&|\|\||;|\n", rest, maxsplit=1)
    return head[0].strip().strip('"').strip("'") or None


def _apply_cd(sess: ShellSession, target: str, anchor: Path) -> str:
    candidate = _resolve_token(target, anchor)
    if candidate is None:
        return f"[已拒绝] 无法解析目标目录：{target}"
    if not boundary_exempt() and not _in_roots(candidate, writable_roots()):
        return f"[已拒绝] 目标目录不在授权范围内：{target}"
    if not candidate.is_dir():
        return f"[错误] 目录不存在：{target}"
    sess.cwd = candidate
    return f"已切换到 {candidate}"


async def _start_background(sess: ShellSession, command: str, anchor: Path) -> str:
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(anchor),
        env=safe_env(),
    )
    job = ShellJob(job_id=job_id, command=command, proc=proc, started_at=time.time())
    sess.jobs[job_id] = job
    _reap_old_jobs(sess)

    async def _drain(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > 2_000_000:          # 后台任务也要有界
                overflow = len(buf) - 1_500_000
                del buf[:overflow]
                job.dropped_bytes += overflow

    asyncio.create_task(_drain(proc.stdout, job.stdout))
    asyncio.create_task(_drain(proc.stderr, job.stderr))

    async def _watch() -> None:
        job.exit_code = await proc.wait()
        job.finished_at = time.time()

    asyncio.create_task(_watch())
    return (
        f"已转入后台执行（job_id={job_id}）：{command}\n"
        f"用 bash_output(job_id='{job_id}') 查看进度与结果，用 bash_kill 可终止它。"
    )


def _reap_old_jobs(sess: ShellSession) -> None:
    """清掉超过 TTL 的后台任务对象（进程还在跑的也终止，避免泄漏）"""
    cutoff = time.time() - BACKGROUND_TTL_SECONDS
    for job_id, job in list(sess.jobs.items()):
        if job.started_at < cutoff:
            if job.proc.returncode is None:
                try:
                    job.proc.kill()
                except ProcessLookupError:
                    pass
            sess.jobs.pop(job_id, None)


# ---------- bash_output —— safe ----------
@agent_tool(
    display_name="查看命令输出",
    description="查看后台 bash 任务的当前输出与状态（运行中/已结束/退出码）。job_id 来自 bash 的后台返回。",
    category="终端",
    danger_level="safe",
    read_only=True,
    allowed_agents=["pc_agent", "software_agent"],
)
async def bash_output(job_id: str = "", ctx: ToolContext | None = None) -> str:
    """
    Args:
        job_id: 后台任务 id；留空列出本会话全部后台任务
    """
    sess = _session(ctx)
    _reap_old_jobs(sess)
    if not job_id:
        if not sess.jobs:
            return "本会话没有后台任务。"
        lines = [
            f"- {j.job_id}  {j.command[:60]}  "
            f"{'运行中' if j.exit_code is None else f'退出码 {j.exit_code}'}"
            for j in sess.jobs.values()
        ]
        return "后台任务：\n" + "\n".join(lines)

    job = sess.jobs.get(job_id.strip())
    if job is None:
        return f"[错误] 找不到后台任务 {job_id}（可能已被回收）。"

    out = job.stdout.decode("utf-8", errors="replace")
    err = job.stderr.decode("utf-8", errors="replace")
    status = "运行中" if job.exit_code is None else f"已结束（退出码 {job.exit_code}）"
    lines = [f"[{status}] job_id={job_id}"]
    if job.exit_code is None:
        lines.append("（任务仍在运行，以下是目前为止的输出）")
    if job.dropped_bytes:
        lines.append(
            f"（输出超过缓冲上限，最早的约 {job.dropped_bytes} 字节已被丢弃，以下为最新保留部分）"
        )
    body = _format_output(
        job.command, job.exit_code, out, err, time.time() - job.started_at
    )
    # 完整输出交给统一出口：超长时由 finalize_text 落盘并可 read 续读，不在这里硬切
    return "\n".join(lines) + "\n" + body


# ---------- bash_kill —— moderate ----------
@agent_tool(
    display_name="停止后台命令",
    description="终止一个后台 bash 任务。用户说「别跑了 / 停掉那个」或任务卡住时使用。",
    category="终端",
    danger_level="moderate",
    allowed_agents=["pc_agent", "software_agent"],
)
async def bash_kill(job_id: str, ctx: ToolContext | None = None) -> str:
    """
    Args:
        job_id: 要终止的后台任务 id
    """
    sess = _session(ctx)
    job = sess.jobs.get((job_id or "").strip())
    if job is None:
        return f"[错误] 找不到后台任务 {job_id}。"
    if job.exit_code is not None:
        return f"任务 {job_id} 已经结束（退出码 {job.exit_code}），无需终止。"
    try:
        job.proc.kill()
        await job.proc.wait()
    except ProcessLookupError:
        return f"任务 {job_id} 已经不存在了。"
    job.exit_code = job.proc.returncode
    job.finished_at = time.time()
    return f"已终止任务 {job_id}。用 bash_output 还能看到它终止前的输出。"

def _health() -> bool:
    """健康检查：沙箱目录存在即可执行命令"""
    return Path(settings.sandbox_path).resolve().is_dir()


__all__ = [
    "DEFAULT_TIMEOUT",
    "HARD_TIMEOUT_MAX",
    "bash",
    "bash_kill",
    "bash_output",
    "check_command",
]
