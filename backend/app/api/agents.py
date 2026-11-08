"""
智能体接口 —— 主对话那一路专员的定义。

★ 这一屏只管「智能体」，不管「数字员工」

专员（本接口）服务于**主对话**：Supervisor 拆解、子 Agent 执行。
数字员工服务于**工作空间**（`/api/workspace`），是「拿某个专员当模板的一次雇佣」。
两边都异构，但改动的爆炸半径完全不同 —— 改一个专员影响所有对话，
改一名员工只影响那个项目。所以这里不提供任何员工字段。

★ 为什么没有任务统计

曾经这里顺手聚合最近 N 条子任务的表现（成功率/耗时/最近一次）。
那批展示连同界面上的「调度清单」「最近子任务」两块一起删掉了：
统计离开了任务明细就是个没出处的数字，而这块界面该回答的是
「有谁、能干什么」，不是「跑得怎么样」—— 后者属于后台任务那一屏。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.agents.registry import (
    MAX_CUSTOM_AGENTS,
    NAME_RE,
    AgentSpec,
    clamp_agent_payload,
    get_registry,
)
from app.agents.tools.registry import tool_registry
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])


class AgentUpsertRequest(BaseModel):
    """新建/编辑专员的入参。全部可选 —— 更新走部分合并，不整包覆盖。"""

    name: Optional[str] = Field(None, description="专员标识（仅新建时给）")
    display_name: Optional[str] = Field(None, description="显示名")
    domain: Optional[str] = Field(None, description="领域一句话")
    responsibility: Optional[str] = Field(None, description="职责（进子 Agent 的 System Prompt）")
    planner_hint: Optional[str] = Field(None, description="调度视角的能力说明（进 Supervisor 清单）")
    capabilities: Optional[List[str]] = Field(None, description="人话能力名，Supervisor 按它选专员")
    tools_allowed: Optional[List[str]] = Field(None, description="显式工具白名单；空=跟随工具表")
    model: Optional[str] = Field(None, description="该专员的模型；空=跟随全局")
    temperature: Optional[float] = Field(None, description="采样温度；null=跟随全局 LLM_TEMPERATURE")
    max_steps: Optional[int] = Field(None, description="工具步数上限；null=跟随全局")
    retrieval: Optional[bool] = Field(None, description="规划前是否跑知识库检索")
    enabled: Optional[bool] = Field(None, description="是否启用")


def _view(spec: AgentSpec) -> Dict[str, Any]:
    """专员定义 + 界面要用的派生量（生效工具、权限来源）。"""
    data = spec.to_json()
    data["effective_tools"] = tool_registry.visible_tool_names(spec.name)
    #: 工具白名单是「跟随工具表」还是「显式指定」，决定「为什么勾了 3 个却显示 5 个」往哪查。
    data["tool_mode"] = "explicit" if spec.tools_allowed else "inherit"
    return data


@router.get("")
async def list_agents() -> Dict[str, Any]:
    """全部专员（内置在前）。"""
    registry = get_registry()
    specs = registry.all()
    return {
        "agents": [_view(s) for s in specs],
        "fallback": registry.fallback_name(),
        "max_custom": MAX_CUSTOM_AGENTS,
        "custom_count": sum(1 for s in specs if not s.builtin),
        "global_model": settings.LLM_MODEL,
        "global_max_steps": int(settings.AGENT_MAX_TOOL_STEPS),
    }


@router.post("")
async def create_agent(payload: AgentUpsertRequest) -> Dict[str, Any]:
    """新建自定义专员。"""
    registry = get_registry()
    name = str(payload.name or "").strip().lower()
    if not NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="专员标识只能用小写字母、数字与下划线，需以字母开头，长度 2-32",
        )
    spec, error = registry.create(name, clamp_agent_payload(payload.model_dump(exclude_none=True, exclude={"name"})))
    if spec is None:
        raise HTTPException(status_code=400, detail=error)
    logger.info("自定义专员已创建: %s", spec.name)
    return {"ok": True, "agent": _view(spec)}


@router.put("/{name}")
async def update_agent(name: str, payload: AgentUpsertRequest) -> Dict[str, Any]:
    """部分更新专员（未提及的字段保持原样）。下一轮对话即生效。"""
    registry = get_registry()
    current = registry.get(name)
    if current is None:
        raise HTTPException(status_code=404, detail="专员不存在")
    changes = payload.model_dump(exclude_none=True, exclude={"name"})
    #: 停用调度者 = 主对话没有入口，是**功能全停**，直接拒掉而不是默默接受。
    if "enabled" in changes and not changes["enabled"] and current.resident:
        raise HTTPException(
            status_code=400,
            detail="调度者是主对话的入口，不能停用",
        )
    spec = registry.upsert(name, changes)
    if spec is None:
        raise HTTPException(status_code=404, detail="专员不存在")
    return {"ok": True, "agent": _view(spec)}


@router.delete("/{name}")
async def delete_agent(name: str) -> Dict[str, Any]:
    """删除自定义专员。内置的删不掉（可停用、可恢复默认）。"""
    registry = get_registry()
    ok, reason = registry.delete(name)
    if not ok:
        code = 404 if reason == "专员不存在" else 400
        raise HTTPException(status_code=code, detail=reason)
    logger.info("自定义专员已删除: %s", name)
    return {"ok": True}


@router.post("/{name}/reset")
async def reset_agent(name: str) -> Dict[str, Any]:
    """恢复默认：内置的退回代码种子，自定义的等价于删除。"""
    registry = get_registry()
    spec, error = registry.reset(name)
    if error:
        raise HTTPException(status_code=404, detail=error)
    return {"ok": True, "agent": _view(spec) if spec else None}


@router.get("/{name}/prompt")
async def agent_prompt(name: str) -> Dict[str, Any]:
    """
    这个专员**实际**会被拼进 System Prompt 的身份段原文。

    「用户设了什么」和「模型真正看到了什么」之间隔着渲染（能力名怎么进清单、
    工具白名单收窄成哪几个、检索会不会额外加一段）。没有这个出口，
    改完职责只能靠「下一轮回答好像不太对了」来确认。

    ⚠️ 只给身份段：助手人设与长期记忆是运行时另外叠上去的
    （注入点在 `runner.run_planner_turn`），这不等于完整的 System Prompt。
    """
    from app.core.prompts import SUB_AGENT_PROMPT

    registry = get_registry()
    spec = registry.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail="专员不存在")
    meta = registry.describe_for_prompt(spec.name)
    tools_description = tool_registry.describe_for_agent(spec.name)
    identity = SUB_AGENT_PROMPT.format(
        agent_name=meta["agent_name"],
        domain=meta["domain"],
        responsibility=meta["responsibility"],
        tools_description=tools_description,
    )
    return {"identity": identity, "chars": len(identity)}
