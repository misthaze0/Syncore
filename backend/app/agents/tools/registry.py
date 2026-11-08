"""
★ 工具治理中心 —— 工具注册中心

职责:
1. 注册 / 注销工具（元信息、参数 JSON Schema、危险等级、Agent 权限、分类）
2. 按 Agent 权限动态发现工具（子 Agent 不硬编码工具，运行时从注册中心取）
3. 统一执行入口：参数校验 → 上下文绑定 → 计时 → 异常兜底
4. 批量健康检查（供 /api/tools 展示）
5. 将工具转换为 LangChain `BaseTool`，供 `llm.bind_tools()` 使用

安全模型:
- `danger_level == "danger"` 的工具在执行前必须经 HITL 人工审批（由 runner/tool_executor 拦截）
- `allowed_agents` 做最小权限分配，Agent 拿不到未授权的工具
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, ValidationError, create_model

from app.agents.tools.base import (
    DangerLevel,
    HealthHandler,
    RegisteredTool,
    ToolHandler,
    find_context_param,
)
from app.agents.tools.context import ToolContext
from app.computer_control import (
    ASSISTANT_AGENT,
    COMPUTER_CONTROL_SERVER,
    assistant_control_turn,
    computer_control_store,
)
from app.schemas.tools import ToolInfo, ToolResult

logger = logging.getLogger(__name__)

# JSON Schema 基础类型 → Python 类型
_JSON_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _build_args_model(tool_name: str, args_schema: dict | None) -> type[BaseModel] | None:
    """
    将工具的 JSON Schema 转换为 Pydantic 模型。

    支持 string / integer / number / boolean / array / object 六种基础类型与
    required 列表；未声明的额外参数被忽略（extra="ignore"）—— 模型偶尔会多塞
    一个无关参数，为这个报错会让整轮对话白跑。
    """
    if not args_schema or args_schema.get("type") != "object" or not args_schema.get("properties"):
        return None

    properties: dict[str, Any] = args_schema["properties"]
    required = set(args_schema.get("required", []))

    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        py_type = _JSON_TYPE_MAP.get(prop_schema.get("type", "string"), str)
        enum = prop_schema.get("enum")
        description = prop_schema.get("description", "")
        if enum:
            py_type = str if all(isinstance(v, str) for v in enum) else py_type
        if prop_name in required:
            fields[prop_name] = (py_type, Field(..., description=description))
        else:
            default = prop_schema.get("default")
            # 可选项一律放宽成 Optional：JSON Schema 里「没有 default 的可选参数」
            # 和 Python 里「默认 None」是同一件事，模型不传时得有个合法取值。
            fields[prop_name] = (Optional[py_type], Field(default=default, description=description))

    model_name = "".join(part.capitalize() for part in tool_name.split("_")) + "Args"
    try:
        return create_model(model_name, **fields)
    except Exception as e:  # noqa: BLE001
        # 某个 server 报了奇怪的 schema 时，宁可退化成「不校验」也不能拖垮整个注册中心
        logger.warning("工具 %s 的参数模型构建失败，跳过参数校验: %s", tool_name, e)
        return None


def _agent_allowlist(agent_name: str) -> list[str] | None:
    """
    该专员的**显式**工具白名单；None = 没配，走各工具自报的 `allowed_agents`。

    ★ 为什么用惰性 import
    `app.agents.registry` 读的是 `data/agents.json`，而工具注册在启动期就发生。
    模块级 import 会把「工具注册」和「读盘」绑在一起（导入顺序、循环、
    以及单测里换存储路径的时机都会变得微妙）。这里每次调用现取一次：
    注册表自己带 stat 缓存，读一次内存判断而已。
    """
    from app.agents.registry import get_registry

    try:
        return get_registry().tools_for(agent_name)
    except Exception as e:  # noqa: BLE001
        # ⚠️ 退回「跟随工具表」而不是抛：专员配置读不出来时，
        # 系统的正确行为是按默认权限继续跑，而不是整条对话报错。
        logger.warning("读取专员工具白名单失败（跟随工具表默认权限）: %s", e)
        return None


#: 联网类工具的分类名（与 `@agent_tool(category="网络")` 一致）
WEB_CATEGORY = "网络"


def _apply_web_switch(
    tools: list[RegisteredTool], web_enabled: bool
) -> list[RegisteredTool]:
    """
    本轮关掉联网时把整类联网工具摘掉。

    ★ 为什么不写 `!= "web_search"`
    原先只摘 `web_search`，`web_fetch`（读网页）漏在外面 —— 用户关了联网开关，
    助手却还能抓网页。按分类摘才有穷尽性：以后再加联网工具，只要归到这个分类
    就自动受开关约束，不需要想起来改这里。
    """
    if web_enabled:
        return tools
    return [t for t in tools if t.info.category != WEB_CATEGORY]


class ToolRegistry:
    """工具注册中心（单例）"""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        #: 工具名 → 参数校验模型
        self._arg_models: dict[str, type[BaseModel] | None] = {}

    # ---------- 注册 / 注销 ----------
    def register(
        self,
        name: str,
        handler: ToolHandler,
        description: str,
        danger_level: DangerLevel = "safe",
        allowed_agents: list[str] | None = None,
        version: str = "1.0.0",
        args_schema: dict | None = None,
        args_model: type[BaseModel] | None = None,
        health_handler: HealthHandler | None = None,
        overwrite: bool = False,
        category: str = "general",
        read_only: bool = False,
        hidden: bool = False,
        source: str = "builtin",
        server: str = "",
        display_name: str = "",
    ) -> None:
        """
        注册一个工具。

        Args:
            name: 工具唯一名（LLM 可见）
            handler: 实际执行函数，支持同步/异步，参数以关键字传入
            description: 工具描述（LLM 据此决定是否调用，越清晰越好）
            danger_level: safe / moderate / danger；danger 会触发 HITL 审批
            allowed_agents: 允许使用的 Agent 名列表；传 None 或 [] 表示对全部 Agent 开放
            version: 版本号
            args_schema: 参数 JSON Schema（`@agent_tool` 会自动从签名推导）
            args_model: 显式的 Pydantic 参数模型（嵌套对象必须用它；
                扁平 JSON Schema 推导不出来两层数组的结构）
            health_handler: 可选健康检查函数
            overwrite: 是否覆盖同名工具
            category: 治理界面分组
            read_only: 只读工具（权限模式可整体放行）
            hidden: 不在治理界面展示
            source: builtin / mcp（技能不注册工具，见 `app/schemas/tools.py` 的注释）
            server: MCP server 名（内置工具留空）；电脑操控按它识别桌面工具
            display_name: 面向用户的中文名；缺省退回工具名（内置工具应显式提供）
        """
        if name in self._tools and not overwrite:
            logger.warning("工具 %s 已注册，跳过（如需覆盖请传 overwrite=True）", name)
            return

        info = ToolInfo(
            name=name,
            display_name=(display_name or name).strip(),
            description=description,
            danger_level=danger_level,
            allowed_agents=allowed_agents or [],
            version=version,
            args_schema=args_schema,
            health="unknown",
            category=category,
            source=source,
            server=server,
            read_only=read_only,
            hidden=hidden,
        )
        self._tools[name] = RegisteredTool(
            info=info,
            handler=handler,
            health_handler=health_handler,
            context_param=find_context_param(handler),
        )
        self._arg_models[name] = args_model or _build_args_model(name, args_schema)
        logger.debug("工具已注册: %s (danger=%s, agents=%s)", name, danger_level, allowed_agents or "ALL")

    def unregister(self, name: str) -> bool:
        removed = self._tools.pop(name, None) is not None
        if removed:
            self._arg_models.pop(name, None)
        return removed

    def clear(self) -> None:
        """清空注册表（测试用）"""
        self._tools.clear()
        self._arg_models.clear()

    # ---------- 发现 ----------
    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def get_info(self, name: str) -> ToolInfo | None:
        tool = self.get(name)
        return tool.info if tool else None

    def list_all(self, agent_name: str | None = None, include_hidden: bool = False) -> list[ToolInfo]:
        """列出所有工具元信息；传 agent_name 时只返回该 Agent 有权限的工具"""
        tools = self._visible(agent_name) if agent_name else list(self._tools.values())
        infos = [t.info for t in tools]
        return infos if include_hidden else [i for i in infos if not i.hidden]

    def categories(self) -> dict[str, int]:
        """分类 → 工具数（治理界面分组用）"""
        out: dict[str, int] = {}
        for tool in self._tools.values():
            if tool.info.hidden:
                continue
            out[tool.info.category] = out.get(tool.info.category, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    # ---------- 专员视角的有效工具（注册表可覆写工具自报的 allowed_agents） ----------
    def _visible(self, agent_name: str) -> list[RegisteredTool]:
        """
        这个专员**实际**能看到哪些工具。两条路径：

        - 专员没配显式白名单（默认那一路）→ 按各工具自报的 `allowed_agents`。
        - 专员配了显式白名单 → 以那份清单为准（未注册的名字如实忽略）。

        ★ 为什么绑定、prompt、权限矩阵、执行校验四条路径必须共用它
        各自算一遍就会出现「prompt 里列了 5 个工具、模型手上只有 3 个」或反过来 ——
        而模型会去调那个它看得见却调不到的工具。`execute` 也走同一个判断，
        否则「专员显式授权了一个 `allowed_agents` 里没有的工具」会在绑定期放行、
        执行期被拒 —— 那是最难查的一种错配，界面上一切正常。
        """
        allowlist = _agent_allowlist(agent_name)
        if allowlist is None:
            tools = [t for t in self._tools.values() if t.allowed_for(agent_name)]
        else:
            tools = [self._tools[name] for name in allowlist if name in self._tools]
        # hidden 是「内部工具」标记：既不展示，也不绑定 —— 只藏界面等于模型照样能调。
        tools = [t for t in tools if not t.info.hidden]
        # ★ 电脑操控只给助理（supervisor 的 direct 循环），不参与专员分发。
        # 即使有人把 MCP 的 allowed_agents 配成别的专员，这里也会把它摘掉 ——
        # 「只给助理」是产品边界，不能依赖一份可被界面改写的配置。
        if agent_name != ASSISTANT_AGENT:
            tools = [t for t in tools if t.info.server != COMPUTER_CONTROL_SERVER]
        return tools

    def _permitted(self, tool_name: str, agent_name: str) -> bool:
        allowlist = _agent_allowlist(agent_name)
        if allowlist is None:
            tool = self._tools.get(tool_name)
            return bool(tool and tool.allowed_for(agent_name))
        return tool_name in allowlist

    def visible_tool_names(self, agent_name: str) -> list[str]:
        """该专员实际能看到的工具名（设置页的权限矩阵与「生效工具数」用它）。"""
        return [t.name for t in self._visible(agent_name)]

    def get_tools_for_agent(
        self, agent_name: str, web_enabled: bool = True
    ) -> list[BaseTool]:
        """
        ★ 获取 Agent 可用的工具列表（按权限过滤）

        返回 LangChain `StructuredTool`，可直接传给 `llm.bind_tools()`。
        Agent 无法看到、也无法调用未被授权的工具。

        `web_enabled=False` 时整类联网工具都不绑定 —— 「关掉联网」的正确实现是
        不给工具，不是在 prompt 里劝模型别搜（它拿着工具就会搜）。
        """
        tools = _apply_web_switch(self._visible(agent_name), web_enabled)
        return [self._to_langchain_tool(tool) for tool in tools]

    def get_tools_for_main(
        self,
        danger_levels: tuple[str, ...] = ("safe", "moderate", "danger"),
        web_enabled: bool = True,
        extra_names: set[str] | None = None,
        exclude_names: set[str] | None = None,
    ) -> list[BaseTool]:
        """主对话聚合所有风险等级的工具；执行前统一判定，需要批准时由 broker 等待。

        MCP 的 allowed_agents 和电脑操控的助理作用域仍然生效。
        """
        return [
            self._to_langchain_tool(tool)
            for tool in self._main_visible(
                danger_levels=danger_levels,
                web_enabled=web_enabled,
                extra_names=extra_names,
                exclude_names=exclude_names,
            )
        ]

    def describe_for_agent(
        self, agent_name: str, exclude_names: set[str] | None = None
    ) -> str:
        """生成「可用工具」的自然语言描述，用于拼进 System Prompt。

        `exclude_names` 用于同一专员在不同作用域下的描述收窄：supervisor 平时
        也能在 `_visible` 里看到电脑操控工具，但普通对话里它根本绑不到 ——
        描述里若照常列出，模型会去调一个它手上没有的工具。
        """
        tools = self._visible(agent_name)
        if exclude_names:
            tools = [t for t in tools if t.name not in exclude_names]
        if not tools:
            return "（该专员暂无可用工具，请基于自身知识直接回答）"
        lines = []
        for t in tools:
            flag = " ⚠️需人工审批" if t.danger_level == "danger" else ""
            lines.append(f"- {t.info.display_name}（{t.name}）: {t.info.description}{flag}")
        return "\n".join(lines)

    def _main_exclusion_reason(
        self,
        tool: RegisteredTool,
        web_enabled: bool,
        extra: set[str],
        danger_levels: tuple[str, ...] = ("safe", "moderate", "danger"),
    ) -> str:
        """主对话为什么拿不到这个工具；空串 = 可用。

        MCP 连接器的 `allowed_agents` 是用户在治理界面写的硬约束：
        主对话不受内置工具的专员分工限制，但**不能**绕过用户显式写给连接器的范围。
        """
        info = tool.info
        if info.hidden:
            return "hidden"
        # 电脑操控是「助理专属」：普通对话与专员都不绑，只有助理窗口 + 总开关时
        # 由 `extra_names` 显式加回来。台账要能说清这条边界，不能笼统归成 danger。
        if info.server == COMPUTER_CONTROL_SERVER and tool.name not in extra:
            return "desktop"
        if tool.danger_level not in danger_levels and tool.name not in extra:
            return "danger"
        if not web_enabled and info.category == WEB_CATEGORY:
            return "web_off"
        if info.source == "mcp" and info.allowed_agents and "supervisor" not in info.allowed_agents:
            return "mcp_scope"
        return ""

    def _main_visible(
        self,
        danger_levels: tuple[str, ...] = ("safe", "moderate", "danger"),
        web_enabled: bool = True,
        extra_names: set[str] | None = None,
        exclude_names: set[str] | None = None,
    ) -> list[RegisteredTool]:
        """
        主对话（Supervisor direct 模式）**实际绑定**的那一份工具集合。

        ★ 绑定、描述、能力台账三处必须共用它
        原先绑定用一套聚合规则、prompt 里又手写「查天气 / 查邮件」，
        两边各自维护的结果是「模型被告知的工具」和「手上真有的工具」对不上。
        """
        extra = set(extra_names or ())
        excluded = set(exclude_names or ())
        tools: list[RegisteredTool] = []
        for tool in self._tools.values():
            if tool.name in excluded:
                continue
            if self._main_exclusion_reason(tool, web_enabled, extra, danger_levels):
                continue
            tools.append(tool)
        return tools

    def main_tool_availability(
        self,
        web_enabled: bool = True,
        extra_names: set[str] | None = None,
    ) -> dict[str, str]:
        """工具名 → 主对话不可用的原因（可用工具不出现在结果里）。"""
        extra = set(extra_names or ())
        out: dict[str, str] = {}
        for tool in self._tools.values():
            reason = self._main_exclusion_reason(tool, web_enabled, extra)
            if reason:
                out[tool.name] = reason
        return out

    def main_tool_names(
        self,
        web_enabled: bool = True,
        extra_names: set[str] | None = None,
    ) -> list[str]:
        """主对话实际能调用的工具名（能力台账与「是否统一」校验用它）。"""
        return [t.name for t in self._main_visible(web_enabled=web_enabled, extra_names=extra_names)]

    def describe_for_main(
        self,
        web_enabled: bool = True,
        extra_names: set[str] | None = None,
        exclude_names: set[str] | None = None,
    ) -> str:
        """主对话可用工具的自然语言描述（规划与直答共用同一份绑定结果）。"""
        tools = self._main_visible(web_enabled=web_enabled, extra_names=extra_names)
        if exclude_names:
            tools = [t for t in tools if t.name not in exclude_names]
        if not tools:
            return "（本轮没有可用工具，请基于自身知识直接回答）"
        lines = []
        for t in tools:
            flag = " ⚠️需人工审批" if t.danger_level == "danger" else ""
            lines.append(f"- {t.info.display_name}（{t.name}）: {t.info.description}{flag}")
        return "\n".join(lines)

    def display_name_for(self, tool_name: str) -> str:
        """工具名 → 面向用户的中文名；未注册时退回工具名。"""
        info = self.get_info(tool_name)
        return info.display_name if info and info.display_name else tool_name

    def _to_langchain_tool(self, tool: RegisteredTool) -> BaseTool:
        """把 RegisteredTool 包装成 LangChain 工具（执行仍统一走 registry.execute）"""

        async def _run(**kwargs: Any) -> str:
            result = await self.execute(tool.name, kwargs, agent_name=None)
            if not result.success:
                return f"[工具执行失败] {result.error}"
            return result.result

        return StructuredTool.from_function(
            coroutine=_run,
            name=tool.name,
            description=tool.info.description,
            args_schema=self._arg_models.get(tool.name)
            or _build_args_model(tool.name, tool.info.args_schema),
        )

    # ---------- 执行 ----------
    async def execute(
        self,
        tool_name: str,
        args: dict | None = None,
        agent_name: str | None = None,
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        """
        统一执行入口。

        ★ 审批仍然**不在这里**做（不变量 6）
        「挂起等用户点批准」需要检查点恢复语义，只有图上的 tool_executor 节点有。
        这里做的是两件不同的事：deny 兜底（任何路径都生效，包括脱离 agent 循环的直调），
        以及拒绝「非审批路径上的 ask」。见下面的注释。

        Args:
            tool_name: 工具名（未注册时错误文案会列出可用工具，模型可自行纠正）
            args: 参数字典
            agent_name: 调用方 Agent（用于权限校验）；ctx 存在时以 ctx 为准
            ctx: 运行时上下文（thread / task / 工作目录 / emit）。
                ★ **agent 循环必须传它**：缺省时这里会兜一个空 `ToolContext`，
                节点不会报错，但工具看到的 `session_key` 是空串 ——
                「写之前先读」的校验会一律放行、shell 的 cwd 会全进程共用一套。
                也就是两项会话语义**同时静默失效**。构造点见
                `context_from_state`（两条循环各调一次，别在别处手搓）。
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            known = ", ".join(sorted(self._tools))
            return ToolResult(
                success=False,
                result="",
                duration_ms=0,
                error=f"工具未注册: {tool_name}。可用工具: {known}",
            )

        caller = agent_name or (ctx.agent_name if ctx else "")
        if caller and not self._permitted(tool_name, caller):
            return ToolResult(
                success=False,
                result="",
                duration_ms=0,
                error=f"Agent '{caller}' 无权调用工具 '{tool_name}'",
            )

        context = ctx or ToolContext()
        context.agent_name = context.agent_name or caller
        if context.employee_id:
            from app.workspace.employee_runtime import get_employee_tools_allowed

            try:
                permitted = tool_name in (get_employee_tools_allowed(context.employee_id) or [])
            except Exception:
                permitted = False
            if not permitted:
                return ToolResult(success=False, result="", duration_ms=0,
                                  error="该扩展未分配给当前员工，或已在插件市场停用、卸载")

        # ---------- 电脑操控的硬门 ----------
        # ★ 为什么在这里而不是只靠绑定期过滤
        # 绑定决定「模型看得见什么」，执行是**所有路径**的汇合点。少这一道，
        # 定时任务 / 编排 / 将来的新代码仍能把 win_* 工具执行出去 ——
        # 而「只给助理」这条边界会变成一句只在对话里成立的约定。
        is_desktop = tool.info.server == COMPUTER_CONTROL_SERVER
        assistant_control = is_desktop and assistant_control_turn(context.thread_id)
        if is_desktop and not assistant_control:
            reason = (
                "总开关没开"
                if not computer_control_store.enabled
                else "当前不是助理会话"
            )
            logger.warning(
                "电脑操控调用被拒（%s）: %s thread=%s", reason, tool_name, context.thread_id or "-"
            )
            return ToolResult(
                success=False,
                result="",
                duration_ms=0,
                error=f"电脑操控只对助理开放，且需要先在设置里开启总开关（{reason}）。",
            )

        invalid = self._validate_args(tool_name, args)
        if invalid is not None:
            # 参数错误也当成一次「失败的工具调用」回给模型：这是它能自行纠正的唯一信号
            logger.info("工具 %s 参数校验失败: %s", tool_name, invalid)
            return ToolResult(success=False, result="", duration_ms=0, error=invalid)

        # 执行前重新套用最新权限上限。审批只在驱动层等待，这里只读配置与凭据。
        # policy_handled 仅表示调用来源，不能作为用户批准的证明。
        from app.permissions.policy import decide as _policy_decide, effective_mode

        context.permission_mode = effective_mode(context.permission_mode)
        blocked = _policy_decide(
            tool_name,
            args,
            danger_level=tool.danger_level,
            mode=context.permission_mode,
            assistant_control=assistant_control,
        )
        if blocked.denied:
            logger.info("权限规则拦下工具调用: %s (%s)", tool_name, blocked.reason)
            return ToolResult(
                success=False,
                result="",
                duration_ms=0,
                error=f"该操作被权限设置禁止：{blocked.reason}",
            )
        confirmed = False
        if blocked.needs_approval and context.call_id:
            from app.approval import approval_store
            receipt = approval_store.find_by_call(
                context.thread_id, context.call_id, run_id=context.run_id,
                employee_id=context.employee_id,
            )
            confirmed = bool(receipt and receipt.decision == "approved"
                             and receipt.decided_by in ("user", "grant")
                             and receipt.tool_name == tool_name and receipt.args == (args or {}))
        if blocked.needs_approval and not confirmed:
            logger.warning(
                "非审批路径请求危险工具，已拒绝: %s thread=%s", tool_name, context.session_key or "-"
            )
            return ToolResult(
                success=False,
                result="",
                duration_ms=0,
                error=(
                    f"这一步需要你批准（{blocked.reason}），但当前调用路径没有确认环节。"
                    "请把它交给对话或子任务去发起，那里会弹出批准。"
                ),
            )

        if is_desktop:
            logger.info("执行桌面工具 %s (thread=%s)", tool_name, context.session_key or "-")
            from app.computer_runtime import desktop_runtime

            return await desktop_runtime.execute(tool, args or {}, context)
        logger.info("执行工具 %s (agent=%s, thread=%s, args=%s)", tool_name, caller or "-", context.session_key or "-", args)
        return await tool.invoke(args, context)

    def _validate_args(self, tool_name: str, args: dict | None) -> str | None:
        """
        执行前校验参数，返回错误文案（None = 通过）。

        校验放在注册中心而不是 handler 里：原先靠 `except TypeError` 兜参数问题，
        文案是 Python 的「missing 1 required positional argument: 'path'」——
        对模型没有指导性，而且真正的运行时 TypeError 会被误判成参数错误。
        """
        model = self._arg_models.get(tool_name)
        if model is None:
            return None
        payload = dict(args or {})
        try:
            model(**payload)
        except ValidationError as e:
            hints = []
            for err in e.errors():
                loc = ".".join(str(p) for p in err.get("loc", ())) or "参数"
                hints.append(f"{loc}: {err.get('msg')}")
            schema = self._tools[tool_name].info.args_schema or {}
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            shape = ", ".join(
                f"{k}{'(必填)' if k in required else '(可选)'}" for k in props
            )
            return f"参数校验失败 → {'；'.join(hints)}。该工具接受: {shape or '无参数'}"
        except Exception as e:  # noqa: BLE001
            logger.debug("工具 %s 参数校验异常（放行）: %s", tool_name, e)
        return None

    # ---------- 健康检查 ----------
    async def health_check(self, agent_name: str | None = None) -> dict[str, str]:
        """
        批量健康检查。

        Returns:
            {tool_name: "healthy" | "down"}
        """
        status: dict[str, str] = {}
        targets = self._visible(agent_name) if agent_name else list(self._tools.values())
        for tool in targets:
            ok = await tool.check_health()
            status[tool.name] = "healthy" if ok else "down"
            tool.info.health = status[tool.name]
        return status

    def stats(self) -> dict[str, int]:
        """注册中心统计信息"""
        by_danger: dict[str, int] = {"safe": 0, "moderate": 0, "danger": 0}
        for tool in self._tools.values():
            by_danger[tool.danger_level] = by_danger.get(tool.danger_level, 0) + 1
        return {"total": len(self._tools), **by_danger}


# 全局单例：全项目共享一个注册中心
tool_registry = ToolRegistry()


__all__ = ["ToolRegistry", "tool_registry"]
