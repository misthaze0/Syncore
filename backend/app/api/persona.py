"""
助手人设接口 —— 查看 / 修改 / 恢复默认 / 注入预览

★ 为什么提供 prompt-fragment 端点（与 /memory/prompt-block 同一条哲学）

「用户设了什么」和「模型真正看到了什么」之间隔着渲染逻辑：
语气预设展开、custom 回退、开关判断。把最终拼进 System Prompt 的文本
原样返回，「人设到底生效没生效」就不用猜 —— 元数据说错比没有更糟
（不变量 23 的教训在这里同样适用）。

★ 为什么 PUT 是部分更新

前端表单是逐字段编辑的，整包 PUT 会把「没渲染出来的字段」覆盖成旧值。
merged() 只覆盖显式给出的字段，让「改名字」与「改语气」互不踩脚。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.persona import PERSONA_PRESETS, PROACTIVE_LEVELS, get_persona_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/persona", tags=["persona"])

_HHMM_PATTERN = r"^(?:[01]\d|2[0-3]):[0-5]\d$"


class PersonaUpdateRequest(BaseModel):
    """部分更新：全部字段可选，只覆盖显式给出的"""

    name: Optional[str] = Field(None, min_length=1, max_length=24, description="助手名字")
    tagline: Optional[str] = Field(None, max_length=60, description="一句话角色定位")
    tone: Optional[str] = Field(None, description="语气预设 key 或 custom")
    tone_custom: Optional[str] = Field(None, max_length=200, description="tone=custom 时的自定义语气文本")
    instructions: Optional[str] = Field(None, max_length=1000, description="自定义人设说明")
    avatar: Optional[str] = Field(
        None, max_length=300_000, description="头像 data URL（前端先缩到 256px；纯 UI，不进 Prompt）"
    )
    traits: Optional[List[str]] = Field(None, max_length=8, description="性格标签（进 Prompt）")
    greeting: Optional[str] = Field(None, max_length=200, description="开场白（新会话欢迎语；纯 UI）")
    examples: Optional[List[str]] = Field(None, max_length=6, description="说话示例（进 Prompt 的口吻样例）")
    enabled: Optional[bool] = Field(None, description="是否注入 Prompt")
    apply_globally: Optional[bool] = Field(
        None, description="true = 普通对话与助理都注入；false = 仅助理命名空间"
    )
    preset: Optional[str] = Field(
        None, description=f"预设 key（仅用于回显，可选：{' / '.join(PERSONA_PRESETS)}）"
    )
    address_me: Optional[str] = Field(None, max_length=20, description="助手怎么称呼你（进 Prompt）")
    quirks: Optional[List[str]] = Field(None, max_length=4, description="口头禅/说话小习惯")
    time_awareness: Optional[bool] = Field(None, description="是否注入当前日期/时段")
    proactive: Optional[str] = Field(
        None, description=f"主动问候级别：{' / '.join(sorted(PROACTIVE_LEVELS))}"
    )
    quiet_start: Optional[str] = Field(None, pattern=_HHMM_PATTERN, description="静默开始（HH:MM）")
    quiet_end: Optional[str] = Field(None, pattern=_HHMM_PATTERN, description="静默结束（HH:MM）")
    proactive_min_gap_hours: Optional[int] = Field(
        None, ge=1, le=168, description="两次主动问候的最小间隔（小时）"
    )
    proactive_daily_max: Optional[int] = Field(
        None, ge=1, description="每日主动问候上限（不设封顶，由用户自己定）"
    )


@router.get("")
async def get_persona() -> Dict[str, Any]:
    """当前人设 + 可用语气预设 + 预设方案 + 元信息"""
    manager = get_persona_manager()
    profile = manager.get_profile()
    return {
        "profile": profile.to_json(),
        "tone_presets": manager.valid_tones(),
        "presets": PERSONA_PRESETS,
        "proactive_levels": sorted(PROACTIVE_LEVELS),
        "stats": manager.stats(),
    }


@router.put("")
async def update_persona(payload: PersonaUpdateRequest) -> Dict[str, Any]:
    """部分更新人设（下一轮回答即生效 —— system_prompt 每轮重新构造）"""
    manager = get_persona_manager()
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    profile = manager.update(**changes)
    logger.info("人设已更新: %s", list(changes.keys()))
    return {"ok": True, "profile": profile.to_json()}


@router.post("/reset")
async def reset_persona() -> Dict[str, Any]:
    """恢复默认人设"""
    profile = get_persona_manager().reset()
    return {"ok": True, "profile": profile.to_json()}


@router.get("/prompt-fragment")
async def persona_prompt_fragment(thread_id: str = "") -> Dict[str, Any]:
    """
    返回指定会话实际会注入的人设块原文（可验证性出口）。

    不传 thread_id 时按助理会话预览 —— 这个入口长在助理设置页上，
    默认要回答的是「助理对话里模型看到了什么」。

    稳定块与动态块分开返回：设置页需要展示「哪些内容不随时间变化」，
    而用户最常问的「为什么改完时间没变」答案就在这个分界上。
    """
    from app.conversations.store import ASSISTANT_MAIN_THREAD_ID

    target = thread_id or ASSISTANT_MAIN_THREAD_ID
    stable, dynamic = get_persona_manager().build_prompt_parts(target)
    fragment = "\n\n".join(part for part in (stable, dynamic) if part)
    return {
        "fragment": fragment,
        "chars": len(fragment),
        "empty": not fragment,
        "thread_id": target,
        "stable": stable,
        "dynamic": dynamic,
        "stable_chars": len(stable),
        "dynamic_chars": len(dynamic),
    }
