# -*- coding: utf-8 -*-
"""
工作流执行引擎 —— 按画布上的边逐步跑，不引入第二套图框架。

★ 为什么不用 LangGraph 执行它

工作流是**用户画出来的**，不是代码写出来的：节点之间有明确的线性关系，
唯一的非线性是条件分支。为一个「无环 + 单入口 + 单出口」的图引入
checkpointer / 状态合并 / 中断恢复，换来的复杂度远大于它解决的问题。
真正需要图能力的是对话 Agent（工具循环、审批挂起），那条链路已经在 LangGraph 上。

★ 变量只有两种来源

- `{{input}}`：本次运行的用户输入；
- `{{节点id}}`：该节点已完成步骤的输出。

缺变量时渲染成空串而不是报错：用户的提示词里写了一个还没跑到的节点，
正确表现是「这一段为空」，不是让整次运行失败。

★ 工具节点的权限仍然走注册中心

工作流可以调工具，但**不绕过权限**：`ToolContext.policy_handled=False`，
需要人工批准的工具在这里会被明确拒绝（没有能弹窗的驱动层）。
这是刻意的 —— 定时触发的工作流不该在无人值守时执行危险操作。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Optional

from app.workflows.models import (
    HANDLE_FALSE,
    HANDLE_TRUE,
    NODE_ANSWER,
    NODE_CONDITION,
    NODE_KNOWLEDGE,
    NODE_LLM,
    NODE_START,
    NODE_TOOL,
    STEP_OUTPUT_CHARS,
    WorkflowRecord,
    cap_output,
    next_edges,
    validate_graph,
)

logger = logging.getLogger(__name__)

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")

#: 单次运行允许的最大节点执行次数（校验已禁止环路，这里只防实现漂移）
_MAX_STEPS = 200


class WorkflowRunError(RuntimeError):
    """执行失败；消息直接给用户看。"""


def render_template(text: str, variables: dict[str, Any]) -> str:
    def _replace(match: re.Match[str]) -> str:
        value = variables.get(match.group(1))
        return "" if value is None else str(value)

    return _VAR_RE.sub(_replace, text or "")


def _render_value(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return render_template(value, variables)
    if isinstance(value, list):
        return [_render_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {str(k): _render_value(v, variables) for k, v in value.items()}
    return value


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content or "")


def _truthy(left: str, operator: str, right: str) -> bool:
    left_s = (left or "").strip()
    right_s = (right or "").strip()
    if operator == "contains":
        return right_s in left_s
    if operator == "not_contains":
        return right_s not in left_s
    if operator == "equals":
        return left_s == right_s
    if operator == "not_equals":
        return left_s != right_s
    if operator == "starts_with":
        return left_s.startswith(right_s)
    if operator == "ends_with":
        return left_s.endswith(right_s)
    if operator == "is_empty":
        return left_s == ""
    if operator == "not_empty":
        return left_s != ""
    if operator in ("gt", "gte", "lt", "lte"):
        try:
            lhs, rhs = float(left_s), float(right_s)
        except (TypeError, ValueError) as e:
            raise WorkflowRunError(f"条件比较需要数字：{left_s!r} {operator} {right_s!r}") from e
        return {
            "gt": lhs > rhs,
            "gte": lhs >= rhs,
            "lt": lhs < rhs,
            "lte": lhs <= rhs,
        }[operator]
    raise WorkflowRunError(f"未知的条件运算符: {operator}")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


async def execute_graph(
    workflow: WorkflowRecord,
    input_text: str,
    *,
    run_id: str = "",
    on_step: Optional[Callable[[dict[str, Any]], None]] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """执行一张已校验的图，返回 (最终回答, 步骤记录)。"""
    nodes = {str(n.get("id")): n for n in (workflow.get("nodes") or [])}
    edges = list(workflow.get("edges") or [])
    errors = validate_graph(workflow.get("nodes"), edges)
    if errors:
        raise WorkflowRunError("工作流配置有误：" + "；".join(errors))

    starts = [n for n in nodes.values() if str(n.get("type")) == NODE_START]
    if not starts:
        raise WorkflowRunError("工作流没有开始节点")

    variables: dict[str, Any] = {
        "input": input_text or "",
        "now": _now_iso(),
    }
    steps: list[dict[str, Any]] = []
    current_id = str(starts[0].get("id"))
    executed = 0

    def record_step(step: dict[str, Any]) -> None:
        steps.append(step)
        if on_step is not None:
            try:
                on_step(step)
            except Exception as e:  # noqa: BLE001
                logger.warning("工作流步骤回调失败（不影响执行）: %s", e)

    while current_id:
        executed += 1
        if executed > _MAX_STEPS:
            raise WorkflowRunError("执行步数超过上限，已中止（工作流可能存在环路）")
        node = nodes.get(current_id)
        if node is None:
            raise WorkflowRunError(f"找不到节点: {current_id}")

        node_type = str(node.get("type") or "")
        title = str(node.get("title") or current_id)
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        started_at = _now_iso()

        def finish(output: str, *, branch: str = "") -> dict[str, Any]:
            step: dict[str, Any] = {
                "node_id": current_id,
                "title": title,
                "type": node_type,
                "status": "succeeded",
                "started_at": started_at,
                "finished_at": _now_iso(),
                "output": cap_output(output, STEP_OUTPUT_CHARS),
            }
            if branch:
                step["branch"] = branch
            return step

        try:
            if node_type == NODE_START:
                output = input_text or ""
                variables[current_id] = output
                variables[f"{current_id}.output"] = output
                record_step(finish(output))
                outgoing = next_edges(edges, current_id)
                current_id = str(outgoing[0].get("target")) if outgoing else ""

            elif node_type == NODE_LLM:
                output = await _run_llm(config, variables)
                variables[current_id] = output
                variables[f"{current_id}.output"] = output
                record_step(finish(output))
                current_id = _next_target(edges, current_id)

            elif node_type == NODE_KNOWLEDGE:
                output = await _run_knowledge(config, variables)
                variables[current_id] = output
                variables[f"{current_id}.output"] = output
                record_step(finish(output))
                current_id = _next_target(edges, current_id)

            elif node_type == NODE_TOOL:
                output = await _run_tool(config, variables, run_id=run_id)
                variables[current_id] = output
                variables[f"{current_id}.output"] = output
                record_step(finish(output))
                current_id = _next_target(edges, current_id)

            elif node_type == NODE_CONDITION:
                left = render_template(str(config.get("left") or ""), variables)
                right = render_template(str(config.get("right") or ""), variables)
                operator = str(config.get("operator") or "")
                passed = _truthy(left, operator, right)
                branch = HANDLE_TRUE if passed else HANDLE_FALSE
                output = "真" if passed else "假"
                variables[current_id] = output
                variables[f"{current_id}.output"] = output
                record_step(finish(output, branch=branch))
                outgoing = next_edges(edges, current_id, handle=branch)
                current_id = str(outgoing[0].get("target")) if outgoing else ""

            elif node_type == NODE_ANSWER:
                output = render_template(str(config.get("template") or ""), variables)
                variables[current_id] = output
                variables[f"{current_id}.output"] = output
                record_step(finish(output))
                return output, steps

            else:
                raise WorkflowRunError(f"不支持的节点类型: {node_type}")

        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            message = str(e) or e.__class__.__name__
            record_step({
                "node_id": current_id,
                "title": title,
                "type": node_type,
                "status": "failed",
                "started_at": started_at,
                "finished_at": _now_iso(),
                "error": message[:1000],
            })
            raise WorkflowRunError(f"「{title}」执行失败：{message}") from e

        if not current_id:
            raise WorkflowRunError(f"「{title}」之后没有下一步，也没有到达回答节点")

    raise WorkflowRunError("执行结束但没有到达回答节点")


def _next_target(edges: list[dict[str, Any]], node_id: str) -> str:
    outgoing = next_edges(edges, node_id)  # type: ignore[arg-type]
    return str(outgoing[0].get("target")) if outgoing else ""


async def _run_llm(config: dict[str, Any], variables: dict[str, Any]) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.core.llm_factory import create_llm

    system = render_template(str(config.get("system") or ""), variables).strip()
    prompt = render_template(str(config.get("prompt") or ""), variables).strip()
    if not prompt:
        raise WorkflowRunError("提示词为空")

    messages: list[Any] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))

    llm = create_llm(model=str(config.get("model") or "") or None, streaming=False)
    response = await llm.ainvoke(messages)
    text = _content_text(getattr(response, "content", response)).strip()
    if not text:
        raise WorkflowRunError("模型没有返回内容")
    return text


async def _run_knowledge(config: dict[str, Any], variables: dict[str, Any]) -> str:
    from app.rag.retriever import retrieve

    query = render_template(str(config.get("query") or ""), variables).strip()
    if not query:
        raise WorkflowRunError("检索内容为空")
    try:
        top_k = max(1, min(20, int(config.get("top_k") or 5)))
    except (TypeError, ValueError):
        top_k = 5

    hits = await retrieve(query, top_k=top_k)
    if not hits:
        return "（知识库没有命中相关内容）"

    lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        source = str(hit.get("source") or "未知来源")
        page = hit.get("page")
        header = f"[{index}] {source}" + (f" P{page}" if page else "")
        text = str(hit.get("text") or "").strip()
        lines.append(f"{header}\n{text}")
    return "\n\n".join(lines)


async def _run_tool(config: dict[str, Any], variables: dict[str, Any], *, run_id: str) -> str:
    from app.agents.tools.context import ToolContext
    from app.agents.tools.registry import tool_registry

    tool_name = str(config.get("tool") or "").strip()
    if not tool_name:
        raise WorkflowRunError("还没选工具")

    raw_args = config.get("args")
    if isinstance(raw_args, str) and raw_args.strip():
        rendered = render_template(raw_args, variables)
        try:
            args = json.loads(rendered)
        except json.JSONDecodeError as e:
            raise WorkflowRunError(f"工具参数不是合法 JSON：{e.msg}") from e
        if not isinstance(args, dict):
            raise WorkflowRunError("工具参数必须是 JSON 对象")
    elif isinstance(raw_args, dict):
        args = _render_value(raw_args, variables)
    else:
        args = {}

    # policy_handled=False：工作流没有弹窗能力，需要人工批准的工具在这里只能被拒绝
    ctx = ToolContext(run_id=run_id)
    result = await tool_registry.execute(tool_name, args, ctx=ctx)
    if not result.success:
        raise WorkflowRunError(result.error or f"工具 {tool_name} 执行失败")
    return str(result.result or "")


__all__ = [
    "WorkflowRunError",
    "execute_graph",
    "render_template",
]
