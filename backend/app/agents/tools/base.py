"""
工具基类 / 协议定义

设计说明:
- `ToolInfo` / `ToolResult` 的数据模型统一定义在 `app/schemas/tools.py`，
  本模块只负责「工具运行时」需要的协议与包装，避免两处重复定义导致漂移。
- 任何实现了 `Tool` 协议的对象都可被注册中心接受；实际使用中最常见的是
  `RegisteredTool`（handler 函数 + 元信息），由 `@agent_tool` 自动构造。
"""

from __future__ import annotations

import inspect
import json
import time
import typing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol, runtime_checkable

from app.agents.tools.context import ToolContext, bind_tool_context
from app.agents.tools.schema import is_tool_context_annotation
from app.config import settings
from app.schemas.tools import ToolInfo, ToolResult

# 危险等级：danger 级工具执行前必须经过 HITL 人工审批
DangerLevel = Literal["safe", "moderate", "danger"]

# 工具处理函数：既支持同步也支持异步，参数通过关键字传入
ToolHandler = Callable[..., Any]
HealthHandler = Callable[[], bool | Awaitable[bool]]

# 全部内置工具统一返回 str（便于直接喂给 LLM）
MAX_TOOL_OUTPUT_CHARS = 8000
#: 超长输出落盘后回给模型的预览：头部留足结构，尾部留足结论
_SPILL_HEAD_CHARS = 4000
_SPILL_TAIL_CHARS = 1200
#: 落盘目录（在沙箱内，所以 read 工具天然能续读）
_OUTPUT_SUBDIR = ".agent_outputs"
#: 落盘保留条数上限，避免长时间运行后目录里堆垃圾
_OUTPUT_KEEP = 200


@runtime_checkable
class Tool(Protocol):
    """工具协议"""

    info: ToolInfo

    async def invoke(self, args: dict) -> ToolResult:
        """执行工具并返回结构化结果"""
        ...


def find_context_param(handler: Any) -> str | None:
    """
    返回 handler 中需要注入运行时上下文的形参名（无则 None）。

    与 schema 推导共用同一个类型判定，否则会出现「schema 里把 ctx 剔掉了、
    执行时却没注入」这种只在特定写法下才暴露的错配。
    """
    try:
        hints = typing.get_type_hints(handler, include_extras=True)
    except Exception:  # noqa: BLE001
        hints = dict(getattr(handler, "__annotations__", {}) or {})
    for name, param in inspect.signature(handler).parameters.items():
        if is_tool_context_annotation(hints.get(name, param.annotation)):
            return name
    return None


def _output_dir() -> Path:
    root = Path(settings.sandbox_path).resolve() / _OUTPUT_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prune_outputs(root: Path) -> None:
    """只保留最近 N 个落盘文件；删不动就放弃（这不是关键路径）"""
    try:
        files = sorted(root.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[_OUTPUT_KEEP:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def spill_output(text: str, tool_name: str) -> str:
    """把完整输出落盘，返回相对沙箱根的路径"""
    root = _output_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tool_name)[:40]
    target = root / f"{stamp}-{safe}.txt"
    target.write_text(text, encoding="utf-8")
    _prune_outputs(root)
    return f"{_OUTPUT_SUBDIR}/{target.name}"


def finalize_text(result: Any, tool_name: str) -> tuple[str, bool, str]:
    """
    把 handler 返回值规整成模型可读文本。

    超过上限时不再像原先那样「砍头留尾丢内容」—— 完整内容落盘，
    预览给出头尾与续读路径。模型因此**始终**有一个获取完整结果的途径，
    而不是只能基于被截断的片段继续推理。
    """
    text = "" if result is None else (result if isinstance(result, str) else str(result))
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text, False, ""

    path = spill_output(text, tool_name)
    preview = (
        f"{text[:_SPILL_HEAD_CHARS]}\n\n"
        f"…[输出共 {len(text)} 字符，已完整保存到 `{path}`。"
        f"上面是前 {_SPILL_HEAD_CHARS} 字符，末尾如下，中间部分需要时用 `read` 按行号取 `{path}`]\n\n"
        f"{text[-_SPILL_TAIL_CHARS:]}"
    )
    return preview, True, path


@dataclass
class RegisteredTool:
    """注册中心内部持有的工具实体"""

    info: ToolInfo
    handler: ToolHandler
    health_handler: HealthHandler | None = None
    #: 需要注入 ToolContext 的形参名；None = 该工具不需要运行时上下文
    context_param: str | None = None

    # ---------- 属性快捷方式 ----------
    @property
    def name(self) -> str:
        return self.info.name

    @property
    def danger_level(self) -> DangerLevel:
        return self.info.danger_level

    def allowed_for(self, agent_name: str) -> bool:
        """权限判定：allowed_agents 为空表示对所有 Agent 开放"""
        return not self.info.allowed_agents or agent_name in self.info.allowed_agents

    # ---------- 执行 ----------
    async def invoke(self, args: dict | None = None, ctx: ToolContext | None = None) -> ToolResult:
        """
        执行工具。

        统一捕获异常并统计耗时，保证「工具报错」不会打断整个 Graph 流程。
        参数校验由注册中心在调用前完成，这里只负责执行与结果规整。
        """
        args = dict(args or {})
        context = ctx or ToolContext()
        if self.context_param:
            args[self.context_param] = context

        start = time.perf_counter()
        try:
            with bind_tool_context(context):
                result = self.handler(**args)
                if inspect.isawaitable(result):
                    result = await result
            duration_ms = int((time.perf_counter() - start) * 1000)

            # 工具直接返回 ToolResult 时（需要声明产出文件/失败语义）原样透传，只补耗时
            if isinstance(result, ToolResult):
                result.duration_ms = duration_ms
                return result
            if isinstance(result, str) and result.lstrip().startswith(("[错误]", "[失败]", "[联网不可用]", "[联网未生效]", "[联网额度不足]")):
                return ToolResult(success=False, result="", duration_ms=duration_ms, error=result)

            # dict/list 结果序列化成 JSON —— 结构化工具不必自己想着 json.dumps
            if isinstance(result, (dict, list)):
                result = json.dumps(result, ensure_ascii=False, default=str)

            text, truncated, output_path = finalize_text(result, self.name)
            return ToolResult(
                success=True,
                result=text,
                duration_ms=duration_ms,
                truncated=truncated,
                output_path=output_path,
            )
        except PermissionError as e:
            duration_ms = int((time.perf_counter() - start) * 1000)
            return ToolResult(
                success=False,
                result="",
                duration_ms=duration_ms,
                error=f"越权拒绝: {e}",
            )
        except Exception as e:  # noqa: BLE001
            duration_ms = int((time.perf_counter() - start) * 1000)
            return ToolResult(
                success=False,
                result="",
                duration_ms=duration_ms,
                error=f"{type(e).__name__}: {e}",
            )

    # ---------- 健康检查 ----------
    async def check_health(self) -> bool:
        """执行健康检查；未提供 health_handler 时视为健康"""
        if self.health_handler is None:
            return True
        try:
            result = self.health_handler()
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:  # noqa: BLE001
            return False


__all__ = [
    "DangerLevel",
    "MAX_TOOL_OUTPUT_CHARS",
    "RegisteredTool",
    "Tool",
    "ToolHandler",
    "finalize_text",
    "find_context_param",
    "spill_output",
]
