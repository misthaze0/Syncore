"""
MCP 工具适配器 —— 把外部 MCP Server 接进工具治理中心

★ 架构位置

    ┌─────────────────────────────┐
    │ 工具治理中心 tool_registry   │  ← 子 Agent 唯一可见的工具边界
    └──────────────▲──────────────┘
                   │ register(source="mcp")
    ┌──────────────┴──────────────┐
    │  mcp_adapter（本模块）       │  ← 协议翻译 + 连接生命周期
    └──────────────▲──────────────┘
                   │ stdio / streamable_http / sse
    ┌──────────────┴──────────────┐
    │ 外部 MCP Server              │  ← 任意语言实现，本机子进程或远端 HTTP
    └─────────────────────────────┘

配置从哪来是另一个模块的事（`app/mcp_store.py`，界面上写的那份文件）。
本模块只管三件事：连上、把 `tools/list` 变成注册中心里的工具、断了怎么再连。

★ 关键设计：MCP 工具在注册中心里「与内置工具完全同权」

适配器不引入任何特殊路径 —— 远端工具走的就是 `tool_registry.register()`，
同样声明 `danger_level` 与 `allowed_agents`。因此：

- 危险等级调整、权限收敛，仍然集中在工具治理中心这一层；
- 一个 MCP 工具如果声明为 `danger`，它的调用会**自动**落入既有的 HITL 审批闭环，
  不需要改一行 Graph 代码；
- 子 Agent 完全感知不到工具来自本地还是远端（也不该感知）。

★ 生命周期：谁都能被随时拔掉

三种传输都不是一次性连接：stdio 是常驻子进程，HTTP 是长连接 + SSE 流。
`AsyncExitStack` 常驻在连接对象里，应用退出时统一 `close_all()`。
和上一版最大的不同是**现在可以从运行中的进程里摘掉一个 server**：
删除/停用/改配置都会关掉它的会话并 `unregister` 掉它贡献的工具 ——
否则界面上删掉了、模型还在调，那才是真的失控。

★ 失败之后的重试是"有节制的"，不是"没有重试"

连不上的 server 记下 `retry_after`（`MCP_RETRY_SECONDS`）。在窗口内：
- 调用工具 → 直接返回一条说清"上次为什么没连上、什么时候会再试"的错误，不再打网络；
- 界面上点「连接测试」→ 无视窗口，立刻真试一次（用户要的就是这个）。

没做后台定时器：远端服务重启几分钟是常态，让一个 3 秒一圈的循环把日志刷满、
把 CPU 空转掉，换来的只是"早几分钟连上"，而这个产品不需要。

★ 明确没做的

- **OAuth 2.1 授权流**（动态注册 / 令牌存储与刷新）。现在支持的是 `headers` 里带
  Bearer token —— 桌面自用场景绝大多数是这种。授权流要落的是"点开浏览器授权、
  回调、存 token、过期再刷"这一整套，不是加个字段，所以宁可写清楚没做，
  也不在状态里显示一个"已授权"的假象。
- **resources / prompts 两种 MCP 原语**：只接 `tools`。接进来需要新的工具面
  （列资源、读资源），而这一期的目标是"外部工具能用、能在界面上管"。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.agents.tools.registry import tool_registry
from app.config import settings
from app.schemas.tools import ToolResult
from app.mcp_store import MAX_TOOLS_PER_SERVER, MCPServerConfig, get_mcp_store

logger = logging.getLogger(__name__)

#: MCP server 单次返回内容的**安全上限**（防止一个失控的 server 吐回内存级文本）。
#: ⚠️ 它不是常规截断线：正常的超长输出交给 `tools/base.py::finalize_text`
#: 完整落盘 + 头尾预览，模型可以按路径续读。在这里先切 8000 会把中间内容永久丢掉。
MAX_MCP_OUTPUT_CHARS = 2_000_000

#: 注册进治理中心时给 MCP 工具打的分组（工具治理界面按分类聚合）
MCP_CATEGORY = "连接器"


# ---------- stdio 参数解析 ----------
def _resolve_command(command: str) -> str:
    """
    配置里写 `python` / `python3` 时替换为**当前解释器**。

    原因：应用跑在虚拟环境里，而子进程若按 PATH 找 `python` 可能落到系统解释器上 ——
    那个环境里没有装 `mcp`，Server 会在启动瞬间因 ImportError 退出，
    表现为「连接成功但一个工具都没有」，非常难排查。
    """
    if command.strip().lower() in {"python", "python3", "py"}:
        return sys.executable
    return command


def _resolve_cwd(config: MCPServerConfig) -> Optional[str]:
    """相对 cwd 一律相对后端项目根解析，这样从任意目录启动都能找到 mcp_servers/ 下的脚本"""
    if not config.is_http and not config.command:
        return None
    if config.cwd:
        p = Path(config.cwd)
        return str(p if p.is_absolute() else (settings.backend_root / p).resolve())
    return str(settings.backend_root)


def _resolve_args(args: List[str]) -> List[str]:
    """把「看起来像仓库内相对路径的」参数转成绝对路径（跨目录启动时才不会找不到脚本）"""
    resolved: List[str] = []
    for arg in args:
        if arg.startswith("-") or not arg:
            resolved.append(arg)
            continue
        candidate = settings.backend_root / arg
        resolved.append(str(candidate) if candidate.exists() else arg)
    return resolved


# ---------- 单个 Server 连接 ----------
class MCPServerConnection:
    """
    一个 MCP Server 的会话 + 它贡献给注册中心的工具名。

    ★ 会话由一个**常驻任务**持有，不是由"建立它的那个请求"持有
    `stdio_client` / `streamable_http_client` 内部都是 anyio TaskGroup：进入上下文的那个任务
    必须一直活着，它负责读对端的流。如果连接是在 HTTP 请求任务里建立的，
    请求一返回，这个任务作用域就没了 —— 表现是"保存成功、工具也列出来了，
    但下一次调用永远卡住"（没人再 pump stdout）。
    所以上下文进在一个只干这件事的任务里，`call()` 从任何任务调用都安全。
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.session: Any = None
        self.tools: List[Dict[str, Any]] = []
        #: 远端真实工具数（可能多于 `tools`：超过上限会被截断，界面要说清）
        self.tools_total: int = 0
        self.error: Optional[str] = None
        self.connected_at: float = 0.0
        self.registered_names: List[str] = []
        self.server_info: Dict[str, str] = {}
        self._retry_after: float = 0.0
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._ready: Optional[asyncio.Event] = None
        self._start_error: Optional[BaseException] = None
        #: 重连成功后由管理器挂上：重注册工具名（前缀/危险等级可能已经改过）。
        #: 连接自己不知道注册中心的存在，所以用回调而不是让它直接去写。
        self._on_reconnected: Optional[Callable[[MCPServerConnection], None]] = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def connected(self) -> bool:
        return self.session is not None

    @property
    def retry_in(self) -> float:
        """还要等多久才允许再次自动重连（秒，<=0 = 现在就可以试）"""
        return max(0.0, self._retry_after - time.monotonic())

    # ---------- 建流 ----------

    async def _enter_transport(self, stack: AsyncExitStack) -> Tuple[Any, Any]:
        """
        按配置打开对应传输，返回 `(read_stream, write_stream)`。

        ★ 三种传输的差异只在"流从哪来"
        握手、`list_tools`、`call_tool` 全是同一个 `ClientSession`，
        所以这一层是唯一需要分支的地方 —— 新增传输不该再往下游扩散。
        """
        from mcp import ClientSession  # noqa: F401  (类型对齐用；实际 ClientSession 在 connect 里)

        transport = self.config.transport
        if transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            # 继承当前环境再叠加自定义变量；PYTHONIOENCODING / PYTHONUNBUFFERED 是
            # Windows 中文输出与实时性的关键（默认 GBK 管道会让子进程中文乱码甚至报错）。
            env = dict(os.environ)
            env.update(self.config.env)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUNBUFFERED", "1")

            params = StdioServerParameters(
                command=_resolve_command(self.config.command),
                args=_resolve_args(self.config.args),
                env=env,
                cwd=_resolve_cwd(self.config),
                encoding="utf-8",
                encoding_error_handler="replace",
            )
            return await stack.enter_async_context(stdio_client(params))

        if transport == "streamable_http":
            from mcp.client.streamable_http import streamable_http_client

            http_client = None
            if self.config.headers:
                # headers（Bearer token）只能通过自建 httpx 客户端传进去；
                # 用 SDK 的工厂函数建，超时与重定向策略保持它推荐的那套。
                from mcp.shared._httpx_utils import create_mcp_http_client

                http_client = await stack.enter_async_context(
                    create_mcp_http_client(headers=dict(self.config.headers))
                )
            streams = await stack.enter_async_context(
                streamable_http_client(self.config.url, http_client=http_client)
            )
            # 2.x 返回 TransportStreams（.read_stream/.write_stream），1.x 返回二元组
            return _pair(streams)

        if transport == "sse":
            from mcp.client.sse import sse_client

            return await stack.enter_async_context(
                sse_client(
                    self.config.url,
                    headers=dict(self.config.headers) or None,
                    timeout=min(30.0, settings.MCP_TIMEOUT_SECONDS),
                    sse_read_timeout=max(60.0, settings.MCP_TIMEOUT_SECONDS * 10),
                )
            )

        raise ValueError(f"不支持的 transport：{transport}")

    def _record_failure(self, message: str) -> None:
        """记一次失败：原因 + 下一次允许自动重试的时间（界面与工具调用都读这两个字段）"""
        self.error = message[:300]
        self._retry_after = time.monotonic() + max(0.0, settings.MCP_RETRY_SECONDS)

    def _upgrade_failure(self, message: str) -> None:
        """
        用更具体的原因替换占位原因（只升级一次，不重开重试窗口）。

        判据说清楚：只有手上那句是"连接被取消"这种没信息量的话时，才接受新的那句。
        """
        if not message or not self.error:
            return
        if "连接被取消" in self.error and "连接被取消" not in message:
            self.error = message[:300]

    async def connect(self, *, force: bool = False) -> None:
        """
        建流并完成 MCP 握手（交给常驻任务），失败抛异常。

        `force=True` 无视重试窗口（界面上的「连接测试」就是这个语义）。
        """
        if self.connected:
            return
        if not force and self.retry_in > 0:
            raise ConnectionError(
                f"上次连接失败，{int(self.retry_in)} 秒后才会自动重试"
                f"（原因：{self.error or '未知'}）"
            )

        if self._owner is not None and not self._owner.done():
            # 已经有一个常驻任务在建这条连接（并发保存 / 调用时自动重连撞上同一件事）
            await self._ready.wait()  # type: ignore[union-attr]
            if self._start_error is not None:
                raise ConnectionError(self.error or "连接失败") from self._start_error
            return

        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._start_error = None
        self._owner = asyncio.create_task(self._owner_loop(), name=f"mcp:{self.name}")
        await self._ready.wait()
        if self._start_error is not None:
            raise ConnectionError(self.error or "连接失败") from self._start_error

    async def _owner_loop(self) -> None:
        """
        常驻任务：持有传输上下文，直到有人 `close()`。

        ★ 建流、握手、收尾都发生在这一个任务里
        anyio 的 cancel scope 不允许"在 A 任务进入、在 B 任务退出"；
        而建连接的请求早就返回了。把整个生命周期收进一个任务，
        既避免跨任务退出报错，也让"对端没人回话"时流仍有人读。
        """
        from mcp import ClientSession

        stack = AsyncExitStack()
        failure: Optional[BaseException] = None
        try:
            read_stream, write_stream = await self._enter_transport(stack)
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            init_result = await session.initialize()
            # 字段名在两代 SDK 之间不一致：线上协议是 camelCase（serverInfo），
            # Python 模型是 snake_case（server_info）。两个都试，取不到才显示 "?" ——
            # 只认一个的症候是"连上了但界面上对方叫什么永远是问号"。
            info = (getattr(init_result, "server_info", None)
                    or getattr(init_result, "serverInfo", None))
            self.server_info = {
                "name": str(getattr(info, "name", "?") if info else "?"),
                "version": str(getattr(info, "version", "") if info else ""),
                "protocol": str(
                    getattr(init_result, "protocol_version", None)
                    or getattr(init_result, "protocolVersion", "")
                    or ""
                ),
            }
            self.session = session
            self.error = None
            self._retry_after = 0.0
            self.connected_at = time.time()
            logger.info(
                "✓ MCP Server '%s' 已连接（%s）: %s %s",
                self.name, self.config.transport,
                self.server_info["name"], self.server_info["version"],
            )
            # 握手成功即可交付使用：本任务此后只干一件事 —— 活着，并负责读流与收尾
            self._ready.set()  # type: ignore[union-attr]
            await self._stop.wait()  # type: ignore[union-attr]
        except BaseException as e:
            # 先记下原因再收尾：清理路径自己也可能抛（anyio TaskGroup 拆解时二次抛错），
            # 顺序反了就变成"连接失败但状态里 error=null"，界面只剩一个转圈的保存按钮。
            failure = e
            self._record_failure(_describe(e))
        finally:
            try:
                await stack.aclose()
            except BaseException as close_error:  # noqa: BLE001
                # ★ 真正的原因常常在这一步才浮出来
                # anyio 先取消本任务（上面只拿到"被取消"），连接被拒 / DNS 失败的
                # 原始异常要等 TaskGroup 收尾时才抛出来。不接过来，用户就只看到"被取消"。
                self._upgrade_failure(_describe(close_error))
                logger.debug("MCP '%s' 收尾时二次抛错（原因已合并）: %s", self.name, close_error)
            self.session = None
            self._start_error = failure
            if not self._ready.is_set():  # type: ignore[union-attr]
                self._ready.set()  # type: ignore[union-attr]

    async def close(self) -> None:
        """停掉常驻任务并等它退出（退出必须在它自己的任务里，所以只能等，不能代劳）"""
        owner, stop = self._owner, self._stop
        self._owner = None
        if stop is not None:
            stop.set()
        if owner is not None and not owner.done():
            try:
                # `shield`：本任务被取消时不要让常驻任务跟着一起死 —— 它得走完收尾
                await asyncio.wait_for(asyncio.shield(owner), timeout=10.0)
            except TimeoutError:
                logger.warning("MCP '%s' 收尾超过 10 秒，强制取消常驻任务", self.name)
                owner.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("关闭 MCP Server '%s' 连接时出错: %s", self.name, e)
        self.session = None
        self.tools = []
        self.tools_total = 0

    async def bootstrap(self, *, timeout: float) -> List[Dict[str, Any]]:
        """
        连接 + 拉工具清单，整体限时。

        ★ 为什么要有一个总时限，而不是"迟早会返回"
        配置现在能从界面写，写动作就会走到这里。一个卡在启动阶段的 server
        （`npx` 首次下载、子进程读不到 stdin）会让保存请求**一直转圈**，
        而 stdio 那条路上子进程其实已经起来了 —— 超时不关它就是没人认领的孤儿进程。
        """
        deadline = time.monotonic() + timeout
        try:
            async with asyncio.timeout(timeout):
                await self.connect()
                return await self.discover_tools()
        except TimeoutError as e:
            await self.close()
            self._record_failure(f"TimeoutError: {int(timeout)} 秒内没连上")
            raise ConnectionError(self.error) from e
        except BaseException as e:
            # 客户端断开导致的取消也要收尾：`close()` 幂等，connect() 里清过就空转
            await self.close()
            # 到点之后内部抛上来的常常是"被取消"而不是 TimeoutError（anyio 的取消语义）。
            # 那句 "Cancelled by cancel scope 0x…" 对用户没有任何价值，
            # 而此刻确实已经超时 —— 按超时来记录，别把内部机制当失败原因写出去。
            if time.monotonic() >= deadline - 0.05:
                self._record_failure(f"TimeoutError: {int(timeout)} 秒内没连上")
                raise ConnectionError(self.error) from e
            raise

    # ---------- 工具发现与调用 ----------

    async def discover_tools(self) -> List[Dict[str, Any]]:
        """拉取 `tools/list`（超过上限的部分**明说**被截断，而不是悄悄丢）"""
        if not self.session:
            return []
        result = await self.session.list_tools()
        raw = getattr(result, "tools", None) or []
        self.tools_total = len(raw)
        tools: List[Dict[str, Any]] = []
        for tool in raw[:MAX_TOOLS_PER_SERVER]:
            # mcp 2.x 的 Python 模型字段是 snake_case（input_schema），
            # 线上协议仍是 camelCase（inputSchema），这里只关心 Python 侧
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
            tools.append({
                "name": str(getattr(tool, "name", "") or ""),
                # MCP 2025-06 起工具可带 title；有就用，没有则退回本地名
                "title": str(getattr(tool, "title", "") or ""),
                "description": str(getattr(tool, "description", "") or ""),
                "input_schema": schema or {"type": "object", "properties": {}},
            })
        self.tools = tools
        return tools

    async def call(self, tool_name: str, args: Dict[str, Any]) -> str | ToolResult:
        """
        调用远端工具并抽取文本结果。

        ★ 会话断了怎么办：先就地重连一次，再把失败抛给治理中心
        子进程被人杀掉 / 远端重启，症状会是"所有 MCP 工具都报错"，
        而如果只能靠重启进程恢复，用户就会以为这个功能坏了。
        重连只试一次（`force=True` 绕开重试窗口），失败原因原样带出去。
        """
        async with self._lock:
            if not self.connected:
                await self.connect()
                await self._restore_after_reconnect()
            try:
                result = await self._call_once(tool_name, args)
            except Exception as e:  # noqa: BLE001
                from app.computer_control import COMPUTER_CONTROL_SERVER

                if self.name == COMPUTER_CONTROL_SERVER:
                    # 超时/断流不代表桌面动作没执行。绝不能自动重放点击、输入或拖动。
                    raise RuntimeError(
                        "桌面调用结果未知，已停止自动重试。请重新截图确认界面后再决定下一步。"
                    ) from e
                # 会话已经废了（流关闭 / 子进程退出）→ 重建一次再调用。
                # 判据是"session 不再可用"而不是异常类型：不同传输抛的异常五花八门，
                # 认类型会漏；直接重连一次则最坏也只是把原始异常继续往外抛。
                logger.debug("MCP '%s' 调用失败，尝试重连一次: %s", self.name, e)
                await self.close()
                await self.connect(force=True)
                await self._restore_after_reconnect()
                result = await self._call_once(tool_name, args)

        text = _extract_text(result)
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            raise RuntimeError(text or "远端工具返回错误")
        if len(text) > MAX_MCP_OUTPUT_CHARS:
            text = (
                text[:MAX_MCP_OUTPUT_CHARS]
                + f"\n...[输出超过安全上限 {MAX_MCP_OUTPUT_CHARS} 字符，共 {len(text)} 字符；"
                "请让该工具缩小返回范围后重试]"
            )
        images = _extract_images(result)
        if images:
            from app.agents.tools.base import finalize_text

            text, truncated, path = finalize_text(text, tool_name)
            return ToolResult(success=True, result=text, images=images, duration_ms=0,
                              truncated=truncated, output_path=path)
        return text

    async def _restore_after_reconnect(self) -> None:
        """
        重连之后补回工具清单，并让管理器按最新配置重注册。

        ★ 为什么这一步不能省
        `close()` 会清空 `tools`。不补回来的症状很隐蔽：工具看着还在（模型仍能调到，
        因为注册名指向的是这条会话），界面却显示"0 个工具"，
        而此后任何一次重注册都会注册出个空集 —— 等于工具真的没了。
        """
        await self.discover_tools()
        if self._on_reconnected is not None:
            self._on_reconnected(self)

    async def _call_once(self, tool_name: str, args: Dict[str, Any]) -> Any:
        return await self.session.call_tool(
            tool_name,
            arguments=args or {},
            read_timeout_seconds=settings.MCP_TIMEOUT_SECONDS,
        )

    def status_dict(self) -> Dict[str, Any]:
        return {
            **self.config.to_public(),
            "connected": self.connected,
            "server_info": dict(self.server_info),
            "tools": [t["name"] for t in self.tools],
            "tools_total": self.tools_total,
            "truncated": self.tools_total > len(self.tools),
            "registered": list(self.registered_names),
            "error": self.error,
            "retry_in": round(self.retry_in, 1),
            "connected_at": self.connected_at or None,
        }


#: 这些异常类型的消息对用户没有信息量（anyio 的取消是内部机制，不是失败原因）
_NOISE_TYPES = {"CancelledError"}


def _describe(error: BaseException) -> str:
    """
    把异常翻译成给人看的一句话。

    ★ 为什么不能直接 `str(e)`
    MCP 客户端跑在 anyio TaskGroup 上，连接被拒时抛的是 `ExceptionGroup`，
    它的 `str()` 只有「unhandled errors in a TaskGroup (1 sub-exception)」；
    而在超时/取消的路径上叶子节点又是 `CancelledError("Cancelled by cancel scope 0x…")`。
    两句用户都看不懂，真实原因（ConnectError: All connection attempts failed）
    埋在子异常或 `__cause__` 链上，必须展开找。
    """
    leaves: List[BaseException] = []
    seen: set[int] = set()

    def walk(node: BaseException, depth: int = 0) -> None:
        if node is None or depth > 6 or id(node) in seen:
            return
        seen.add(id(node))
        children = getattr(node, "exceptions", None)
        if children:
            for sub in children:
                walk(sub, depth + 1)
            return
        leaves.append(node)
        walk(node.__cause__, depth + 1)
        if not node.__suppress_context__:
            walk(node.__context__, depth + 1)

    walk(error)
    # 先要"有意义的叶子"，噪声类型（取消）排在后面兜底
    ordered = [n for n in leaves if type(n).__name__ not in _NOISE_TYPES] + [
        n for n in leaves if type(n).__name__ in _NOISE_TYPES
    ]
    for node in ordered:
        text = str(node).strip().replace("\n", " ")
        if not text:
            # 空消息的节点（比如 `WouldBlock`）只是链路的一环，跳过它继续找下一个，
            # 不能让它把搜索结束掉 —— 否则又会退回那句没人看得懂的原始文本
            continue
        if type(node).__name__ in _NOISE_TYPES:
            # 取消本身不是原因，直说"这一侧放弃了"比留一个 scope 地址有用
            return "连接被取消（超时或对方没有响应）"
        # 有些异常已经把类名写进消息里了（`ConnectError: All connection attempts failed`），
        # 再拼一次前缀就成了 `ConnectionError: ConnectError: ...` —— 界面上像出了两个 bug
        if text.split(":", 1)[0].strip().endswith(("Error", "Exception")):
            return text[:200]
        return f"{type(node).__name__}: {text[:200]}"
    return f"{type(error).__name__}: {str(error).strip()[:200] or '未知错误'}"


def _pair(streams: Any) -> Tuple[Any, Any]:
    """把 2.x 的 TransportStreams 与 1.x 的二元组都收敛成 (read, write)"""
    if isinstance(streams, tuple) and len(streams) == 2:
        return streams
    read = getattr(streams, "read_stream", None)
    write = getattr(streams, "write_stream", None)
    if read is None or write is None:
        raise RuntimeError(f"无法从 {type(streams).__name__} 里取出读写流")
    return read, write


def _extract_text(result: Any) -> str:
    """从 CallToolResult 中抽取可读文本（文本块拼接，其它类型降级为 JSON）"""
    parts: List[str] = []
    for block in getattr(result, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(str(getattr(block, "text", "") or ""))
        elif block_type == "image":
            parts.append("[已附图像；请根据实际画面判断]")
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            parts.append(str(getattr(resource, "text", "") or ""))
        else:
            parts.append(str(block))

    text = "\n".join(p for p in parts if p).strip()
    if not text:
        # 有些 server 只回 structuredContent（结构化结果），这里兜底序列化
        structured = getattr(result, "structured_content", None) or getattr(
            result, "structuredContent", None
        )
        if structured:
            text = json.dumps(structured, ensure_ascii=False)
    return text


def _extract_images(result: Any) -> List[dict]:
    """保留 MCP ImageContent，限制体积，禁止将 base64 误当文本处理。"""
    images: List[dict] = []
    size = 0
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) != "image":
            continue
        mime = getattr(block, "mimeType", "") or getattr(block, "mime_type", "")
        data = getattr(block, "data", "")
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"} or not isinstance(data, str):
            raise ValueError("连接器返回了不支持的图像格式")
        size += len(data)
        if size > 24_000_000 or len(images) >= 4:
            raise ValueError("连接器图像超过上限，请缩小截图范围")
        try:
            base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError("连接器返回了无效图像数据") from e
        images.append({"mime_type": mime, "data": data})
    return images


# ---------- 管理器 ----------
class MCPManager:
    """MCP 连接池 + 注册编排（单例）"""

    def __init__(self) -> None:
        self._connections: Dict[str, MCPServerConnection] = {}

    # ---------- 生命周期 ----------

    async def apply(self) -> Dict[str, Any]:
        """
        让运行时状态追上配置文件：新启用的连上、被删/被停用的断开并摘掉工具。

        ★ 为什么是"对账"而不是"逐个注册"
        界面能增删改配置，而注册中心是进程内的。任何一次写配置之后都需要一个
        幂等动作把两边对齐；做成"新建就注册、删除就注销"的两条代码路径，
        早晚会出现一边有工具一边没连接（或反之）的状态。
        """
        desired = {c.name: c for c in get_mcp_store().enabled_servers()} if settings.MCP_ENABLED else {}

        # 1) 摘掉不该在的（含配置删除、界面停用、MCP_ENABLED 关闭）
        for name in [n for n in self._connections if n not in desired]:
            await self.disconnect(name)

        # 2) 连上新增的、或配置变了的（transport/url/command 变了必须先断开再连）
        results: Dict[str, str] = {}
        for name, config in desired.items():
            existing = self._connections.get(name)
            if existing is not None and not _same_endpoint(existing.config, config):
                await self.disconnect(name)
                existing = None
            if existing is not None and existing.connected:
                # 只改了治理字段（危险等级 / 可见范围 / 前缀）：会话不用拔，
                # 但**必须重注册** —— 否则界面上把工具改成 danger，
                # 注册中心里它还是 safe，审批闭环压根不会触发（改了等于没改）。
                if not _same_governance(existing.config, config):
                    existing.config = config
                    self._re_register(existing)
                results[name] = "connected"
                continue
            if existing is not None:
                # 之前失败过：还在重试窗口里就不重复打网络，直接把状态报出去
                if existing.retry_in > 0:
                    results[name] = f"deferred:{existing.error}"
                    continue
                await self.disconnect(name)
            results[name] = await self._connect_and_register(config)

        return {
            "servers": results,
            "tools": sum(len(c.registered_names) for c in self._connections.values()),
            "connected": [n for n, s in results.items() if s == "connected"],
            "failed": {n: s for n, s in results.items() if s != "connected"},
        }

    async def _connect_and_register(self, config: MCPServerConfig) -> str:
        conn = MCPServerConnection(config)
        try:
            await conn.bootstrap(timeout=settings.MCP_CONNECT_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001
            self._connections[config.name] = conn  # 留住错误与重试窗口，界面才看得见
            logger.warning("MCP Server '%s' 接入失败（其工具不可用）: %s", config.name, e)
            return f"failed:{e}"
        self._connections[config.name] = conn
        names = self._register_tools(conn)
        # 首注册完成之后才挂回调：自动重连时按最新配置重注册（清单可能已经变了）
        conn._on_reconnected = lambda c: self._re_register(c)
        logger.info("MCP Server '%s' 注册了 %s 个工具: %s", config.name, len(names), names)
        return "connected"

    async def disconnect(self, name: str) -> None:
        """关掉会话并从注册中心摘掉它贡献的全部工具"""
        conn = self._connections.pop(name, None)
        if conn is None:
            return
        for tool_name in conn.registered_names:
            tool_registry.unregister(tool_name)
        dropped = len(conn.registered_names)
        conn.registered_names = []
        await conn.close()
        # 用摘掉的**注册名**计数：`close()` 会清空 tools，这时候再数就是 0
        logger.info("MCP Server '%s' 已断开（摘掉 %s 个工具）", name, dropped)

    async def reconnect(self, name: str, *, force: bool = True) -> Dict[str, Any]:
        """重连一个 server（断开→重连→重新发现工具）。界面上的「重连」按钮。"""
        config = get_mcp_store().get(name)
        if config is None:
            raise KeyError(f"MCP Server 不存在: {name}")
        await self.disconnect(name)
        status = await self._connect_and_register(config)
        conn = self._connections.get(name)
        return {"server": name, "status": status, "connection": conn.status_dict() if conn else None}

    async def refresh_tools(self, name: str) -> Dict[str, Any]:
        """
        只重新拉 `tools/list` 并重注册（不断会话）。

        给"server 那边加了个工具，但我不想断开重连"用：
        对 stdio 来说断开重连等于重启子进程，有些 server 冷启动很慢。
        """
        conn = self._connections.get(name)
        if conn is None or not conn.connected:
            raise ConnectionError(f"'{name}' 当前没连上，请先重连")
        for tool_name in conn.registered_names:
            tool_registry.unregister(tool_name)
        conn.registered_names = []
        await conn.discover_tools()
        names = self._register_tools(conn)
        # 撞名被跳过的工具要单独报出来：否则"远端有 8 个、这边只显示 6 个"
        # 只能靠用户自己对数（`register()` 不返回结果，只能按名字反推）
        attempted = [
            f"{conn.config.effective_prefix}{t['name']}"
            for t in conn.tools if t.get("name")
        ]
        conflicted = [
            n for n in attempted
            if n not in names and tool_registry.get(n) is not None
        ]
        return {
            "server": name,
            "tools": names,
            "total": conn.tools_total,
            "truncated": conn.tools_total > len(conn.tools),
            "conflicted": conflicted,
        }

    async def probe(self, config: MCPServerConfig) -> Dict[str, Any]:
        """
        临时连接一个尚未保存（或已保存）的配置，看能不能连、有哪些工具。

        ★ 为什么要"不落盘地试一次"
        新建表单里的地址打错了，如果只能"保存 → 看到失败 → 删除"，
        用户是在拿配置文件当草稿纸。试完立刻关闭：不留会话、不注册工具。
        """
        conn = MCPServerConnection(config)
        try:
            await conn.bootstrap(timeout=settings.MCP_CONNECT_TIMEOUT_SECONDS)
            return {"ok": True, **conn.status_dict()}
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "server": config.name,
                "transport": config.transport,
                "error": _describe(e),
            }
        finally:
            await conn.close()

    # ---------- 注册 ----------

    def _register_tools(self, conn: MCPServerConnection) -> List[str]:
        """把远端工具逐个注册进工具治理中心，返回注册成功的本地工具名"""
        config = conn.config
        names: List[str] = []
        for tool in conn.tools:
            raw_name = tool["name"]
            if not raw_name:
                continue
            name = f"{config.effective_prefix}{raw_name}"
            if tool_registry.get(name) is not None:
                # 重名不静默覆盖内置工具（前缀冲突时在状态里看得见）
                logger.warning(
                    "MCP 工具 '%s'（来自 %s）与已注册工具重名，已跳过；"
                    "如确需接入请改这个 server 的 tool_prefix",
                    name, config.name,
                )
                continue
            tool_registry.register(
                name=name,
                display_name=_tool_label(config, tool),
                handler=_make_handler(conn, raw_name, name),
                description=tool.get("description") or f"[MCP:{config.name}] {raw_name}",
                danger_level=config.danger_level,  # type: ignore[arg-type]
                allowed_agents=config.allowed_agents or None,
                version="mcp",
                args_schema=tool.get("input_schema"),
                health_handler=_make_health(conn),
                overwrite=True,
                category=MCP_CATEGORY,
                source="mcp",
                server=config.name,
            )
            names.append(name)
        conn.registered_names = names
        return names

    def _re_register(self, conn: MCPServerConnection) -> List[str]:
        """按当前配置重注册工具（不断会话）：先摘旧名，再按新前缀/新等级注册"""
        for tool_name in conn.registered_names:
            tool_registry.unregister(tool_name)
        conn.registered_names = []
        return self._register_tools(conn)

    # ---------- 读 ----------

    def get_connection(self, server_name: str) -> Optional[MCPServerConnection]:
        return self._connections.get(server_name)

    def status(self) -> Dict[str, Any]:
        """MCP 接入状态摘要（`/api/mcp` 与 `/api/health` 共用）"""
        servers: List[Dict[str, Any]] = []
        for config in get_mcp_store().load():
            conn = self._connections.get(config.name)
            if conn is not None:
                servers.append(conn.status_dict())
            else:
                servers.append({
                    **config.to_public(),
                    "connected": False,
                    "server_info": {},
                    "tools": [],
                    "tools_total": 0,
                    "truncated": False,
                    "registered": [],
                    "error": None if config.enabled else "已停用（不会连接）",
                    "retry_in": 0.0,
                    "connected_at": None,
                })
        return {
            "enabled": bool(settings.MCP_ENABLED),
            "store": str(settings.mcp_store_path),
            "timeout_seconds": settings.MCP_TIMEOUT_SECONDS,
            "retry_seconds": settings.MCP_RETRY_SECONDS,
            "max_tools_per_server": MAX_TOOLS_PER_SERVER,
            "oauth": "不支持授权流；需要在 headers 里自带 Bearer token",
            "servers": servers,
            "tools": [
                {"name": n, "server": s.name}
                for s in self._connections.values()
                for n in s.registered_names
            ],
            "connected": sum(1 for s in self._connections.values() if s.connected),
            "failed": [
                {"server": n, "error": c.error}
                for n, c in self._connections.items()
                if not c.connected and c.error
            ],
        }

    async def close_all(self) -> None:
        """关闭全部 MCP 会话并摘掉工具（应用退出时调用）"""
        for name in list(self._connections.keys()):
            try:
                await self.disconnect(name)
            except Exception:  # noqa: BLE001
                pass
        self._connections.clear()


def _same_endpoint(old: MCPServerConfig, new: MCPServerConfig) -> bool:
    """
    连接端点是否没变（变了就必须断开重连）。

    只比**影响会话建立**的字段：治理字段（danger_level / allowed_agents / tool_prefix）
    改了就重注册工具，不必把长连接拔掉 —— 拔一次 stdio 子进程可能得重新冷启动几秒。
    """
    return (
        old.transport == new.transport
        and old.command == new.command
        and old.args == new.args
        and old.env == new.env
        and old.cwd == new.cwd
        and old.url == new.url
        and old.headers == new.headers
    )


def _same_governance(old: MCPServerConfig, new: MCPServerConfig) -> bool:
    """
    治理身份是否没变（变了只需重注册，不需要拔连接）。

    与 `_same_endpoint` 是一对：那一边管"会话要不要重建"，这一边管
    "注册中心里的 danger_level / allowed_agents / 工具名 / 显示名要不要跟着配置走"。
    少了这一半，界面上改危险等级就是个假开关；改显示名而工具标题不变，同理。
    """
    return (
        old.danger_level == new.danger_level
        and old.allowed_agents == new.allowed_agents
        and old.effective_prefix == new.effective_prefix
        and old.display_name == new.display_name
    )


def _tool_label(config: MCPServerConfig, tool: Dict[str, Any]) -> str:
    """
    MCP 工具在界面上的中文名。

    MCP 协议里的工具名由远端定义、通常无法翻译，所以显示名至少要说清
    「来自哪个连接器」；远端给了 `title` 就优先用远端标题。
    """
    server_label = config.display_name or config.name
    title = str(tool.get("title") or "").strip()
    raw_name = str(tool.get("name") or "").strip()
    return f"{server_label} · {title or raw_name}"


def _make_handler(conn: MCPServerConnection, remote_name: str, local_name: str):
    """
    生成注册中心使用的 handler。

    注册中心以 `handler(**args)` 方式调用，而 MCP 需要 `arguments` 字典，
    因此这里做一次形参收集。
    """

    async def _handler(**kwargs: Any) -> str | ToolResult:
        from app.computer_control import COMPUTER_CONTROL_SERVER, capture_desktop, computer_control_store

        desktop = conn.name == COMPUTER_CONTROL_SERVER
        logger.debug("调用 MCP 工具 %s -> %s(%s)", local_name, remote_name, "桌面参数已省略" if desktop else kwargs)
        if desktop and remote_name.casefold() == "snapshot":
            kwargs["use_vision"] = True
        result = await conn.call(remote_name, kwargs)
        if desktop and remote_name.casefold() in {"snapshot", "screenshot"}:
            if not isinstance(result, ToolResult) or not result.images:
                raise RuntimeError("未取得有效屏幕图像，请重新连接桌面连接器后再观察，暂勿操作键鼠")
        if (desktop and computer_control_store.load().get("capture_after_action", True)
                and remote_name.casefold() in {"click", "type", "shortcut", "scroll", "move", "drag", "app", "multi", "multiselect", "multiedit", "wait", "waitfor"}):
            try:
                observation = await capture_desktop(conn=conn, respect_policy=True)
                if not isinstance(result, ToolResult):
                    result = ToolResult(success=True, result=result, duration_ms=0)
                result.images = observation.images
                result.result += "\n\n【操作后画面】\n" + observation.result
            except Exception as e:
                # 动作已完成，截图失败不能把动作标为可重试的失败。
                if not isinstance(result, ToolResult):
                    result = ToolResult(success=True, result=result, duration_ms=0)
                result.result += f"\n操作后的截图未能取得（{type(e).__name__}）。请先重新截图，勿重复上一步动作。"
        return result

    return _handler


def _make_health(conn: MCPServerConnection):
    """健康检查：会话还在即视为健康（流断开时 session 会被置空）"""

    def _health() -> bool:
        return conn.connected

    return _health


# 全局单例
mcp_manager = MCPManager()


__all__ = [
    "MAX_MCP_OUTPUT_CHARS",
    "MCP_CATEGORY",
    "MCPServerConnection",
    "MCPManager",
    "mcp_manager",
]
