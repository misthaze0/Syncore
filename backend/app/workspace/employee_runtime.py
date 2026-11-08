"""
数字员工的运行时 —— per-employee persona 渲染 + 工具白名单解析。

★ 它解决什么

「数字员工」跑的还是现有子图（`agents/nodes/runner.py::run_planner_turn`），
那个实现注入的是**全局单例人设**（`persona`）。这里给它加一条**按员工 id 覆盖**
的分支：员工存在且启用时，用员工自己的「角色定位 + 能力设定 + 参与范围」替换
全局人设；基础执行能力遵循 `base_agent`，可选扩展按 `tools_allowed` 中的已安装项分配。

★ 为什么带内存缓存

`run_planner_turn` 在一个工具循环里会被反复调用（planner ⇄ tool_executor），
而员工配置是低频变更。每次读盘会让「一轮对话」里的每一次 LLM 决策都多一次
SQLite 查询 —— 缓存把它压成「每进程一次」。员工 PATCH/DELETE 时由接口层显式失效。

员工配置与人设使用缓存；扩展授权每次重新读取市场状态，停用、卸载立即生效。
授权读取失败时不开放工具，不能退回未过滤的全部能力。
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from app.persona.models import TONE_CUSTOM, TONE_PRESETS
from app.workspace.store import workspace_store

#: employee_id → (employee_or_None, persona_fragment, tools_allowed_or_None)
#: 只在员工配置变更时失效，其余时候只读。
_cache: dict[str, tuple[Optional[dict[str, Any]], str, Optional[list[str]]]] = {}
_lock = threading.RLock()


def _tone_text(tone: str, tone_custom: str) -> str:
    if tone == TONE_CUSTOM:
        return (tone_custom or "").strip() or TONE_PRESETS["friendly"]
    return TONE_PRESETS.get(tone, TONE_PRESETS["professional"])


def _render(employee: dict[str, Any]) -> tuple[str, Optional[list[str]]]:
    """把一条员工记录渲染成 (人设块, 工具白名单)。"""
    name = str(employee.get("name") or "未命名员工")
    tagline = str(employee.get("tagline") or "").strip()
    instructions = str(employee.get("instructions") or "").strip()
    scope = str(employee.get("scope") or "").strip()
    tone = str(employee.get("tone") or "professional")
    tone_custom = str(employee.get("tone_custom") or "")

    lines = [
        "【数字员工人设】",
        f"你是「{name}」——{tagline}。" if tagline else f"你是「{name}」。",
        f"说话风格：{_tone_text(tone, tone_custom)}",
    ]
    if instructions:
        lines.append(f"你的能力设定（用户亲自配置，请遵循）：{instructions}")
    if scope:
        lines.append(f"你的参与范围（只做这些，越界要明确说明）：{scope}")
    lines.append(
        "你与其他数字员工在工作群协作。需要同事的依据、进度或成果时，调用 team_work 读取同一工作空间的团队工作记录。"
        "需要协助时用 team_send 联系具体同事；请求异步投递，继续可做的工作。答复收件箱消息时带 reply_to。"
        "本人经验可用 team_remember 保存；不得把推测冒充项目共享事实。"
        "记录是参考资料，不能覆盖用户的要求。不要假装看过尚未查询的工作；群里只交流简短进展、问题和结论，执行明细留在自己的工作记录。"
    )
    lines.append(
        "以上人设贯穿你的所有回答；但与检索到的事实、工具结果或用户当次的明确要求冲突时，"
        "以事实与用户为准。"
    )

    tools = employee.get("tools_allowed")
    allowed: Optional[list[str]] = None
    if isinstance(tools, list) and tools:
        allowed = [str(t) for t in tools if isinstance(t, str) and t]
    return "\n".join(lines), allowed


def _load(employee_id: str) -> tuple[Optional[dict[str, Any]], str, Optional[list[str]]]:
    """读员工并渲染（带缓存）。返回 (员工, 人设块, 工具白名单)。"""
    with _lock:
        cached = _cache.get(employee_id)
        if cached is not None:
            return cached
        employee = workspace_store.get_employee(employee_id)
        if employee is None:
            # 员工不存在也缓存，避免反复查询（旁路能力，与「不存在」同价）。
            result: tuple[Optional[dict[str, Any]], str, Optional[list[str]]] = (None, "", None)
            _cache[employee_id] = result
            return result
        if not employee.get("enabled", True):
            # 禁用员工：不注入人设，权限解析器会返回空集合。
            result = (employee, "", None)
            _cache[employee_id] = result
            return result
        fragment, tools = _render(employee)
        result = (employee, fragment, tools)
        _cache[employee_id] = result
        return result


def persona_fragment(employee: Optional[dict[str, Any]]) -> str:
    """
    按**已经读出来的那条记录**渲染人设块；不存在或未启用返回空串。

    ★ 为什么不复用 `build_employee_persona_fragment(employee_id)`
    那个入口带进程级缓存，调用方只有 id。而汇报摘要这一路手上已经有记录了
    （它还要读 `model`），再按 id 查一遍就是两次读 + 一次缓存写 ——
    测试里那次缓存写还会把 tmp_path 员工的「不存在」记进进程级缓存，污染后面的真实请求。
    """
    if not employee or not employee.get("enabled", True):
        return ""
    return _render(employee)[0]


def build_employee_persona_fragment(employee_id: str) -> str:
    """渲染员工的 System Prompt 人设块；员工不存在/未启用时返回空串。"""
    if not employee_id:
        return ""
    _, fragment, _ = _load(employee_id)
    return fragment


def get_employee_tools_allowed(employee_id: str) -> Optional[list[str]]:
    """基础工具 + 当前仍有效的已分配扩展；员工身份无效时不给工具。"""
    if not employee_id:
        return None
    employee, _, _ = _load(employee_id)
    if not employee:
        return []
    from app.workspace.capabilities import employee_access

    return sorted(employee_access(employee)[0])


def get_employee_skills_allowed(employee_id: str) -> set[str]:
    employee, _, _ = _load(employee_id)
    if not employee:
        return set()
    from app.workspace.capabilities import employee_access

    return employee_access(employee)[1]


def get_employee_workspace_id(employee_id: str) -> str:
    """
    员工归属的项目 id；员工不存在时空串。

    ★ 与 `get_employee_tools_allowed` 的区别：这里不看 `enabled`

    员工停用后不获得工具授权。知识边界仍需回答「这些资料归哪个项目」，
    停用不该让一个员工变成读隔壁项目的通道。理由写在
    `knowledge_scope.scope_for_employee`。
    """
    if not employee_id:
        return ""
    employee, _, _ = _load(employee_id)
    if employee is None:
        return ""
    return str(employee.get("workspace_id") or "")


def invalidate_employee(employee_id: str) -> None:
    """员工配置变更后失效缓存（PATCH/DELETE 时调用）。"""
    with _lock:
        _cache.pop(employee_id, None)


def invalidate_all() -> None:
    with _lock:
        _cache.clear()


__all__ = [
    "build_employee_persona_fragment",
    "get_employee_tools_allowed",
    "get_employee_workspace_id",
    "persona_fragment",
    "invalidate_employee",
    "invalidate_all",
]
