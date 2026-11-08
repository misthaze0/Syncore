"""
MCP 控制面接口 —— 设置页「MCP」的后端

一个 server 的完整生命周期都能在这里走完：
列清单（含连接状态）、新增、改、开关、删、连接测试、重连、重拉工具。

★ 为什么写操作之后要跟着一次 `apply()`

配置写在文件里，工具活在**进程内**（注册中心 + 常驻会话）。
如果"保存"只写文件，界面显示的是新配置、模型用的是旧工具，
两边对不上的时候没人能解释。所以每个写动作都以一次对账收尾：
新增的连上并注册，删掉/停用的断开并摘掉工具，改了端点的拔掉重连。
`apply()` 是幂等的 —— 已经连上且端点没变的 server 不会被动。

★ 凭据只进不出

出参（`to_public()`）只有 `env_keys` / `header_keys` 和掩码，
**任何接口都不会回显 token 真值**。改配置时把某个键的值原样填成 `********`
就表示"这个键别动"（合并逻辑见 `app/mcp_store._merge_secret`）。

★ 「连接测试」和「保存」是两件事

`POST /mcp/test` 拿的是**表单当前内容**，可以连一个还没保存的 server，
试完立刻关掉：不落盘、不注册工具、不留会话。
否则用户只能"保存 → 看到红的 → 再删掉"，把配置文件当草稿纸。

★ 明确没有的接口

- 没有 OAuth 授权流接口（动态注册 / 令牌存储刷新）。要鉴权请在 `headers` 里放 Bearer token。
- 没有 resources / prompts 接口：这一期只接 `tools` 这一种 MCP 原语。
  两件事都在 `/api/mcp` 的返回里写明了，不在界面按钮上装样子。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.agents.tools.mcp_adapter import mcp_manager
from app.config import settings
from app.mcp_store import DANGER_LEVELS, TRANSPORTS, MCPServerConfig, get_mcp_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp", tags=["mcp"])


class MCPServerPayload(BaseModel):
    """
    新建 / 修改的请求体。

    全部字段可选：`PUT` 只改传上来的那些（`exclude_none`），
    未提及的保持原样 —— 前端提交整张表单时不会意外把没填的字段清空，
    而 `extra="forbid"` 保证键名打错是 422，不是"看起来保存了但什么都没变"。
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, description="小写字母/数字/下划线/连字符，1–32 位")
    display_name: Optional[str] = Field(None, description="界面显示名（中文优先）；留空显示 server 标识")
    transport: Optional[str] = Field(None, description=f"{' / '.join(TRANSPORTS)}")
    enabled: Optional[bool] = Field(None, description="停用 = 断开并摘掉它的全部工具")

    command: Optional[str] = Field(None, description="stdio：启动命令（python/npx/uvx…）")
    args: Optional[List[str]] = Field(None, description="stdio：参数；仓库内相对路径会自动转绝对")
    env: Optional[Dict[str, str]] = Field(None, description="stdio：附加环境变量（值只写不读）")
    cwd: Optional[str] = Field(None, description="stdio：工作目录，相对 backend/")

    url: Optional[str] = Field(None, description="streamable_http / sse：端点")
    headers: Optional[Dict[str, str]] = Field(None, description="HTTP 头，鉴权 token 放这里（值只写不读）")

    tool_prefix: Optional[str] = Field(None, description="工具名前缀，留空 = 自动用 `<name>_`")
    danger_level: Optional[str] = Field(None, description=f"{' / '.join(DANGER_LEVELS)}")
    allowed_agents: Optional[List[str]] = Field(None, description="只允许哪些专员调用；空 = 都允许")
    note: Optional[str] = Field(None, description="备注，只给界面看")


class EnabledPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


async def _apply_or_fail() -> Dict[str, Any]:
    """
    写完配置后的对账。

    ★ 对账失败不能把写操作一起回滚掉
    配置已经落盘了，连接失败是运行期的事（服务没起来、地址写错了）。
    这里把异常转成 `failed` 里的一条原因返回给界面，而不是抛 500 ——
    抛错会让用户以为"没保存成功"，进而反复点保存。
    """
    try:
        return await mcp_manager.apply()
    except Exception as e:  # noqa: BLE001
        logger.error("MCP 对账失败: %s", e, exc_info=True)
        return {"servers": {}, "tools": 0, "connected": [], "failed": {"apply": f"{type(e).__name__}: {e}"}}


def _build_probe_config(payload: MCPServerPayload) -> MCPServerConfig:
    raw = payload.model_dump(exclude_none=True)
    if not str(raw.get("name") or "").strip():
        # 连接测试问的是"这个地址/命令能不能用"，不该被"先给它起个名字"挡住。
        # 名字在保存时由 store 的 validate() 把关，这里只是临时对象的前缀占位。
        raw["name"] = "probe"
    config = MCPServerConfig.from_dict(raw)
    try:
        config.validate()
    except ValueError as e:
        raise _bad_request(str(e)) from e
    return config


# ---------- 读 ----------

@router.get("")
async def list_servers() -> Dict[str, Any]:
    """
    全部 server 的「配置 + 运行状态」合并视图。

    配置来自文件（唯一事实来源），状态来自进程内的会话池：
    没连上的 server 也要出现在清单里并带上失败原因与 `retry_in` ——
    "接不上"必须看得见，否则用户只会看到空列表。
    """
    status = mcp_manager.status()
    status["transports"] = list(TRANSPORTS)
    status["danger_levels"] = list(DANGER_LEVELS)
    status["connect_timeout_seconds"] = settings.MCP_CONNECT_TIMEOUT_SECONDS
    return status


# ---------- 写 ----------

@router.post("")
async def create_server(payload: MCPServerPayload) -> Dict[str, Any]:
    """新增一个 MCP Server（写文件 → 立刻对账 → 返回状态）"""
    try:
        config = get_mcp_store().save(payload.model_dump(exclude_none=True))
    except ValueError as e:
        raise _bad_request(str(e)) from e
    applied = await _apply_or_fail()
    logger.info("MCP Server 已新增: %s（%s）", config.name, config.transport)
    return {"ok": True, "server": config.to_public(), "apply": applied}


@router.put("/{name}")
async def update_server(name: str, payload: MCPServerPayload) -> Dict[str, Any]:
    """
    修改一个 server（部分更新）。

    ⚠️ 改了 `url` / `command` / `transport` 会拔掉旧连接重连；
    只改治理字段（`danger_level` / `allowed_agents` / `tool_prefix`）不动会话。
    判据在 `mcp_adapter._same_endpoint`。
    """
    changes = payload.model_dump(exclude_none=True)
    try:
        config = get_mcp_store().update(name, changes)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise _bad_request(str(e)) from e
    applied = await _apply_or_fail()
    logger.info("MCP Server 已更新: %s", config.name)
    return {"ok": True, "server": config.to_public(), "apply": applied}


@router.delete("/{name}")
async def delete_server(name: str) -> Dict[str, Any]:
    """删除一个 server：会话关掉，它注册的工具一起摘掉（界面上删了、模型还能调 = 失控）"""
    try:
        get_mcp_store().delete(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    applied = await _apply_or_fail()
    logger.info("MCP Server 已删除: %s", name)
    return {"ok": True, "apply": applied}


@router.post("/{name}/enabled")
async def set_enabled(name: str, payload: EnabledPayload) -> Dict[str, Any]:
    """启用 / 停用（停用是"断开 + 摘工具"，不是"标记一下下次再说"）"""
    try:
        config = get_mcp_store().set_enabled(name, payload.enabled)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    applied = await _apply_or_fail()
    return {"ok": True, "server": config.to_public(), "apply": applied}


# ---------- 连接动作 ----------

@router.post("/test")
async def test_connection(payload: MCPServerPayload) -> Dict[str, Any]:
    """
    连接测试：拿表单当前内容真连一次，返回它有多少个工具、叫什么。

    不落盘、不注册工具、试完立刻关（见 `mcp_manager.probe`）。
    全局 `MCP_ENABLED=false` 时也允许测 —— 用户要判断的是"这个地址对不对"，
    而不是"这台机器现在接不接"。
    """
    config = _build_probe_config(payload)
    result = await mcp_manager.probe(config)
    return {"name": config.name, **result}


@router.post("/{name}/reconnect")
async def reconnect(name: str) -> Dict[str, Any]:
    """断开重连（无视重试窗口）—— 远端服务刚重启时点这个"""
    try:
        return {"ok": True, **await mcp_manager.reconnect(name)}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{name}/refresh")
async def refresh_tools(name: str) -> Dict[str, Any]:
    """只重拉 tools/list 并重注册（server 加了工具，不想重启子进程）"""
    try:
        return {"ok": True, **await mcp_manager.refresh_tools(name)}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ConnectionError as e:
        # 409：请求本身没错，是当前状态（没连上）不允许做这件事
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/apply")
async def apply() -> Dict[str, Any]:
    """手动对账一次（改了文件、或想让重试窗口到点的 server 立刻重连）"""
    return {"ok": True, "apply": await _apply_or_fail()}
