"""
`@agent_tool` —— 声明式工具定义

一个工具 = 一个带类型注解和 docstring 的函数。其余信息（危险等级、权限、
分类）在装饰器参数里一次写完，注册在 import 时自动发生。

    @agent_tool(category="文件", danger_level="safe", allowed_agents=["file_agent"])
    async def read(path: str, offset: int = 1, limit: int = 2000) -> str:
        '''读取文件内容。

        Args:
            path: 文件路径
            offset: 起始行号，从 1 开始
        '''

对比原先的写法：同一个工具原本要在 `builtin/__init__.py` 里写一份 JSON Schema
字典、再写一次 `registry.register(...)`、改权限还要同步顶部那张权限总表注释
—— 三处平行描述必然漂移，现在只剩一处。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

from app.agents.tools.base import DangerLevel, HealthHandler
from app.agents.tools.registry import tool_registry
from app.agents.tools.schema import derive_args_schema, parse_docstring


@dataclass
class ToolSpec:
    """装饰器解析出来的完整声明（也是工具治理界面读的东西）"""

    name: str
    display_name: str
    description: str
    category: str = "general"
    danger_level: DangerLevel = "safe"
    allowed_agents: list[str] = field(default_factory=list)
    read_only: bool = False
    hidden: bool = False
    source: str = "builtin"
    version: str = "1.0.0"
    args_schema: dict[str, Any] = field(default_factory=dict)


def agent_tool(
    name: str | None = None,
    *,
    display_name: str | None = None,
    description: str | None = None,
    category: str = "general",
    danger_level: DangerLevel = "safe",
    allowed_agents: list[str] | None = None,
    read_only: bool = False,
    hidden: bool = False,
    source: str = "builtin",
    version: str = "1.0.0",
    args_schema: dict[str, Any] | None = None,
    args_model: type[BaseModel] | None = None,
    args_docs: dict[str, str] | None = None,
    health_handler: HealthHandler | None = None,
) -> Callable:
    """
    注册一个内置工具。

    Args:
        name: 工具名（LLM 可见）；缺省用函数名
        display_name: 面向用户的中文名；缺省用工具名（内置工具应显式提供）
        description: 工具描述；缺省取 docstring 首段 —— 这一句是模型决定
            「要不要调这个工具」的唯一依据，写不清就会没人调
        category: 治理界面分组（文件 / 搜索 / 终端 / 网络 / 编排 / 记忆 ...）
        danger_level: safe / moderate / danger；danger 执行前走 HITL 审批
        allowed_agents: 允许使用的专员；空 = 全部开放
        read_only: 只读工具 —— 权限模式可以整体放行它而不动写操作
        hidden: 不在治理界面展示（内部工具用）
        args_schema: 显式覆盖自动推导的参数 Schema（一般不需要）
        args_model: 显式的 Pydantic 参数模型。嵌套对象（例如 `ask_user`
            的 question → options 两层数组）无法由扁平 JSON Schema 推导，
            这类工具必须同时提供它；它同时用于执行前校验与 LangChain 绑定。
        args_docs: 参数描述补丁，与 docstring 合并
    """

    def decorator(fn: Callable) -> Callable:
        tool_name = name or fn.__name__
        doc = parse_docstring(inspect.getdoc(fn))
        summary = (description or doc.summary or tool_name).strip()

        schema = args_schema or (
            args_model.model_json_schema() if args_model is not None else derive_args_schema(fn)
        )
        merged_docs = {**doc.param_docs, **(args_docs or {})}
        for prop_name, text in merged_docs.items():
            props = schema.get("properties") or {}
            if prop_name in props and not props[prop_name].get("description"):
                props[prop_name]["description"] = text

        spec = ToolSpec(
            name=tool_name,
            display_name=(display_name or tool_name).strip(),
            description=summary,
            category=category,
            danger_level=danger_level,
            allowed_agents=list(allowed_agents or []),
            read_only=read_only,
            hidden=hidden,
            source=source,
            version=version,
            args_schema=schema,
        )
        tool_registry.register(
            name=spec.name,
            handler=fn,
            description=spec.description,
            danger_level=spec.danger_level,
            allowed_agents=spec.allowed_agents or None,
            version=spec.version,
            args_schema=spec.args_schema,
            args_model=args_model,
            health_handler=health_handler,
            overwrite=True,
            category=spec.category,
            read_only=spec.read_only,
            hidden=spec.hidden,
            source=spec.source,
            display_name=spec.display_name,
        )
        fn.tool_spec = spec  # type: ignore[attr-defined]
        return fn

    return decorator


__all__ = ["ToolSpec", "agent_tool"]
