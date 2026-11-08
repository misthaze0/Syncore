"""
子 Agent 通用运行时 —— 规划 (planner) + 工具执行 (tool_executor)

关键设计：一次 superstep 只处理一个工具调用

为什么这么做:
LangGraph 的`interrupt()` 在恢复时会让节点从头重放，如果一次执行多个工具会导致重复副作用。因此流程被拆成：
planner  →（有待执行工具）→ tool_executor →（还有剩余）→ tool_executor → ... → planner
- planner 负责一次 LLM 决策，产出「待执行工具队列」，自身不调用 interrupt ，因此不会重放
- tool_executor 每次只消费队列头的一个调用，最多触发一次 interrupt
- 这样每次重放都只会把同一个工具执行一次，且 interrupt 与结果一一对应

同时因为「N 个工具调用必须 N 个 ToolMessage 齐了才能再调 LLM」
路由在队列未清空时直接回到 tool_executor ，避免出现不完整的 tool_calls 消息块
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from langchain_core.runnables import RunnableConfig

from langchain_core.messages import ToolMessage
from langgraph.types import interrupt

from app.agents.approval_flow import record_policy_outcome
from app.agents.context import (
    build_context,
    is_context_overflow_error,
    shape_tool_result,
)
from app.agents.emitter import get_emitter
from app.agents.state import PlannerInput, ToolExecutorInput
from app.agents.tools.context import context_from_state
from app.agents.tools.registry import tool_registry
from app.config import settings
from app.core.llm_factory import create_llm
from app.core.prompts import RAG_SYSTEM_PROMPT, SUB_AGENT_PROMPT
from app.hooks import POST_TOOL, PRE_TOOL, build_payload, fire, fire_tool_pre
from app.observability import get_current_recorder, now_ms
from app.usage import PURPOSE_SUBAGENT, record_usage
from app.computer_control import assistant_control_active
from app.permissions.policy import (
    PermissionDecision,
    SOURCE_ASSISTANT,
    SOURCE_HOOK,
    SOURCE_MODE,
    SOURCE_RULE,
    decide,
    mode_store_default,
)
from app.schemas.tools import ToolResult

logger = logging.getLogger(__name__)

# 「没有人点过按钮但也放行了」的三种依据。
# 为什么要单独列出来：这三处都要进审批留痕（否则审计看不出这一步是谁允许的），
# 但又都**不能**记成「人工批准」（`approved=True`）—— 用户没做过这个决定。
# 两处判断（写留痕、写 record）必须用同一份清单，否则会出现
# 「留痕里有记录、状态里 approved 却是 True」这种自相矛盾的账。
AUTO_APPROVE_SOURCES = (SOURCE_MODE, SOURCE_RULE, SOURCE_HOOK, SOURCE_ASSISTANT)


# ---------- 追踪埋点（LLM 用量只有这一层拿得到） ----------
# 为什么不从 SSE 事件流里统计 token：
# token 事件是按增量文本推的，拿不到模型返回的真实 usage；
# 而 usage_metadata 只在流式聚合后的 AIMessage 上，也就是这里的 `acc`。
# 所以这一段必须就地埋点。全部门槛由 get_current_recorder() 兜住，
# 未启用追踪时一次函数调用就返回 None，无额外开销。
def _extract_usage(message: Any) -> dict[str, int]:
    """
    从 AIMessage 中提取 token 用量。

    两个来源：LangChain 标准化的 `usage_metadata`（input/output/total_tokens）
    与网关原样透传的 `response_metadata.token_usage`（prompt/completion_tokens）。
    部分 OpenAI 兼容网关只给后者，所以两条路都要试。
    """
    if message is None:
        return {}

    raw = getattr(message, "usage_metadata", None) or {}
    if not raw:
        raw = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
    if not raw:
        return {}

    prompt = raw.get("input_tokens", raw.get("prompt_tokens"))
    completion = raw.get("output_tokens", raw.get("completion_tokens"))
    total = raw.get("total_tokens")

    usage: dict[str, int] = {}
    if isinstance(prompt, (int, float)):
        usage["prompt_tokens"] = int(prompt)
    if isinstance(completion, (int, float)):
        usage["completion_tokens"] = int(completion)
    if isinstance(total, (int, float)):
        usage["total_tokens"] = int(total)
    elif usage:
        usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)

    # 思考型模型（qwen3.x 等）的「内部推理」token 计数。
    # 它已经包含在 completion_tokens 里，单独拎出来只是为了让界面能说清
    # 「这些 token 里有多少花在了你看不见的思考上」——
    # 这是首字延迟十几秒最直观的解释，也是本机实测唯一能拿到的思考信息
    # （该网关不透出 `reasoning_content` 正文，只在 usage 里给一个计数）。
    details = raw.get("output_token_details") or raw.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning", details.get("reasoning_tokens"))
    if isinstance(reasoning, (int, float)):
        usage["reasoning_tokens"] = int(reasoning)
    return usage


def _emit_usage(
    emit: Any,
    agent_name: str,
    message: Any,
    *,
    llm: Any = None,
    purpose: str = PURPOSE_SUBAGENT,
    step: int = 0,
    duration_ms: float = 0.0,
    status: str = "ok",
) -> dict[str, int]:
    """
    一次 LLM 调用的用量收尾：推给事件流 + 落进用量账本。

    ★ 为什么用量必须走事件流，而不是写进图状态

    用量是「旁路观测数据」，不是图的业务状态：写进 `AgentState` 会让
    Checkpointer 每轮多存一份、还要为它定义 reducer。而 `emit` 本来就是
    节点对外的**唯一通道**（Phase 4 的追踪埋点、Phase 7 的标题生成都借它），
    顺手带上最轻。外层的 `emit`（`graph.py`）会把这个内部信号**吞掉**
    （不进队列、不进 SSE），只用来给收尾的 `done` 事件补一个 `usage` 字段 ——
    和 `done.files` 是同一套「已有事件上加可选字段」的做法。

    多次 LLM 调用（多 Agent / 工具循环）会 emit 多次，外层负责累加。

    ★ 为什么同时在这里落账

    账本要的是**每一次调用**（哪个模型、花多少），而 `done.usage` 是**整轮累加值** ——
    一轮里用了两个模型时，累加值再也分不出各自多少。所以账本必须埋在调用点。
    两条路数据源相同（同一个 `_extract_usage(message)`），不会各记一套。
    """
    usage = _extract_usage(message)
    if usage:
        emit({"_internal": "usage", "usage": usage, "agent": agent_name})
    record_usage(
        model=_model_of(llm),
        usage=usage,
        agent=agent_name,
        purpose=purpose,
        step=step,
        duration_ms=duration_ms,
        status=status,
    )
    return usage


def _model_of(llm: Any) -> str:
    """
    取这次调用**真正用的**模型名。

    只能从 LLM 实例上读：`create_llm` 已经把「专员配置的模型 > 全局默认」
    收敛成一个具体值，而调用点各自传的参数形态不同（有的传 None 走默认），
    在调用点重新推一遍必然和 `create_llm` 的实现漂移。
    """
    return str(getattr(llm, "model_name", "") or getattr(llm, "model", "") or "") or settings.LLM_MODEL


def _span_duration(span: Any) -> float:
    """已关闭 span 的耗时（追踪未启用时 span 是个空对象，给 0 即可）"""
    return float(getattr(span, "duration_ms", 0.0) or 0.0)


def _tools_for_agent(agent_name: str, state: Any, allowed_names: list[str] | None = None) -> list[Any]:
    """
    取该 Agent 可用的工具，并按**本轮选项**收窄。

    ★ 「关掉联网」的正确实现是不给工具，不是改 prompt 劝模型别搜

    工具治理（`allowed_agents`）本来就是现成的机制，用户开关只是它之上的一层
    收窄。在 prompt 里写「请不要联网」是会被模型无视的（它已经拿着那个工具），
    而不给它 `web_search` 是结构性的 —— 想调也调不到。

    ★ `allowed_names`（数字员工工具白名单）

    在 `base_agent` 的默认权限之上再收窄一层：白名单非空时，只保留名单内的工具。
    `bind_tools` 是硬约束 —— 不在名单里的工具模型根本调不到。
    """
    tools = tool_registry.get_tools_for_agent(
        agent_name, web_enabled=bool(state.get("web_enabled", True))
    )
    if allowed_names is not None:
        allowed = set(allowed_names)
        tools = [t for t in tools if getattr(t, "name", "") in allowed]
    return tools


def _trace_llm_start(agent_name: str, system_prompt: str, message_count: int, *, llm: Any = None) -> Any:
    rec = get_current_recorder()
    if rec is None:
        return None
    return rec.start_span(
        "llm",
        kind="generation",
        input={"agent": agent_name, "message_count": message_count},
        agent=agent_name,
        # 实例上的真实模型名，而不是 `settings.LLM_MODEL` —— 专员可以各自配模型，
        # 记全局默认会让链路里的模型名和实际调用的模型对不上。
        model=_model_of(llm),
        # Prompt 长度是判断「上下文是否被撑爆」最直接的指标，
        # 比 token 数更早可得（不必等模型返回）
        system_prompt_chars=len(system_prompt),
    )


def _trace_llm_end(
    span: Any,
    answer: str,
    acc: Any,
    *,
    first_token_ms: float | None,
    chunk_count: int,
    error: str | None = None,
) -> None:
    if span is None:
        return
    rec = get_current_recorder()
    if rec is None:
        return
    rec.end_span(
        span,
        output=answer,
        status="error" if error else "ok",
        error=error,
        usage=_extract_usage(acc),
        # 首字延迟是流式体验的核心指标：思考型模型（qwen3.x-flash 等）
        # 会先产出一大段 reasoning_content，首字延迟可达十几秒
        first_token_ms=first_token_ms,
        chunks=chunk_count,
        output_chars=len(answer),
    )


# ---------- 工具函数 ----------
def _chunk_text(chunk: Any) -> str:
    """兼容 str / content-blocks 两种 content 形态，提取纯文本增量"""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _message_text(message: Any) -> str:
    """提取完整消息的文本内容"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b if isinstance(b, str) else b.get("text", "")
            for b in content
            if isinstance(b, (str, dict))
        )
    return str(content or "")


def _subtask_scope(state: dict[str, Any], config: RunnableConfig, agent_name: str) -> str:
    """
    子任务的摘要作用域。

    ★ 必须带 task_id：同一会话里两次「查资料」是两件事，
    共用一份摘要会让第二次任务「记得」第一次的用户诉求。
    """
    thread_id = str(state.get("thread_id") or "")
    task_id = str(state.get("task_id") or "")
    if task_id:
        return f"sub:{thread_id or 'thread'}:{task_id}"
    # 兜底：子图 checkpointer 的 thread_id 就是 `subtask_<task_id>`，同样唯一
    cfg_thread = str((config.get("configurable") or {}).get("thread_id") or "")
    return f"sub:{thread_id or cfg_thread or 'thread'}:{agent_name}"


def _is_approved(decision: Any) -> bool:
    """归一化审批结果：支持 bool / {"approved": bool} / "yes" 等形式"""
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, dict):
        return bool(decision.get("approved", False))
    if isinstance(decision, str):
        return decision.strip().lower() in {"true", "yes", "y", "1", "approve", "approved"}
    return False


def _source_label(source: str) -> str:
    """判定来源 → 界面用的一句「按什么放行的」"""
    return {
        SOURCE_MODE: "权限模式",
        SOURCE_RULE: "权限规则",
        SOURCE_HOOK: "自定义钩子",
        SOURCE_ASSISTANT: "助理电脑操控",
    }.get(source, "权限设置")


def _refuse(
    state: ToolExecutorInput,
    updates: dict[str, Any],
    call: dict[str, Any],
    *,
    emit: Callable[[dict[str, Any]], None],
    call_id: str,
    tool_name: str,
    reason: str,
    model_note: str,
    user_rejected: bool = False,
) -> dict[str, Any]:
    """
    构造「这个工具没有执行」的节点返回。

    ★ 拒绝与被拦共用一条路径是有意的
    两者在**执行语义上完全相同**（没跑、要给模型一句能纠正的话、要发 `tool_result`），
    差别只在留痕里「由谁决定」—— 而那件事已经由 `record_policy_outcome`
    在判定处写好了，不该由这里的分支决定。
    分成两套写的后果是：某一套忘了发 `tool_result`，界面上就会出现
    「工具卡片永远转圈」，而审批面板上一条记录都没有。

    ⚠️ 仍然**不能**在这里写审批留痕（不变量 36）。
    """
    emit({
        "type": "tool_result",
        "call_id": call_id,
        "tool_name": tool_name,
        "tool_label": tool_registry.display_name_for(tool_name),
        "result": reason,
        "success": False,
        # 结构化标记：桌面助手据此区分「权限/审批拒绝」和普通工具失败，
        # 不能靠匹配中文文案（文案会改，语音提示不该跟着坏）。
        "denied": True,
        "duration_ms": 0,
    })
    return {
        "pending_calls": updates.get("pending_calls") or [],
        "current_call": None,
        "tool_calls": list(state.get("tool_calls") or []) + [{
            **call,
            "success": False,
            "result": reason,
            "duration_ms": 0,
            "approved": False if user_rejected else None,
            "blocked": not user_rejected,
        }],
        "approval_log": [
            *list(state.get("approval_log") or []),
            *([{"call_id": call_id, "tool_name": tool_name, "approved": False}]
              if user_rejected else []),
        ],
        "pending_approval": None,
        "messages": [ToolMessage(content=model_note, tool_call_id=call_id)],
    }


# ---------- 两条执行循环共用的判定入口 ----------
async def decide_tool_call(
    *,
    emit: Callable[[dict[str, Any]], None],
    call_id: str,
    tool_name: str,
    args: dict[str, Any],
    danger_level: str,
    mode: str,
    session: dict[str, str],
    run_id: str = "",
) -> PermissionDecision:
    """
    跑 PreToolUse 钩子 → `policy.decide()` → 把「没人点按钮但也放行了」记进留痕。

    ★ 子图与主对话必须共用这一个入口
    两条循环的**处置**确实不同：只有子图有 interrupt 点，能真的弹窗问用户
    （主图在节点中途挂起会把已执行的工具重放第二遍）。
    但「这一步该不该做」只有一份答案。各写一遍的下场是规则与钩子只在一条路上生效 ——
    最典型的一种：用户在设置页加了 deny，模型从主对话照样能跑，
    因为主对话那条还在看 `danger_level`。

    ⚠️ 必须在任何副作用**之前**调用：钩子是真的会执行脚本的。

    Args:
        session: 身份字典（`thread_id` / `task_id` / `employee_id` / `agent`），
                 进钩子的 stdin payload 与留痕。只能是状态里带出来的值，
                 不能让模型填（不变量：运行时身份不是工具参数）。
    """
    from app.permissions.policy import effective_mode
    mode = effective_mode(mode)
    pre = await fire_tool_pre(
        tool_name=tool_name, args=args, session=session, permission_mode=mode
    )
    for notice in pre.notice:
        emit({"type": "hook_notice", "content": notice, "call_id": call_id,
              "tool_name": tool_name, "tool_label": tool_registry.display_name_for(tool_name),
              "event": PRE_TOOL})

    decision = decide(
        tool_name,
        args,
        danger_level=danger_level,
        mode=mode,
        hook_behavior=pre.decision,
        hook_reason=pre.reason,
        # 「助理 + 总开关 + 桌面工具」三者都成立时，桌面操作不再逐次弹窗。
        # 判定仍走同一函数：deny 规则与钩子拒绝依旧生效（见 policy.decide）。
        assistant_control=assistant_control_active(
            session.get("thread_id", ""), tool_name
        ),
    )

    # 没有人点过按钮的放行（规则 / 模式 / 钩子）也要进留痕，并如实告诉界面。
    # 复用 `approval_auto` 这个事件类型：它在前端语义就是「不弹窗、只告知」，
    # 新造一个类型只会多一处「原来还有第二种不弹窗的放行」要记。
    if (decision.allowed or decision.denied) and decision.source in AUTO_APPROVE_SOURCES:
        record_policy_outcome(
            thread_id=session.get("thread_id", ""), call_id=call_id,
            tool_name=tool_name, args=args, danger_level=danger_level,
            decision=decision, run_id=run_id,
            employee_id=session.get("employee_id", ""),
        )
        tool_label = tool_registry.display_name_for(tool_name)
        if decision.allowed:
            emit({
                "type": "approval_auto",
                "call_id": call_id,
                "tool_name": tool_name,
                "tool_label": tool_label,
                "args": args,
                "reason": decision.reason,
                "content": f"「{tool_label}」已按{ _source_label(decision.source) }自动放行",
            })
    return decision


async def fire_post_tool(
    *,
    emit: Callable[[dict[str, Any]], None],
    call_id: str,
    tool_name: str,
    args: dict[str, Any],
    mode: str,
    session: dict[str, str],
    success: bool,
    result_text: str,
) -> list[str]:
    """
    跑 PostToolUse，把 `notice` 推给界面，返回要拼进工具输出的补充句。

    ⚠️ 钩子**不能**改写执行结果：工具已经跑完，副作用已经发生，
    让脚本把「已成功」改成「失败」只会让模型与留痕对不上账。
    所以这里只往外送信息，不收回判定。

    payload 的字段名是钩子脚本读的合同。两条循环共用这一个函数，
    就是在保证这份合同只有一份 —— 脚本在子图里读得到 `tool_response.result`、
    在主对话里读不到，那种 bug 排查起来最费时间。
    """
    post = await fire(
        POST_TOOL,
        build_payload(
            POST_TOOL,
            tool_name=tool_name,
            args=args,
            session=session,
            permission_mode=mode,
            # 送**未截断**的执行输出（只按 4000 字切）：脚本常按内容判断成败
            # （grep 一个错误标记），先截断会让它读到「…[已截断]」而不是真实结尾。
            tool_response={"success": success, "result": str(result_text)[:4000]},
        ),
        tool_name=tool_name,
    )
    for notice in post.notice:
        emit({"type": "hook_notice", "content": notice, "call_id": call_id,
              "tool_name": tool_name, "tool_label": tool_registry.display_name_for(tool_name),
              "event": POST_TOOL})
    return list(post.context)


def _session_of(state: Any) -> dict[str, str]:
    """从状态里取身份四元组（钩子 payload 与留痕用）"""
    return {
        "thread_id": str(state.get("thread_id") or ""),
        "task_id": str(state.get("task_id") or ""),
        "employee_id": str(state.get("employee_id") or ""),
        "agent": str(state.get("active_agent") or ""),
    }


#: 连续失败达到该次数后，向模型注入止损信号（模型会头铁地重试同一条死路）
FAILURE_STREAK_WARN_THRESHOLD = 3

#: 失败报告里每条记录的结果文本最大长度（够说清原因，又不把报告变成日志）
_FAILURE_REPORT_RESULT_CHARS = 150


def _failure_streak(tool_calls: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    """从最近的调用往前数连续失败（success 严格为 False 才算）的次数与记录"""
    failures: list[dict[str, Any]] = []
    for call in reversed(tool_calls or []):
        if call.get("success") is False:
            failures.append(call)
        else:
            break
    failures.reverse()
    return len(failures), failures


def _failure_warning(tool_calls: list[dict[str, Any]]) -> str:
    """连续失败达到阈值时的止损提示（拼进 system prompt；未达阈值返回空串）"""
    streak, failures = _failure_streak(tool_calls)
    if streak < FAILURE_STREAK_WARN_THRESHOLD or not failures:
        return ""
    lines = [f"【工具连续失败】最近 {streak} 次工具调用全部失败："]
    for call in failures[-5:]:
        label = call.get("tool_label") or call.get("tool_name") or "未知工具"
        reason = str(call.get("result") or "").strip().replace("\n", " ")
        if len(reason) > 120:
            reason = reason[:120] + "…"
        lines.append(f"- {label}：{reason or '未返回错误原因'}")
    lines.append(
        "同类操作已反复失败，请立即停止重试："
        "如果还有完全不同的可行途径，最多再试一次；"
        "否则直接输出最终回答，明确说明任务未完成、失败原因和对用户的建议。"
        "不要重复调用已失败的同类工具，不要空手结束。"
    )
    return "\n".join(lines)


def _failure_report(tool_calls: list[dict[str, Any]], step_limit: int, *, reason: str = "") -> str:
    """
    步数耗尽且模型没写任何说明时，从工具历史合成一份可读的失败报告。

    ★ 为什么需要它：`at_limit` 的硬兜底会丢掉模型的工具调用、只留正文，
    而正文可能为空 —— 任务就带着「没有产出回答」这种毫无信息量的失败收场。
    合成报告让失败原因（403 / robots.txt / 网络错误…）直接可见。
    """
    calls = list(tool_calls or [])
    if not calls and not reason:
        return ""
    failed = [c for c in calls if c.get("success") is False]
    done = [c for c in calls if c.get("success") is True]
    lines = [f"任务未能完成：{reason}" if reason else
             f"任务未能完成：{len(failed)} 个步骤失败，且已达工具调用上限（{step_limit} 步）。"]
    if done:
        lines.append("已完成的步骤：")
        for i, c in enumerate(done[-5:], 1):
            lines.append(f"{i}. {c.get('tool_label') or c.get('tool_name') or '未知工具'}")
    if failed:
        lines.append("失败的步骤：")
        for i, c in enumerate(failed[-6:], 1):
            label = c.get("tool_label") or c.get("tool_name") or "未知工具"
            reason = str(c.get("result") or "").strip().replace("\n", " ")
            if len(reason) > _FAILURE_REPORT_RESULT_CHARS:
                reason = reason[:_FAILURE_REPORT_RESULT_CHARS] + "…"
            lines.append(f"{i}. {label}：{reason or '未返回错误原因'}")
    lines.append("建议：检查相关服务（如搜索服务的账户额度）后重试，或让我换个方式再做。")
    return "\n".join(lines)


def _build_context_text(sources: list[dict[str, Any]]) -> str:
    """把检索结果拼成 Prompt 上下文"""
    if not sources:
        return "（未检索到相关文档）"
    parts = [f"[文档{i + 1}] (来源: {d.get('source', '?')})\n{d.get('text', '')}"
             for i, d in enumerate(sources)]
    return "\n\n---\n\n".join(parts)


# ---------- 规划节点（每个子 Agent 共用） ----------
async def run_planner_turn(
    state: PlannerInput,
    config: RunnableConfig,
    agent_name: str,
) -> dict[str, Any]:
    """
    子 Agent 的一轮规划：LLM 决策 → 流式输出 token → 产出待执行工具队列

    Args:
        state: 状态投影（PlannerInput —— build_graph 按 input_schema 过滤后传入）
        config: LangGraph RunnableConfig（从中取 emit）
        agent_name: 子 Agent 节点名（pc_agent / file_agent ...）
    """
    emit = get_emitter(config)
    updates: dict[str, Any] = {}

    # ---------- 进入新 Agent 时推送 agent_switch ----------
    previous_agent = state.get("active_agent") or "main_agent"
    if previous_agent != agent_name:
        emit({"type": "agent_switch", "content": agent_name, "from_agent": previous_agent})
        emit({
            "type": "state_update",
            "content": f"当前执行: {agent_name}",
            "state_key": "active_agent",
            "value": agent_name,
        })

    chain = list(state.get("agent_chain") or [])
    if not chain or chain[-1] != agent_name:
        chain.append(agent_name)
    updates["agent_chain"] = chain
    updates["active_agent"] = agent_name

    # ---------- 构造 System Prompt ----------
    from app.agents.registry import get_registry

    registry = get_registry()
    meta = registry.describe_for_prompt(agent_name)
    system_prompt = SUB_AGENT_PROMPT.format(
        agent_name=meta["agent_name"],
        domain=meta["domain"],
        responsibility=meta["responsibility"],
        tools_description=tool_registry.describe_for_agent(agent_name),
    )

    # ---------- 数字员工归属（检索边界 + 工具白名单两处都要用） ----------
    # 提到检索之前读：主 Agent 走的是全局知识库（employee_id 为空 → 不设边界），
    # 而员工会话/员工子任务必须知道「这一轮该按哪个项目的范围筛」。
    employee_id = str(state.get("employee_id") or "")

    # ---------- RAG 预检索（按专员的 `retrieval` 开关，且本轮尚未检过时） ----------
    # 改造前这里是 `agent_name == "rag_agent"` 的硬编码 —— 于是「给新专员也开检索」
    # 必须改代码。现在它是注册表里的一个字段，与其余三个维度同源。
    # `sources_ready` 而不是 `if not sources`：主 Agent 可能已经**预取**过
    # （与意图识别并发跑，见 main_agent_node）。检索为空与「还没检过」是两回事 ——
    # 知识库没命中、依赖全挂都会得到空结果，把它们误判成「还没检」会白跑第二遍。
    sources = state.get("sources") or []
    if registry.uses_retrieval(agent_name):
        if not state.get("sources_ready"):
            try:
                from app.rag.retriever import retrieve, serialize_sources
                from app.workspace.knowledge_scope import scope_for_employee

                # 知识边界（ADR 0022）：None = 本轮不设边界。
                # ⚠️ 它只管「取用」—— 索引仍是全局的，越界内容检索不到但没从库里消失。
                sources = await retrieve(
                    state["user_input"],
                    top_k=settings.RERANK_TOP_N,
                    graph=state.get("graph_enabled", True),
                    scope=scope_for_employee(employee_id),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("RAG 检索失败（降级为纯 LLM 回答）: %s", e)
                sources = []
            emit({"type": "sources", "content": serialize_sources(sources)})
            updates["sources"] = sources
            updates["sources_ready"] = True

        system_prompt = RAG_SYSTEM_PROMPT.format(context=_build_context_text(sources)) + "\n\n" + system_prompt

    # ---------- 工具绑定 ----------
    step_count = len(state.get("tool_calls") or [])
    step_limit = registry.effective_max_steps(agent_name)
    at_limit = step_count >= step_limit
    # ★ 数字员工工具白名单：employee_id 非空时，在 base_agent 默认权限之上再收窄。
    allowed_names: list[str] | None = None
    if employee_id:
        try:
            from app.workspace.employee_runtime import get_employee_tools_allowed

            allowed_names = get_employee_tools_allowed(employee_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取员工扩展权限失败（本轮不提供工具）: %s", e)
            allowed_names = []
    tools = [] if at_limit else _tools_for_agent(agent_name, state, allowed_names=allowed_names)
    if at_limit:
        system_prompt += "\n\n【注意】本次任务的工具调用次数已达上限，请直接基于已有信息给出最终回答，不要再调用工具。"
    failure_warning = _failure_warning(state.get("tool_calls") or [])
    if failure_warning:
        system_prompt += f"\n\n{failure_warning}"

    # ---------- 助手人设注入（前端可编辑） ----------
    # ★ 注入点只有这一处：所有子 Agent 的 system prompt 都在这里构造。
    # 人设放在最前（身份先于规则），与拼在末尾的记忆块分工：
    # 人设回答「我是谁」，记忆回答「我记得你什么」。
    # 失败一律忽略：人设是增益，不能因为它读不出来就让回答报错。
    # 注意：主 Agent 的意图识别不吃人设 —— 路由精度不该被「轻松幽默」影响。
    #
    # ★ 数字员工覆盖：employee_id 非空时，用该员工自己的「角色定位 + 能力设定 +
    # 参与范围」替换全局单例人设（工作空间模块）。
    try:
        if employee_id:
            from app.workspace.employee_runtime import build_employee_persona_fragment

            persona_block = build_employee_persona_fragment(employee_id)
        else:
            from app.persona import get_persona_manager

            # 作用范围由人设自己的 apply_globally 决定：默认只在助理会话注入。
            # ★ 子 Agent 只要稳定块：工具循环里 system prompt 每步重建，
            # 动态块（此刻/关系记忆）会让同一轮内前缀不断变化，白烧 token。
            persona_block = get_persona_manager().build_prompt_fragment(
                str(state.get("thread_id") or ""),
                include_dynamic=False,
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("人设注入失败（已忽略）: %s", e)
        persona_block = ""
    if persona_block:
        system_prompt = f"{persona_block}\n\n{system_prompt}"

    # ---------- 长期记忆注入（Phase 5） ----------
    # ★ 只在这一处注入：所有子 Agent 的 system prompt 都在这里构造，
    #   与 Phase 4「只在事件流埋一次点」是同一种收敛 ——
    #   将来新增任何子 Agent，都自动获得记忆，不会漏。
    #
    # 读取本身带 stat 缓存，所以即使一轮工具循环里被调很多次也不会反复读盘。
    # 失败一律忽略：记忆是增益，不能因为它读不出来就让回答报错。
    try:
        from app.memory import get_manager

        memory_block = get_manager().build_prompt_block(query=str(state.get("user_input") or ""), shareable_only=bool(employee_id))
    except Exception as e:  # noqa: BLE001
        logger.debug("长期记忆注入失败（已忽略）: %s", e)
        memory_block = ""
    if memory_block:
        system_prompt = f"{system_prompt}\n\n{memory_block}"

    if employee_id:
        from app.workspace.scoped_memory import scoped_memory
        from app.workspace.inbox import inbox
        memory = scoped_memory.prompt(employee_id)
        mail = inbox.context(employee_id, str(state.get("task_id") or ""), str(state.get("thread_id") or ""))
        system_prompt += "\n\n" + memory + "\n\n" + mail

    # ---------- 技能清单注入（L1，见 `app/skills.py`） ----------
    # 与主对话同一份 `build_prompt_block()`：专员也可能被派到「某件有专门做法的事」
    # （写周报、整理纪要），技能对它们是同一种增益。
    # 失败一律忽略 —— 技能读不出来时，专员按通用做法干活，不该因此报错。
    try:
        from app.skills import get_skill_store

        if employee_id:
            from app.workspace.employee_runtime import get_employee_skills_allowed

            skills_block = get_skill_store().build_prompt_block(allowed_names=get_employee_skills_allowed(employee_id))
        else:
            skills_block = get_skill_store().build_prompt_block()
    except Exception as e:  # noqa: BLE001
        logger.debug("技能清单注入失败（已忽略）: %s", e)
        skills_block = ""
    if skills_block:
        system_prompt = f"{system_prompt}\n\n{skills_block}"

    # ---------- 钩子补充的上下文 ----------
    # 来源是 `UserPromptSubmit` 钩子的 `context` 字段（脚本能读 stdin 里的用户输入，
    # 于是可以回一句「这个问题涉及的目录是只读的」）。
    # 放在系统提示末尾：它是**本轮**的信息，比人设与长期记忆更该靠近对话。
    # ⚠️ 它只是**说明**，改变不了任何判定 —— 权限在 `tool_executor_node` 里
    # 重新 `decide()` 一次，模型说什么、这段文字里写什么，都放宽不了权限。
    hook_block = str(state.get("hook_context") or "").strip()
    if hook_block:
        system_prompt = f"{system_prompt}\n\n【外部检查补充】{hook_block}"

    # 本轮模型：顶栏用户选的 > 专员自己配置的 > 全局默认（`.env` 的 LLM_MODEL）。
    # 只影响**回答**这一层 —— 意图识别仍用全局模型（它要快，不该被大模型拖慢）。
    # 温度：注册表没显式给就跟随全局 `LLM_TEMPERATURE`。原先这里写死 0，
    # 会误杀只接受单一温度的模型（kimi-k2.7-code 对 temperature != 1 直接 400）。
    employee_profile_id = None
    if employee_id:
        from app.workspace.store import workspace_store

        employee_config = workspace_store.get_employee(employee_id) or {}
        employee_profile_id = str(employee_config.get("model_profile_id") or "") or None
    effective_model = registry.effective_model(str(state.get("model") or ""), agent_name)
    llm = create_llm(
        temperature=registry.effective_temperature(agent_name),
        streaming=True,
        model=effective_model or None,
        profile_id=employee_profile_id,
        **({"max_retries": 0, "max_tokens": 4096} if state.get("run_id") else {}),
    )
    llm_bound = llm.bind_tools(tools) if tools else llm

    # ---------- 上下文视图 ----------
    # 按 token 预算构造，而不是「最近 20 条」。子任务的指令始终保留，
    # 中段的工具步骤按交换块边界压缩，最近的结果原样送给模型。
    scope_id = _subtask_scope(state, config, agent_name)
    task_thread_id = str(state.get("thread_id") or "")
    context = await build_context(
        scope_id=scope_id,
        thread_id=task_thread_id,
        system_prompt=system_prompt,
        history=state.get("messages") or [],
        tools=tools,
        model=effective_model,
        profile_id=employee_profile_id,
        is_subtask=True,
    )
    messages = context.messages

    # ---------- 流式调用 LLM ----------
    acc: Any = None
    emitted_text = []
    first_token_ms: float | None = None
    chunk_count = 0
    context_retried = False

    while True:
        attempt_emitted = False
        llm_span = _trace_llm_start(agent_name, system_prompt, len(messages), llm=llm)
        try:
            from app.workspace.budget import budgeted_stream
            async for chunk in budgeted_stream(llm_bound, messages, run_id=str(state.get("run_id") or ""), retry=context_retried):
                acc = chunk if acc is None else acc + chunk
                chunk_count += 1
                text = _chunk_text(chunk)
                if text:
                    attempt_emitted = True
                    if first_token_ms is None and llm_span is not None:
                        first_token_ms = round(now_ms() - llm_span.start_ms, 2)
                    emitted_text.append(text)
                    emit({"type": "token", "content": text})
            break
        except Exception as e:  # noqa: BLE001
            # ★ 真的超限（估算偏乐观 / 网关窗口更小）时压缩后重试一次。
            # 只有「一个字都没吐出来」才能重试：已经推给前端的增量无法撤回。
            if (
                not context_retried
                and not attempt_emitted
                and acc is None
                and is_context_overflow_error(e)
            ):
                context_retried = True
                logger.warning("子 Agent %s 上下文超限，压缩后重试一次: %s", agent_name, e)
                rebuilt = False
                try:
                    context = await build_context(
                        scope_id=scope_id,
                        thread_id=task_thread_id,
                        system_prompt=system_prompt,
                        history=state.get("messages") or [],
                        tools=tools,
                        model=effective_model,
                        profile_id=employee_profile_id,
                        is_subtask=True,
                        force_compact=True,
                        aggressive=True,
                    )
                    messages = context.messages
                    rebuilt = True
                except Exception as build_error:  # noqa: BLE001
                    logger.warning("超限后压缩失败（按原错误上报）: %s", build_error)
                if rebuilt:
                    _trace_llm_end(
                        llm_span, "", None,
                        first_token_ms=None, chunk_count=chunk_count,
                        error="context_overflow_retry",
                    )
                    continue

            logger.error("子 Agent %s LLM 调用失败: %s", agent_name, e, exc_info=True)
            _trace_llm_end(llm_span, "".join(emitted_text), acc,
                           first_token_ms=first_token_ms, chunk_count=chunk_count, error=str(e))
            # 失败的调用一样会消耗 token（模型可能已生成一部分才断），
            # 所以用量照推 —— 只是它可能不完整，界面按「至少这些」理解
            _emit_usage(emit, agent_name, acc, llm=llm, step=step_count,
                        duration_ms=_span_duration(llm_span), status="error")
            emit({"type": "error", "content": f"模型调用失败: {e}"})
            updates["pending_calls"] = []
            updates["final_answer"] = ""
            detail = str(e)
            quota_error = any(marker in detail.lower() for marker in (
                "freetieronly", "free quota", "insufficient_quota", "allocationquota", "额度耗尽", "余额不足",
            ))
            reason = "模型账户额度已耗尽，请切换可用模型或检查账户额度后重试。" if quota_error else f"模型调用失败：{detail[:500]}"
            updates["final_failure"] = _failure_report(
                list(state.get("tool_calls") or []), step_limit, reason=reason
            ) or f"模型调用失败：{e}"
            return updates

    _trace_llm_end(llm_span, "".join(emitted_text), acc,
                   first_token_ms=first_token_ms, chunk_count=chunk_count)
    # 真实用量只有这一层拿得到（聚合后的 `acc`），推给事件流供 `done` 使用
    _emit_usage(emit, agent_name, acc, llm=llm, step=step_count,
                duration_ms=_span_duration(llm_span))

    # ★ 必须保留这条 AI 消息：即使 content 为空，它携带的 tool_calls 也是
    # 后续 ToolMessage 的引用目标。丢掉它会让工具结果变成「孤立 ToolMessage」
    # 被清洗掉 → 模型永远看不到工具结果 → 无限重复调用。
    # （模型只发工具调用、不带前言文本是很常见的行为，此处不可“优化”掉。）
    if acc is not None and (_message_text(acc).strip() or getattr(acc, "tool_calls", None)):
        updates["messages"] = [acc]

    # ---------- 解析工具调用 ----------
    tool_calls = list(getattr(acc, "tool_calls", None) or [])

    # 已达步数上限：忽略模型这一轮想要的工具调用，强制收尾。
    # 不能只靠 Prompt 里「请不要再调用工具」的劝导 —— 模型不听话就会死循环，
    # 这里做一次硬性兜底，保证单轮一定能结束。
    budget_blocked = bool(tool_calls and at_limit)
    if budget_blocked:
        logger.warning("工具调用已达上限 %s，忽略本轮工具请求并强制收尾", step_limit)
        emit({
            "type": "state_update",
            "content": f"已达工具调用上限（{step_limit} 步），正在基于已有信息作答",
            "state_key": "phase",
            "value": "max_steps_reached",
        })
        tool_calls = []

    if not tool_calls:
        answer = "".join(emitted_text).strip()
        if not answer and at_limit:
            # 步数耗尽且模型没写任何说明：合成失败报告兜底，
            # 保证任务要么成功、要么带着可读的原因失败（终态必有说明）。
            answer = _failure_report(list(state.get("tool_calls") or []), step_limit) or (
                f"任务未完成：已达工具调用上限（{step_limit} 步）且没有产出说明。"
            )
            updates["final_failure"] = answer
        updates["pending_calls"] = []
        if budget_blocked:
            updates["final_failure"] = "任务未完成：工具预算已用尽，尚未通过交付验收。" + (f"\n{answer}" if answer else "")
        updates["final_answer"] = answer
        updates["sub_agent_result"] = answer
        return updates

    pending: list[dict[str, Any]] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
        info = tool_registry.get_info(name)
        danger_level = info.danger_level if info else "safe"
        tool_label = tool_registry.display_name_for(name)
        record = {
            "call_id": call_id,
            "tool_name": name,
            "tool_label": tool_label,
            "args": tc.get("args") or {},
            "danger_level": danger_level,
            "description": info.description if info else "未注册的工具",
        }
        pending.append(record)
        emit({
            "type": "tool_call",
            "tool_name": name,
            "tool_label": tool_label,
            "args": record["args"],
            "call_id": call_id,
            "danger_level": danger_level,
        })

    # ★ ask_user 提到队首：它必须是这一步的唯一执行者
    # 与主图（supervisor.respond_node）同一条纪律 —— 其余调用若排在它前面，
    # 就会在用户看到问题之前先产生副作用，等于替用户做了选择。
    # 提到队首后，`_handle_ask_user` 会把其余调用统一回成「未执行」。
    ask_position = next(
        (i for i, item in enumerate(pending) if item.get("tool_name") == "ask_user"),
        None,
    )
    if ask_position not in (None, 0):
        pending.insert(0, pending.pop(ask_position))

    updates["pending_calls"] = pending
    updates["sub_agent_result"] = ""

    return updates


# ---------- 工具执行节点（全子 Agent 共用） ----------
async def _handle_ask_user(
    state: ToolExecutorInput,
    updates: dict[str, Any],
    call: dict[str, Any],
    *,
    emit: Callable[[dict[str, Any]], None],
    call_id: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """
    在子图里处理一次 `ask_user`：归一化 → `interrupt()` → 把回答变成 ToolMessage。

    ★ 为什么必须在权限判定与钩子**之前**拦截
    `interrupt()` 之后的代码会在恢复时重放。把判定/钩子放在它前面，
    恢复时就会跑第二遍（钩子脚本写两次文件、留痕重复开单）。
    提问本身是 safe 工具，不需要判定；把它提到最前面同时解决两件事。

    ★ 参数不合法时不挂起
    把错误作为 `tool_result` 回给模型，让它自己改一版再问 ——
    一次格式抖动不该让用户面对一张残缺的卡片，也不该让任务停在那里。
    """
    from app.questions.models import (
        build_question_payload,
        format_question_result,
        normalize_ask_user_args,
        normalize_question_resume,
    )

    questions, error = normalize_ask_user_args(args)
    if error:
        result_text = f"提问没有发出：{error}"
        emit({
            "type": "tool_result",
            "call_id": call_id,
            "tool_name": "ask_user",
            "tool_label": tool_registry.display_name_for("ask_user"),
            "result": result_text,
            "success": False,
            "duration_ms": 0,
        })
        return {
            **updates,
            "tool_calls": list(state.get("tool_calls") or []) + [{
                **call,
                "success": False,
                "result": result_text,
                "duration_ms": 0,
                "approved": None,
            }],
            "messages": [ToolMessage(content=result_text, tool_call_id=call_id)],
        }

    payload = build_question_payload(
        call_id=call_id,
        questions=questions,
        run_id=str(state.get("run_id") or ""),
        employee_id=str(state.get("employee_id") or ""),
    )
    # interrupt 之前没有副作用：节点恢复时从头重放是安全的
    resumed = interrupt(payload)
    outcome = normalize_question_resume(resumed)
    result_text = format_question_result(questions, outcome.status, outcome.answers)

    emit({
        "type": "tool_result",
        "call_id": call_id,
        "tool_name": "ask_user",
        "tool_label": tool_registry.display_name_for("ask_user"),
        "result": result_text,
        "success": True,
        "duration_ms": 0,
    })

    # ---------- 同一步里的其它调用不执行（与主图同一条纪律） ----------
    # 它们的结果很可能取决于用户将给出的答案；先产生副作用再问，
    # 等于替用户做了选择。给每条一条「未执行」的 ToolMessage，
    # 模型拿到回答后会自己重新发起 —— 绝不能静默丢弃，那会让消息里
    # 留下没有回应的 tool_call（下一轮清洗时整块消失，界面表现为永远转圈）。
    #
    # ★ 必须在 interrupt 之后：恢复时本节点从头重放，写在前面会重复发事件。
    siblings = [item for item in (updates.get("pending_calls") or []) if isinstance(item, dict)]
    sibling_messages: list[Any] = []
    sibling_records: list[dict[str, Any]] = []
    if siblings:
        updates["pending_calls"] = []
        for sibling in siblings:
            sibling_call_id = str(sibling.get("call_id") or "")
            sibling_name = str(sibling.get("tool_name") or "")
            sibling_text = (
                "本轮同时发起了向你提问，这一步没有执行。"
                "请先根据用户的回答决定下一步，再重新调用。"
            )
            emit({
                "type": "tool_result",
                "call_id": sibling_call_id,
                "tool_name": sibling_name,
                "tool_label": tool_registry.display_name_for(sibling_name),
                "result": sibling_text,
                "success": False,
                "duration_ms": 0,
            })
            sibling_messages.append(ToolMessage(content=sibling_text, tool_call_id=sibling_call_id))
            sibling_records.append({
                **sibling,
                "success": False,
                "result": sibling_text,
                "duration_ms": 0,
                "approved": None,
            })

    return {
        **updates,
        "tool_calls": list(state.get("tool_calls") or []) + [{
            **call,
            "success": True,
            "result": result_text,
            "duration_ms": 0,
            "approved": None,
        }] + sibling_records,
        "messages": [ToolMessage(content=result_text, tool_call_id=call_id), *sibling_messages],
    }


async def tool_executor_node(state: ToolExecutorInput, config: RunnableConfig) -> dict[str, Any]:
    """
    执行 pending_calls 队列头的**一个**工具，然后由路由决定是继续执行下一个还是回到 planner。

    要不要执行由 `decide_tool_call` 回答（规则 / 权限模式 / 钩子 / 危险等级），
    判定为「要问」时 `interrupt()` 挂等人工审批，通过才执行。
    **只有这条循环能挂起** —— 主对话那条没有 interrupt 点，见 `supervisor.py`。

    state 是 ToolExecutorInput 投影（build_graph 按 input_schema 过滤后传入）：
    本节点关心待执行队列、留痕上下文、权限档，以及用来构造 `ToolContext` 的三个身份字段；
    不读对话历史/意图/检索来源。
    """
    emit = get_emitter(config)

    pending = list(state.get("pending_calls") or [])
    if not pending:
        return {}

    call = pending[0]
    rest = pending[1:]
    agent_name = state.get("active_agent") or ""
    tool_name = call.get("tool_name", "")
    args = call.get("args") or {}
    call_id = call.get("call_id", "")

    # 公共的状态更新（所有分支都基于它）：队列头已消费，界面上的当前调用清空
    updates: dict[str, Any] = {"pending_calls": rest, "current_call": None}

    # ---------- 模型主动提问：先于一切副作用拦截 ----------
    # 详见 `_handle_ask_user` 的注释；这一条必须在 `decide_tool_call` 之前，
    # 否则恢复重放会把钩子与留痕再跑一遍。
    if tool_name == "ask_user":
        return await _handle_ask_user(
            state,
            updates,
            call,
            emit=emit,
            call_id=call_id,
            args=args if isinstance(args, dict) else {},
        )

    info = tool_registry.get_info(tool_name)
    danger_level = info.danger_level if info else "safe"
    mode = str(state.get("permission_mode") or "") or mode_store_default()
    session = _session_of(state)

    # ---------- Phase 5：判定 + 钩子 ----------
    #
    # ★ 这一段整体替换了原来的 `if danger_level == "danger"`
    # 原来「要不要问」只由工具的等级决定；现在是 `policy.decide()` 的一串依据
    # （deny 规则 → 只读模式 → 钩子 → ask 规则 → 模式 → allow 规则 → 等级）。
    # 判定与钩子都必须在 `interrupt()` **之前**，因为 interrupt 之后的任何代码
    # 都会在恢复时**重放一遍** —— 钩子会被跑第二次，规则会多算一次留痕。
    # （同一条约束决定了这个节点为什么只消费队列头一个工具，见不变量 6。）
    #
    # 入口函数与主对话共用（见 `decide_tool_call`），所以留痕与 `approval_auto`
    # 那两件事不在这里写了 —— 两条路都得有，一处实现。
    decision = await decide_tool_call(
        emit=emit, call_id=call_id, tool_name=tool_name, args=args,
        danger_level=danger_level, mode=mode, session=session,
        run_id=str(state.get("run_id") or ""),
    )

    if decision.denied:
        return _refuse(
            state, updates, call,
            emit=emit,
            call_id=call_id, tool_name=tool_name,
            reason=decision.reason,
            model_note="该操作被权限设置禁止，绝对不要再尝试调用它；请改为说明情况并给出替代方案。",
        )

    # ---------- HITL：判定为「要问」时挂起等人工审批 ----------
    if decision.needs_approval:
        # interrupt 之前不做任何副作用，保证节点重放安全
        decision_result = interrupt({
            "call_id": call_id,
            "tool_name": tool_name,
            "args": args,
            "danger_level": danger_level,
            # ★ 这句会**直接显示给用户**（审批弹窗主区域下方），所以不写工具名 ——
            # 「要你批准哪一步」由前端的人话摘要回答（「将删除 demo.txt」），
            # 这里只回答「为什么需要你点头」。Phase 5 之后这句话可能来自规则或钩子，
            # 不再只有「不可逆影响」那一种。工具名在 `tool_name` 字段里，
            # 开发者模式下前端会把它显示出来，需要排查的人不缺这一条信息。
            "reason": decision.reason,
            "decision_source": decision.source,
            "permission_mode": mode,
        })

        if not _is_approved(decision_result):
            return _refuse(
                state, updates, call,
                emit=emit,
                call_id=call_id, tool_name=tool_name,
                reason="用户已拒绝执行该操作",
                model_note="用户拒绝执行该操作，请不要再尝试调用该工具，改为说明情况并给出替代方案。",
                user_rejected=True,
            )

    # ---------- 实际执行 ----------
    # ★ 必须带 ctx：工具的会话级语义（写前的读校验、shell 的 cwd 与后台任务、
    # 授权根）全靠它。传 None 时注册中心会兜一个空上下文，节点不报错，
    # 但那两项能力同时静默失效 —— 见 `ToolContext.context_from_state`。
    ctx = context_from_state(state, agent_name=agent_name, emit=emit)
    ctx.call_id = call_id
    result: ToolResult = await tool_registry.execute(tool_name, args, ctx=ctx)

    emit({
        "type": "tool_result",
        "call_id": call_id,
        "tool_name": tool_name,
        "tool_label": tool_registry.display_name_for(tool_name),
        "result": result.result if result.success else (result.error or "执行失败"),
        "success": result.success,
        "duration_ms": result.duration_ms,
    })

    # ---------- PostToolUse 钩子 ----------
    # 它的 `context` 会被拼进工具输出（模型看得到），`notice` 只推给界面。
    raw_output = result.result if result.success else f"执行失败：{result.error}"
    post_context = await fire_post_tool(
        emit=emit, call_id=call_id, tool_name=tool_name, args=args,
        mode=mode, session=session, success=result.success, result_text=raw_output,
    )

    # ---------- 回到模型 ----------
    # 顺序是有意的：先落盘/折叠（保护上下文），再拼钩子补充。
    # 与旧实现的关键差别：超长输出不再「切掉就扔」——
    # 全文写到只读文件里，模型看到的是「开头 + 结尾 + 文件路径」。
    tool_output = shape_tool_result(
        thread_id=str(state.get("thread_id") or ""),
        call_id=call_id,
        tool_name=tool_name,
        text=raw_output,
        args=args,
    )
    for extra in post_context:
        tool_output = f"{tool_output}\n\n【钩子补充】{extra}"

    # `approved` 只记「用户点过批准」。
    # 由规则/模式/钩子放行的写 None —— 它们的留痕在 `record_policy_outcome` 里，
    # 两处都记 True 的话审计就分不清「用户同意的」和「配置允许的」（不变量 23）。
    record = {
        **call,
        "success": result.success,
        "result": result.result if result.success else (result.error or ""),
        "duration_ms": result.duration_ms,
        "approved": True if decision.needs_approval else None,
        "files": list(result.files),
    }

    approval_log = list(state.get("approval_log") or [])
    if decision.needs_approval:
        approval_log.append({"call_id": call_id, "tool_name": tool_name, "approved": True})

    return {
        **updates,
        "tool_calls": list(state.get("tool_calls") or []) + [record],
        "approval_log": approval_log,
        "pending_approval": None,
        "messages": [ToolMessage(content=tool_output, tool_call_id=call_id)],
    }


__all__ = [
    "decide_tool_call",
    "fire_post_tool",
    "run_planner_turn",
    "tool_executor_node",
]
