"""员工可分配的扩展来自插件市场的已安装清单，运行时按当前状态解析。"""

from dataclasses import dataclass, field
from typing import Any

from app.agents.tools.registry import tool_registry
from app.computer_control import COMPUTER_CONTROL_SERVER
from app.marketplace import get_market_store
from app.builtin_plugins import get_builtin


@dataclass
class EmployeeCapability:
    name: str
    display_name: str
    description: str
    kind: str
    tools: set[str] = field(default_factory=set)
    skills: set[str] = field(default_factory=set)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "display_name": self.display_name,
                "description": self.description, "kind": self.kind,
                "legacy_tool_names": sorted(self.tools) if self.kind == "mcp" else []}


def installed_capabilities() -> dict[str, EmployeeCapability]:
    """使用市场同一份库存，包含本地安装的扩展；分页不遗漏超过 100 项的情况。"""
    market = get_market_store()
    response = market.search(view="installed", page=1, page_size=100, with_refs=False)
    items = list(response["items"])
    for page in range(2, (int(response["total"]) + 99) // 100 + 1):
        items.extend(market.search(view="installed", page=page, page_size=100, with_refs=False)["items"])
    ready = {f"{item['kind']}:{item['canonical']}": item for item in items
             if item.get("installed") and item.get("enabled") and not item.get("configuration_required")}
    tools = tool_registry.list_all()
    result: dict[str, EmployeeCapability] = {}
    for key, item in ready.items():
        kind = item["kind"]
        builtin = get_builtin(item["canonical"])
        if builtin:
            result[key] = EmployeeCapability(key, item["name"], item.get("description") or "", kind,
                tools={tool.name for tool in tools if tool.source == "builtin" and tool.name in builtin["tools"]},
                skills=set(builtin["skills"]))
            continue
        if kind == "plugin":
            continue
        names = {tool.name for tool in tools if tool.source == "mcp"
                 and tool.server == item["canonical"] and tool.server != COMPUTER_CONTROL_SERVER}
        # 未连接的连接器没有已注册工具，不能成为一次看似成功的授权。
        if kind == "mcp" and not names:
            continue
        result[key] = EmployeeCapability(key, item["name"], item.get("description") or "", kind,
                                        tools=names if kind == "mcp" else set(),
                                        skills={item["canonical"]} if kind == "skill" else set())

    def resolve_bundle(key: str, visiting: set[str]) -> EmployeeCapability | None:
        if key in result:
            return result[key]
        item = ready.get(key)
        if not item or key in visiting or item["kind"] != "plugin":
            return None
        children = []
        for component in item.get("components") or []:
            child_key = f"{component['kind']}:{component['ref']}"
            child = result.get(child_key) or resolve_bundle(child_key, visiting | {key})
            if child is None:
                return None
            children.append(child)
        if not children:
            return None
        capability = EmployeeCapability(key, item["name"], item.get("description") or "", "plugin",
                                        tools=set().union(*(child.tools for child in children)),
                                        skills=set().union(*(child.skills for child in children)))
        result[key] = capability
        return capability

    for key, item in ready.items():
        if item["kind"] == "plugin":
            resolve_bundle(key, set())
    return result


def validate_capabilities(selections: list[str]) -> list[str]:
    if not selections:
        return []
    available = installed_capabilities()
    invalid = [name for name in selections if name not in available]
    if invalid:
        raise ValueError("只能选择插件市场中已安装且启用的扩展，请刷新列表后重试")
    return list(dict.fromkeys(selections))


def employee_access(employee: dict[str, Any]) -> tuple[set[str], set[str]]:
    """基础执行工具遵循能力模板；可选扩展只来自员工选中的有效安装项。"""
    if not employee.get("enabled", True):
        return set(), set()
    baseline = {tool.name for tool in tool_registry.list_all(include_hidden=True)
                if tool.source == "builtin" and tool.name not in ("use_skill", "skills_list")}
    selections = set(employee.get("tools_allowed") or [])
    if not selections:
        return baseline, set()
    # 每次重新读取市场状态，避免缓存让停用/卸载的扩展继续被调用。
    available = installed_capabilities()
    skills: set[str] = set()
    for key, capability in available.items():
        if key in selections:
            baseline.update(capability.tools)
            skills.update(capability.skills)
        elif capability.kind == "mcp":
            baseline.update(capability.tools & selections)
    if skills:
        baseline.update(("use_skill", "skills_list"))
    return baseline, skills
