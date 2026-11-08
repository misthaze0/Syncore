"""
技能工具 —— 渐进式披露的 L2 / L3 入口

★ 为什么两个工具就够（而不是一个技能一个工具）

`use_skill` 是**取说明**，不是**执行技能**。技能本体是一份 markdown 指令 +
若干材料文件，它没有"运行"这个动作：读完之后仍然用既有的 read / write / bash
去干活。所以这里不需要为每个技能生成一个工具定义 ——
那会让 N 份 schema 进入**每一个**请求，而技能数量正是要往上长的。

L1（名字 + 一句话用途）由 `SkillStore.build_prompt_block()` 常驻注入，
模型看到的就是"有哪些事她有更详细的做法"；对上了才调 `use_skill` 取全文。
`skills_list` 只在两种时候需要：注入预算把清单截断了，或者用户问"你装了哪些技能"
（那时需要看到**被关闭的**和**读不出来的**条目，而 L1 块里只有生效的）。
"""

from __future__ import annotations

import logging

from app.agents.tools.decorator import agent_tool
from app.agents.tools.context import ToolContext
from app.skills import get_skill_store

logger = logging.getLogger(__name__)


def _health() -> bool:
    """健康检查：能扫描到目录即为健康（清单为空不算坏）"""
    try:
        get_skill_store().scan()
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("技能扫描健康检查失败: %s", e)
        return False


@agent_tool(
    display_name="使用技能",
    description=(
        "取回一个已安装技能的完整做法说明（含它的材料文件清单）。"
        "用户的诉求对上了【可用技能】里的某条描述时，先调用它拿到具体步骤再动手。"
    ),
    category="技能",
    danger_level="safe",
    read_only=True,
    health_handler=_health,
)
def use_skill(name: str, ctx: ToolContext | None = None) -> str:
    """加载技能正文。

    Args:
        name: 技能名，必须逐字来自【可用技能】清单或 `skills_list`（大小写不敏感）
    """
    if not (name or "").strip():
        return "需要指定技能名。先用 skills_list 看本机装了哪些技能。"
    if ctx and ctx.employee_id:
        from app.workspace.employee_runtime import get_employee_skills_allowed

        allowed = get_employee_skills_allowed(ctx.employee_id)
        name = next((item for item in allowed if item.casefold() == name.strip().casefold()), "")
        if not name:
            return "该技能未分配给当前员工，或已在插件市场停用、卸载。"
    try:
        return get_skill_store().load_instructions(name)
    except Exception as e:  # noqa: BLE001
        # 技能是增益：读挂了要说清挂了，而不是返回空串让模型以为"这个技能没内容"
        logger.warning("加载技能失败（name=%s）: %s", name, e)
        return f"（加载技能「{name}」时出错：{e}）"


@agent_tool(
    display_name="查看技能列表",
    description=(
        "列出本机安装的全部技能，包括已关闭的和读不出来的（SKILL.md 有问题的那些年）。"
        "用户问「你有哪些技能」「那个技能怎么没生效」，或注入清单被截断时调用。"
    ),
    category="技能",
    danger_level="safe",
    read_only=True,
    health_handler=_health,
)
def skills_list(ctx: ToolContext | None = None) -> str:
    """列出全部技能及其状态。"""
    try:
        if ctx and ctx.employee_id:
            from app.workspace.employee_runtime import get_employee_skills_allowed

            allowed = get_employee_skills_allowed(ctx.employee_id)
            return get_skill_store().build_prompt_block(allowed_names=allowed) or "当前员工没有已分配的可用技能。"
        return get_skill_store().describe_all()
    except Exception as e:  # noqa: BLE001
        logger.warning("列出技能失败: %s", e)
        return f"（暂时读不到技能清单：{e}）"


__all__ = ["skills_list", "use_skill"]
