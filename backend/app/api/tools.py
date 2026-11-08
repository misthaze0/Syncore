"""
工具治理查询接口 (Phase 2)

对外暴露工具注册中心的「已注册工具」与「健康状态」，
供前端设置页 / 调试面板展示，也可用于运维巡检。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from app.agents.tools.registry import tool_registry
from app.schemas.tools import ToolListResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=ToolListResponse)
async def list_tools(
    agent: str | None = Query(default=None, description="只返回该 Agent 有权限使用的工具"),
    check_health: bool = Query(default=False, description="是否实时执行健康检查（可能较慢）"),
) -> ToolListResponse:
    """查询已注册工具列表"""
    if check_health:
        try:
            await tool_registry.health_check(agent_name=agent)
        except Exception as e:  # noqa: BLE001
            logger.warning("工具健康检查失败: %s", e)

    tools = tool_registry.list_all(agent_name=agent)

    from app.agents.registry import get_registry

    known_agents = {spec.name for spec in get_registry().all()}
    scope_warnings = []
    for tool in tools:
        unknown = [name for name in tool.allowed_agents if name not in known_agents]
        if unknown:
            scope_warnings.append({
                "tool": tool.name,
                "display_name": tool.display_name or tool.name,
                "unknown_agents": unknown,
            })

    return ToolListResponse(
        total=len(tools),
        stats=tool_registry.stats(),
        categories=tool_registry.categories(),
        tools=tools,
        main_tools=tool_registry.main_tool_names(),
        main_unavailable=tool_registry.main_tool_availability(),
        scope_warnings=scope_warnings,
    )


@router.get("/health")
async def tools_health() -> dict:
    """批量健康检查（等价于 /api/tools?check_health=true 的精简版）"""
    status = await tool_registry.health_check()
    return {
        "total": len(status),
        "healthy": sum(1 for v in status.values() if v == "healthy"),
        "details": status,
    }
