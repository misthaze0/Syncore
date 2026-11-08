"""
`ask_user` —— 模型主动向用户提问的唯一入口。

★ 这是一个「看起来像工具、实际上是一次挂起」的工具

模型看到的仍然是普通工具：给参数、拿 `tool_result`。
但那一步不会立刻返回 —— 驱动层把 `interrupt()` 的载荷推给前端，
用户回答后才用 `Command(resume=...)` 继续。对模型而言，
它只是「这次工具调用等了一会儿」，下一轮读到的就是用户给的答案。

★ 为什么危险等级是 safe

提问不改变任何东西，也不产生副作用。真正的安全边界在审批那条路：
把「批准危险操作」也做成一个 safe 工具，会让模型可以用一次提问绕过审批
（用户以为在回答问题，实际在签授权）。所以 `ask_user` 的回答
**永远不能**被当成审批结果 —— 两条路的事件类型、留痕、恢复载荷都不同。

★ 为什么 handler 返回一句「没送达」

它只在**没有挂起能力**的路径上被执行（定时任务、脚本直调、没有检查点的图）。
那些路径上等待一个永远不会来的回答，比诚实报错更糟：
模型会以为用户在无视它。返回这句话让它基于假设继续，
并把「发生过一次没送达的提问」留在工具结果里可查。
"""

from __future__ import annotations

from typing import Any

from app.agents.tools.context import ToolContext
from app.agents.tools.decorator import agent_tool
from app.questions.models import ASK_USER_TOOL, AskUserArgs


@agent_tool(
    name=ASK_USER_TOOL,
    display_name="向你提问",
    description=(
        "当完成请求还缺少**关键**信息（选哪个方案、偏好、范围、对象），"
        "且无法从对话、文件或工具结果推断时，向用户提出 1-4 个带选项的问题。"
        "调用后本轮会暂停，用户回答会作为本次工具调用的结果返回，你再继续。"
        "不要用它请求危险操作的批准（那走审批）；"
        "不要为已经能从上下文推断的信息发问；"
        "也不要用它做「要不要继续」这类没有信息量的确认。"
        "每个问题给 2-4 个可点击的选项，推荐项标 recommended=true；"
        "用户始终可以在「其他」里自由输入。"
    ),
    category="对话",
    danger_level="safe",
    read_only=True,
    args_model=AskUserArgs,
    args_schema=AskUserArgs.model_json_schema(),
)
def ask_user(questions: list[dict[str, Any]], ctx: ToolContext | None = None) -> str:
    """向用户提出结构化问题并等待回答。

    Args:
        questions: 1-4 个问题；每个问题包含 question、header（可选）、
            options（2-4 个 {label, description, recommended}）与 multiSelect。
    """
    return (
        "（这次提问没有送达用户：当前执行路径无法等待回答。）\n\n"
        "请基于现有信息选择最合理的做法继续，并在最终回答里说明你采用的假设；"
        "不要重复这次提问。"
    )


__all__ = ["ask_user"]
