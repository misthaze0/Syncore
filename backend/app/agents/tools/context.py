"""
工具运行时上下文

★ 为什么工具需要一个「模型看不见」的入参

工具的 handler 迟早要知道「是谁在调我」：文件工具要用调用方所在会话的工作目录，
memory/todo 工具要写进正确的 thread，任务工具要认得出是哪个数字员工发起的。
把这些塞进 JSON Schema 让模型填是错的 —— 模型既可能填错（把别人的 thread_id 填进来，
等于跨会话越权），也会为每个调用多花 token。

所以它们是 `ToolContext`：形参标注上这个类型就从 schema 里消失，由注册中心在
调用前用 contextvar 注入。执行入口 `tool_registry.execute()` 负责绑定，
两条 agent 循环（主对话 supervisor、子图 runner→tool_executor）都走它。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from app.config import settings


@dataclass
class ToolContext:
    """一次工具调用的运行时身份"""

    agent_name: str = ""
    thread_id: str = ""
    run_id: str = ""
    call_id: str = ""
    #: 后台子任务 id（主对话为空）
    task_id: str = ""
    #: 数字员工 id（工作空间形态下的会话级隔离用；主对话为空）
    employee_id: str = ""
    #: 本次调用的文件根目录；None = 用配置的沙箱默认值
    workspace_root: Path | None = None
    #: 权限模式（`manual` / `auto` / `read_only` / `full`）。
    #: ★ 默认值是**最紧的那一档之一**：脱离 agent 循环单测工具时，
    #: 这里必须是「照常审批」，不能是一个悄悄放宽的 `auto`。
    #: 由 `context_from_state` 从图状态带进来，工具与沙箱层读它。
    permission_mode: str = "manual"
    #: 这一次调用是否已经由「能挂起等审批」的驱动层判过（规则 / 模式 / 钩子 / 人工批准）。
    #: ★ 只有 `context_from_state` 会把它置真 —— 那两个调用点（主对话 respond、
    #: 子图 tool_executor）是唯一在 execute **之前**跑过 `decide_tool_call` 的地方。
    #: 仅表示判定来自驱动层；它不是用户批准的证明。
    #: 需要确认的调用必须带 call_id，由注册中心核对对应审批记录的工具、参数与请求者。
    #: 默认 False（= 按没人问过处理），因为漏接一条路径的后果应该是「不做」，
    #: 而不是「做了没人知道」。
    policy_handled: bool = False
    #: 尚未收敛为固定字段的东西（前端传来的本轮选项、审批预授权 id 等）
    extras: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    @property
    def session_key(self) -> str:
        """
        会话级工具状态（shell 的 cwd 与后台任务、「这个文件读过没有」）的隔离键。

        ★ 为什么不直接用 `thread_id`：同一个会话里可以并发跑好几个后台子任务，
        它们共用一个 thread_id。按 thread_id 建会话的话，甲 `cd build-a`、乙紧接着
        `cd build-b`，甲的下一条命令就跑进了乙的目录 —— 界面上一切正常，只有结果是错的。
        所以有 `task_id` 就必须带上它。

        空串 = 没有会话身份（脚本直调注册中心、健康检查），调用方按「无从判断」处理。
        """
        if not self.thread_id:
            return ""
        return f"{self.thread_id}#{self.task_id}" if self.task_id else self.thread_id

    def emit(self, event: dict[str, Any]) -> bool:
        """
        把事件推给驱动层的实时通道（由调用方经 `extras["emit"]` 传进来）。

        返回是否真的推了出去。**不要把返回值当「用户看到了」**：主对话那条 SSE
        只在用户正在发消息时活着，页面关掉就只剩进度通道（`task_bus`）了。
        """
        sink = self.extras.get("emit")
        if not callable(sink):
            return False
        sink(event)
        return True


_EMPTY_CONTEXT = ToolContext()
_CONTEXT_VAR: ContextVar[ToolContext] = ContextVar("tool_context", default=_EMPTY_CONTEXT)


def get_tool_context() -> ToolContext:
    """取当前工具上下文；未绑定时返回空上下文而不是抛错（工具应能独立被脚本调用）"""
    return _CONTEXT_VAR.get()


def workspace_root() -> Path:
    """
    本次调用的文件根目录。

    上下文里带了就用它（会话级工作区、数字员工独立目录都从这里进来），
    否则退回全局沙箱配置 —— 保证脱离 agent 循环单测工具时行为仍然确定。
    """
    ctx = _CONTEXT_VAR.get()
    return (ctx.workspace_root or settings.sandbox_path).resolve()


@contextmanager
def bind_tool_context(ctx: ToolContext) -> Iterator[ToolContext]:
    """在工具执行期间绑定上下文；异常路径也必须 reset，否则会串到下一个调用"""
    token = _CONTEXT_VAR.set(ctx)
    try:
        yield ctx
    finally:
        _CONTEXT_VAR.reset(token)


def context_from_state(
    state: dict[str, Any] | None,
    *,
    agent_name: str = "",
    emit: Callable[[dict], None] | None = None,
) -> ToolContext:
    """
    从图状态构造 `ToolContext` —— 两条 agent 循环（主对话 respond、子图 tool_executor）共用。

    ★ 为什么两处共用一个工厂而不是各写一遍
    `extras["emit"]` 是「工具能不能往实时通道推事件」的唯一约定（派发子任务、
    以后的 skill 加载提示都靠它）。各写一遍就会只有一处写对，而写错的那处**不报错**：
    工具照常执行、任务照常跑完，只有界面上收不到事件 —— 这类「静默半失效」是最难查的。

    `thread_id` 一律取**状态里的**（用户的会话）。
    ⚠️ 不要用 `config["configurable"]["thread_id"]`：子图里那个是 checkpointer 的
    命名空间 key（`subtask_<task_id>`），它长得像会话 id 但不是，拿去做会话隔离
    会得到「每个子任务各一套、每次重建」的错结果，而没有任何一处会报错。
    """
    s = state or {}
    # 本轮工作区（用户在输入框下面选的那个目录）。空 = 没选 = 交给沙箱默认根。
    # ⚠️ 这里**不校验路径是否存在**：入口 `resolve_thread_root()` 已经确认过它是
    # 一个真实目录，中途被删的话由工具自己报「目录不存在」——那才是用户看得懂的一句话。
    root = str(s.get("workdir_root") or "").strip()
    from app.permissions.policy import effective_mode
    ctx = ToolContext(
        agent_name=agent_name,
        thread_id=str(s.get("thread_id") or ""),
        run_id=str(s.get("run_id") or ""),
        task_id=str(s.get("task_id") or ""),
        employee_id=str(s.get("employee_id") or ""),
        workspace_root=Path(root) if root else None,
        # 缺少本轮覆盖时继承全局默认；已有覆盖只能相对当前全局模式收紧。
        permission_mode=effective_mode(str(s.get("permission_mode") or "")),
        # 表示驱动层已判定权限；真正批准仍由当前 call_id 对应的审批凭据证明。
        policy_handled=True,
    )
    if emit is not None:
        ctx.extras["emit"] = emit
    # ★ 本轮选项按「状态里有没有」原样带过去，不做默认值填充。
    # 主对话的投影带这三个字段，子图 tool_executor 的投影不带 ——
    # 于是 extras 里也就没有，工具据此知道「这次没有用户开关可继承」，
    # 而不是被这里替它编一个「用户开着联网」的假设。
    for key in ("web_enabled", "graph_enabled", "model"):
        if key in s:
            ctx.extras[key] = s[key]
    return ctx


__all__ = [
    "ToolContext",
    "bind_tool_context",
    "context_from_state",
    "get_tool_context",
    "workspace_root",
]
