"""
Hooks 执行器 —— 跑脚本、读退出码、把结果收敛成一句「收紧了的判定」

★ 退出码约定（与主流 agent 软件一致，故意不做成配置文件里可选的东西）

| 退出码 | 含义 | 之后发生什么 |
| :--- | :--- | :--- |
| 0 | 正常 | 若 stdout 是 JSON，按 `decision` 采纳；否则不表态 |
| 2 | **拦下这一步** | 视为 deny，stderr 第一行作为理由回给用户与模型 |
| 其它 / 超时 | 钩子自己坏了 | **不阻塞**，只记留痕 |

第三条是这个模块最重要的决定：钩子失败绝不能变成「她什么都不做了」。
一个脚本因为路径写错而退出 1，症状就该是「这一条没生效」，
而不是整条对话卡住 —— 后者会让用户把钩子功能整体判定为坏的，
然后把总开关关掉，连带着那条真正在保护她的 deny 钩子一起失效。

★ 钩子只能收紧

stdout 里写 `{"decision":"allow"}` 会被**丢弃**（记留痕，不采纳）。
放行意味着「不用再问用户」，而这个决定只能由用户自己在设置里做。
如果脚本可以放行，那么任何一个能被模型影响到内容的路径
（比如钩子读了模型写的临时文件）都会变成提权入口。

★ 参数走 stdin，绝不拼进命令行

见 `spec.py` 模块注释。命令串在写下来那一刻就是最终形态，
payload 只能通过标准输入进去 —— 这条决定了「工具参数里带分号」会不会变成注入。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.core.subproc import safe_env
from app.hooks import logs
from app.hooks.spec import HOOK_EVENTS, PRE_TOOL, POST_TOOL, PROMPT_SUBMIT, NOTIFICATION, HookSpec, hook_store

logger = logging.getLogger(__name__)

#: stdout 里能表态的三个值（其它值一律视为「没说话」）
_BLOCK_WORDS = ("block", "deny", "reject")
_ASK_WORDS = ("ask", "prompt")
_ALLOW_WORDS = ("allow", "approve")

_STDERR_REASON_CHARS = 300
_STDOUT_CONTEXT_CHARS = 1200

#: `session` 里恒定出现的五个键。缺的补空串，调用方多给的保留。
#: ★ 为什么要补：脚本里 `payload["session"]["employee_id"]` 不该因为
#: 「这条路径刚好没有员工」而 KeyError —— 那种崩法的退出码是 1，
#: 在界面上表现为「这条钩子没生效」，用户完全看不出是哪一句代码坏了。
SESSION_KEYS: tuple[str, ...] = ("thread_id", "task_id", "run_id", "employee_id", "agent")


@dataclass
class HookOutcome:
    """一次事件里所有钩子合起来的意见"""

    #: `deny` / `ask` / 空串（不表态）。永远不可能是 `allow`。
    decision: str = ""
    reason: str = ""
    #: 要补进模型上下文的话（PostToolUse / UserPromptSubmit 用）
    context: List[str] = field(default_factory=list)
    #: 要显示给用户的一句话（不进模型）
    notice: List[str] = field(default_factory=list)
    ran: int = 0
    failed: int = 0
    #: 被丢弃的 allow 表态条数 —— 界面要如实说明「钩子想放行但被忽略」
    ignored_allow: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "context": list(self.context),
            "notice": list(self.notice),
            "ran": self.ran,
            "failed": self.failed,
            "ignored_allow": self.ignored_allow,
        }


def _first_line(text: str, limit: int) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def _parse_stdout(raw: str) -> Dict[str, Any]:
    """
    解析钩子的 stdout，产出 `{decision, reason, context, notice}`。

    四个字段各有各的去处，**不要合并**：`reason` 是给判定用的（它只在有
    decision 时才有意义），`notice` 是给用户看的，`context` 是给模型看的。
    曾经把 `message` 折进 `reason`，结果是「脚本明明说话了，界面什么都不显示」
    —— 因为 `reason` 在没有 decision 时会被判定层丢掉。

    不是 JSON 就当「说了句人话」：首行作为 notice 显示给用户。
    这一条让「先 echo 一句提醒」这种最朴素的用法不必写 JSON。
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"notice": _first_line(text, _STDOUT_CONTEXT_CHARS)}
    if not isinstance(payload, dict):
        return {"notice": _first_line(text, _STDOUT_CONTEXT_CHARS)}

    decision = str(
        payload.get("permissionDecision")
        or payload.get("decision")
        or payload.get("behavior")
        or ""
    ).strip().lower()
    return {
        "decision": decision,
        "reason": str(payload.get("reason") or "").strip(),
        "notice": str(payload.get("systemMessage") or payload.get("message") or "").strip(),
        "context": str(payload.get("additionalContext") or payload.get("context") or "").strip(),
    }


async def _run_one(hook: HookSpec, payload: Dict[str, Any]) -> Tuple[int, str, str, float, bool]:
    """
    执行一个钩子。返回 (退出码, stdout, stderr, 耗时秒, 是否超时)。

    ⚠️ 超时后必须真的把进程杀掉：`wait_for` 取消的是「等待」，
    子进程不会因此消失。留一个活着的孤儿进程，用户会在任务管理器里
    看到一排不知道哪来的脚本。
    """
    started = time.perf_counter()
    stdin = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    timed_out = False
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_shell(
            hook.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(settings.sandbox_path),
            env=safe_env(),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin), timeout=hook.timeout_seconds
        )
    except asyncio.TimeoutError:
        timed_out = True
        if proc is not None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                pass
        stdout, stderr = b"", f"钩子超时（>{hook.timeout_seconds}s）".encode("utf-8")
    except (OSError, ValueError) as e:
        # 起都起不来（命令不存在、PATH 里没有）也是「钩子自己坏了」，同样不阻塞
        return -1, "", f"无法启动钩子：{e}", time.perf_counter() - started, False

    code = -1 if timed_out else int(proc.returncode if proc is not None else -1)
    return (
        code,
        (stdout or b"").decode("utf-8", errors="replace"),
        (stderr or b"").decode("utf-8", errors="replace"),
        time.perf_counter() - started,
        timed_out,
    )


def _interpret(hook: HookSpec, code: int, stdout: str, stderr: str) -> Dict[str, Any]:
    """把退出码 + stdout 收敛成一条统一的表态"""
    if code == 2:
        return {"decision": "deny", "reason": _first_line(stderr, _STDERR_REASON_CHARS) or "自定义钩子拦下了这一步"}
    if code != 0:
        return {"decision": "", "reason": "", "error": _first_line(stderr, _STDERR_REASON_CHARS) or f"退出码 {code}"}

    parsed = _parse_stdout(stdout)
    word = parsed.get("decision", "")
    reason = parsed.get("reason", "")
    notice = parsed.get("notice", "")

    if word in _BLOCK_WORDS:
        return {"decision": "deny", "reason": reason or "自定义钩子拦下了这一步",
                "notice": notice, "context": parsed.get("context", "")}
    if word in _ASK_WORDS:
        return {"decision": "ask", "reason": reason or "自定义钩子要求这一步由你确认",
                "notice": notice, "context": parsed.get("context", "")}
    if word in _ALLOW_WORDS:
        # 见模块注释：钩子不能替用户放行
        return {"decision": "", "reason": "", "notice": notice,
                "context": parsed.get("context", ""), "ignored_allow": True}

    # 没表态，或 decision 是个认不出来的值 —— **不猜**用户想干什么，
    # 但也不能让他写的话消失：没有 notice 就用 reason 顶上。
    if not notice:
        notice = reason or (f"钩子的表态「{word[:40]}」无法识别，已忽略。" if word else "")
    return {"decision": "", "reason": "", "notice": notice, "context": parsed.get("context", "")}


def build_payload(
    event: str,
    *,
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    session: Optional[Dict[str, Any]] = None,
    permission_mode: str = "",
    prompt: str = "",
    answer: str = "",
    message: str = "",
    reason: str = "",
    tool_response: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    五个事件的 stdin JSON 形状 —— **只有这一处定义**（不含 `hook_event_name`，
    那个键由 `fire()` / `run_once()` 加盖，因为「这次是什么事件」是触发方的事实）。

    ★ 为什么值得集中成一个函数

    界面「试跑」发的那份 JSON 必须和真跑时一字不差，否则「试跑通过」是一句假话：
    脚本在试跑时读 `tool_response.result` 拿到了值，真跑时那个键叫 `output`，
    症状就是「明明测过了，一到真用上就没反应」。散在五个调用点各写一遍字面量，
    一定会有某一天只改了其中一处。

    ★ 字段名照主流约定（`tool_name` / `tool_input` / `permission_mode` 那一套）

    用户会抄现成的钩子脚本来自定义行为，自创字段名的代价是每份抄来的脚本都得改。
    """
    if event not in HOOK_EVENTS:
        raise ValueError(f"未知的钩子事件: {event}")
    sess = {k: "" for k in SESSION_KEYS}
    sess.update({str(k): str(v or "") for k, v in (session or {}).items()})
    if event in (PRE_TOOL, POST_TOOL):
        body: Dict[str, Any] = {
            "tool_name": tool_name,
            "tool_input": dict(args or {}),
            "session": sess,
            "permission_mode": permission_mode,
        }
        if event == POST_TOOL:
            body["tool_response"] = tool_response or {}
        return body
    if event == PROMPT_SUBMIT:
        return {"prompt": prompt, "session": sess}
    if event == NOTIFICATION:
        return {
            "message": message,
            "reason": reason,
            "tool_name": tool_name,
            "session": sess,
        }
    return {"session": sess, "answer": answer}


async def run_once(hook: HookSpec, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    界面「试跑」：执行**一个**钩子并把原始结果返回。

    ⚠️ 它真的会跑那条命令 —— 试跑不是模拟。所以总开关关着时也允许试跑
    （用户正是靠试跑决定要不要打开），但结果只回给界面，
    **不会**改变任何权限判定，也照样写留痕（标 `test=true`）。

    `hook_event_name` 用的是这条钩子**自己声明的事件**而不是 "Test"：
    脚本常常按事件分支，试跑要复现的正是那条分支。是不是试跑由 `test` 字段区分。
    """
    code, stdout, stderr, duration, timed_out = await _run_one(
        hook, {"hook_event_name": hook.event, **payload}
    )
    verdict = _interpret(hook, code, stdout, stderr)
    logs.append_run({
        "event": hook.event,
        "hook_id": hook.hook_id,
        "command": hook.command[:200],
        "tool_name": str(payload.get("tool_name") or ""),
        "exit_code": code,
        "timed_out": timed_out,
        "duration_ms": int(duration * 1000),
        "decision": verdict.get("decision") or None,
        "reason": (verdict.get("reason") or verdict.get("error") or "")[:_STDERR_REASON_CHARS],
        "notice": (verdict.get("notice") or "")[:_STDOUT_CONTEXT_CHARS],
        "test": True,
    })
    return {
        "exit_code": code,
        "timed_out": timed_out,
        "duration_ms": int(duration * 1000),
        "stdout": stdout[:4000],
        "stderr": stderr[:2000],
        "verdict": verdict,
    }


async def fire(
    event: str,
    payload: Dict[str, Any],
    *,
    tool_name: str = "",
) -> HookOutcome:
    """
    触发一个事件的所有钩子（按配置顺序串行执行）。

    Args:
        event: `spec.HOOK_EVENTS` 之一
        payload: 传给脚本的 JSON（stdin）。调用方负责不放凭据进去。
        tool_name: 工具类事件用来匹配 `hook.tool` 通配

    Returns:
        `HookOutcome`；总开关关闭时是全零结果，**且不执行任何进程**
    """
    hooks = hook_store.for_event(event, tool_name)
    outcome = HookOutcome()
    if not hooks:
        return outcome

    base = {"hook_event_name": event, **payload}
    for hook in hooks:
        code, stdout, stderr, duration, timed_out = await _run_one(hook, base)
        verdict = _interpret(hook, code, stdout, stderr)
        outcome.ran += 1

        decision = verdict.get("decision", "")
        if decision == "deny":
            # 先到先得：后面的钩子没必要再改「这一步被拦」这个结论
            outcome.decision = "deny"
            outcome.reason = verdict.get("reason") or outcome.reason
        elif decision == "ask" and outcome.decision != "deny":
            outcome.decision = "ask"
            outcome.reason = verdict.get("reason") or outcome.reason

        if verdict.get("context"):
            outcome.context.append(str(verdict["context"])[:_STDOUT_CONTEXT_CHARS])
        if verdict.get("notice"):
            outcome.notice.append(str(verdict["notice"])[:_STDOUT_CONTEXT_CHARS])
        if verdict.get("ignored_allow"):
            outcome.ignored_allow += 1
            outcome.notice.append(
                f"钩子「{hook.describe()}」申请放行，但钩子不能替你放行 —— 已忽略。"
            )
        if verdict.get("error"):
            outcome.failed += 1
            logger.warning(
                "钩子执行异常（不阻塞主流程）: event=%s hook=%s %s",
                event, hook.hook_id, verdict["error"],
            )

        logs.append_run({
            "event": event,
            "hook_id": hook.hook_id,
            "command": hook.command[:200],
            "tool_name": tool_name,
            "exit_code": code,
            "timed_out": timed_out,
            "duration_ms": int(duration * 1000),
            "decision": decision or None,
            "reason": (verdict.get("reason") or verdict.get("error") or "")[:_STDERR_REASON_CHARS],
            "notice": (verdict.get("notice") or "")[:_STDOUT_CONTEXT_CHARS],
        })

    # PreToolUse 之外的事件不该产出 deny —— 拦不拦由判定层决定，
    # 这里只是「没有地方可以拦」。收口在这里，免得调用方各写一遍判断。
    if event != PRE_TOOL and outcome.decision == "deny":
        outcome.notice.append(outcome.reason)
        outcome.decision = ""
        outcome.reason = ""
    return outcome


async def fire_tool_pre(
    *,
    tool_name: str,
    args: Dict[str, Any],
    session: Dict[str, Any],
    permission_mode: str,
) -> HookOutcome:
    """`PreToolUse`：判定层在弹窗之前问一次用户的脚本"""
    return await fire(
        PRE_TOOL,
        build_payload(
            PRE_TOOL,
            tool_name=tool_name,
            args=args,
            session=session,
            permission_mode=permission_mode,
        ),
        tool_name=tool_name,
    )


__all__ = ["HookOutcome", "build_payload", "fire", "fire_tool_pre", "run_once"]
