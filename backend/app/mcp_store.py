"""
MCP Server 配置存储 —— 设置页「MCP」写的那份

★ 为什么不再是 `.env` 里的一段 JSON

原先 `MCP_SERVERS` 是一段手写的 JSON 数组：改一个地址要编辑 `.env` 再重启进程，
连不上时界面只会显示「0 个工具」。这段配置的性质其实是**运行时状态**
（用户在本机接的外部服务，含 token），和 `data/llm_profiles.json` 同一类，
所以搬到文件里，并把「改完立刻看到工具」做成界面能驱动的动作。

`.env` 的 `MCP_SERVERS` 退化为**首次种子**：文件不存在时读一次并落盘，
之后不再回读 —— 两边都读就会产生「改了 .env 为什么没生效」这种两头都对不上的问题，
必须有一个唯一事实来源（见 `load()` 里的迁移日志）。

★ 凭据是只写的

`env` 与 `headers` 的值可能是 token。列表/详情出参一律打码（`MASK`），
只有**逐字回传掩码**才表示"保持原值不变" —— 用户想改就填新值，
不想改就别动。反过来的坑是把真值回给前端：任何一份截图、
任何一次界面数据被抓走，token 就没了。

★ `transport` 三种

- `stdio`：本机起一个子进程（`command` + `args`）。
- `streamable_http`：远端/本机 HTTP 端点（`url`），MCP 现行推荐。
- `sse`：老的 SSE 端点（`url`），不少现存 server 只支持这一种。

后两种不需要 `command`，也就没有"Windows 上子进程找不到 python"那一类问题。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

#: 出参里的凭据占位符。前端原样回传 = 不改动原值（见模块注释）
MASK = "********"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TRANSPORTS = ("stdio", "streamable_http", "sse")
DANGER_LEVELS = ("safe", "moderate", "danger")

#: 一个 server 最多注册多少个工具（接一个"什么都暴露"的 server 时，
#: 工具清单会直接挤爆 prompt；宁可截断并说明，也不要悄悄全量塞进去）
MAX_TOOLS_PER_SERVER = 40

_HTTP_TRANSPORTS = ("streamable_http", "sse")


@dataclass
class MCPServerConfig:
    """一个 MCP Server 的完整声明（配置 + 治理身份）"""

    name: str
    #: 界面显示名（中文优先）。`name` 是工具前缀与索引键，不直接当标题用。
    display_name: str = ""
    transport: str = "stdio"
    enabled: bool = True

    # ---------- stdio ----------
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""

    # ---------- streamable_http / sse ----------
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    # ---------- 治理参数（决定这些工具在注册中心里的身份） ----------
    #: 工具名前缀。留空 = 自动用 `<name>_`（见 `_default_prefix`）
    tool_prefix: str = ""
    danger_level: str = "safe"
    allowed_agents: List[str] = field(default_factory=list)
    note: str = ""

    @property
    def is_http(self) -> bool:
        return self.transport in _HTTP_TRANSPORTS

    @property
    def effective_prefix(self) -> str:
        """
        实际使用的工具名前缀。

        ★ 为什么默认带前缀而不是留空
        空前缀意味着远端工具叫 `read` 就直接和内置 `read` 撞名，
        而撞名是被**跳过**的（不覆盖内置工具）——
        症状是"接上了但少几个工具"。默认前缀把这件事变成命名空间问题而不是丢失问题。
        """
        return self.tool_prefix if self.tool_prefix else f"{self.name}_"

    def to_public(self) -> Dict[str, Any]:
        """出参（凭据打码，只留键名）"""
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "transport": self.transport,
            "enabled": self.enabled,
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "env_keys": sorted(self.env.keys()),
            "url": self.url,
            "header_keys": sorted(self.headers.keys()),
            "tool_prefix": self.tool_prefix,
            "effective_prefix": self.effective_prefix,
            "danger_level": self.danger_level,
            "allowed_agents": list(self.allowed_agents),
            "note": self.note,
        }

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "transport": self.transport,
            "enabled": self.enabled,
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "cwd": self.cwd,
            "url": self.url,
            "headers": dict(self.headers),
            "tool_prefix": self.tool_prefix,
            "danger_level": self.danger_level,
            "allowed_agents": list(self.allowed_agents),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MCPServerConfig":
        raw = dict(data or {})
        transport = str(raw.get("transport") or "stdio").strip().lower()
        # 早期写法 `http` / `streamable-http` 都指同一件事；不认识的值按 stdio 处理
        # 并在 `validate()` 里报错，而不是悄悄接一个连不上的传输。
        transport = {"http": "streamable_http", "streamable-http": "streamable_http"}.get(
            transport, transport
        )
        return cls(
            name=str(raw.get("name") or "").strip(),
            display_name=str(raw.get("display_name") or "").strip(),
            transport=transport,
            enabled=_as_bool(raw.get("enabled"), True),
            command=str(raw.get("command") or "").strip(),
            args=[str(a) for a in (raw.get("args") or [])],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            cwd=str(raw.get("cwd") or "").strip(),
            url=str(raw.get("url") or "").strip(),
            headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
            tool_prefix=str(raw.get("tool_prefix") or "").strip(),
            danger_level=str(raw.get("danger_level") or "safe").strip().lower(),
            allowed_agents=[str(a) for a in (raw.get("allowed_agents") or [])],
            note=str(raw.get("note") or "").strip(),
        )

    def validate(self) -> None:
        """配置层能当场判定的错误（连不上是运行期的事，这里不猜）"""
        if not NAME_RE.match(self.name):
            raise ValueError(
                "name 只能用小写字母、数字、下划线或连字符，且以字母/数字开头（1–32 位）"
            )
        if self.transport not in TRANSPORTS:
            raise ValueError(
                f"transport「{self.transport}」不支持；可选：{', '.join(TRANSPORTS)}"
            )
        if self.is_http:
            if not self.url:
                raise ValueError(f"{self.transport} 需要 url")
            if not self.url.lower().startswith(("http://", "https://")):
                raise ValueError("url 必须以 http:// 或 https:// 开头")
        elif not self.command:
            raise ValueError("stdio 传输需要 command（例如 python / npx / uvx）")
        if self.danger_level not in DANGER_LEVELS:
            raise ValueError(f"danger_level 只能是 {', '.join(DANGER_LEVELS)}")


def _as_bool(value: Any, default: bool = True) -> bool:
    """
    宽松布尔解析。

    `.env` / 表单里的 "false" 用 `bool()` 转是 **True**，
    会把"关掉"变成"打开" —— 这类开关不能靠转型。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"", "0", "false", "no", "off", "disabled"}:
            return False
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
    return default


def _merge_secret(new: Dict[str, str], old: Dict[str, str]) -> Dict[str, str]:
    """
    把"逐字回传掩码"的项换回原值。

    只认**整个值就是掩码**这一种情况：半真半码（`sk-****1234`）说明用户是在编辑它，
    那种值本来就不是凭据了，照抄存回去比猜用户想存什么更诚实。
    """
    merged: Dict[str, str] = {}
    for key, value in new.items():
        if value == MASK:
            merged[key] = old.get(key, value)
        else:
            merged[key] = value
    return merged


class MCPServerStore:
    """`data/mcp/servers.json` 的读写（进程内缓存 + 首次从 .env 迁移）"""

    def __init__(self) -> None:
        # ★ 必须是**可重入**锁：写方法（save/update/delete/set_enabled）都是
        # "拿锁 → 调 load() 拿当前清单 → 改 → 落盘"，而 `load()` 自己也要拿锁。
        # 用普通 Lock 的话，第一次写就会**自己等自己**，请求永远挂住。
        self._lock = threading.RLock()
        self._servers: Optional[List[MCPServerConfig]] = None

    # ---------- 读写 ----------

    def load(self, *, force: bool = False) -> List[MCPServerConfig]:
        with self._lock:
            if self._servers is not None and not force:
                return self._servers

            path = settings.mcp_store_path
            servers: List[MCPServerConfig] = []
            migrated = False
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    for item in data.get("servers") or []:
                        if isinstance(item, dict) and item.get("name"):
                            servers.append(MCPServerConfig.from_dict(item))
                except (OSError, json.JSONDecodeError) as e:
                    # 配置文件坏掉时**不能**当作"没有 server"继续跑：
                    # 那样用户看到的是列表空了，而真相是文件读不出来。
                    # 保不住新副本就退回上一次读到的内存副本（不改写、不迁移），
                    # 并把错误留在日志里 —— 界面上会显示"当前 0 个"还是旧清单，
                    # 排查从日志开始，而不是从猜开始。
                    logger.error("MCP 配置文件读取失败（沿用上次载入的清单，不做迁移）: %s", e)
                    return self._servers or []
            else:
                servers = self._seed_from_env()
                migrated = bool(servers)

            self._servers = servers
            if not path.is_file() or migrated:
                self._save()
            return self._servers

    def _seed_from_env(self) -> List[MCPServerConfig]:
        """首次使用：把 `.env` 里手写的 MCP_SERVERS 搬进文件（一次性迁移）"""
        raw = settings.mcp_servers_list
        if not raw:
            return []
        servers = [MCPServerConfig.from_dict(item) for item in raw]
        logger.info(
            "MCP 配置已从 .env 迁移到 %s（%s 个 server，此后以文件为准）",
            settings.mcp_store_path, len(servers),
        )
        return [s for s in servers if s.name]

    def _save(self) -> None:
        assert self._servers is not None
        payload = {"servers": [s.to_json() for s in self._servers]}
        settings.mcp_store_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 查询 ----------

    def get(self, name: str) -> Optional[MCPServerConfig]:
        key = (name or "").strip()
        for s in self.load():
            if s.name == key:
                return s
        return None

    def enabled_servers(self) -> List[MCPServerConfig]:
        return [s for s in self.load() if s.enabled]

    # ---------- 写入 ----------

    def save(self, payload: Dict[str, Any]) -> MCPServerConfig:
        """新建一个 server（同名拒绝 —— 改名走 update，别靠覆盖）"""
        config = MCPServerConfig.from_dict(payload)
        config.validate()
        with self._lock:
            servers = self.load()
            if any(s.name == config.name for s in servers):
                raise ValueError(f"已存在同名 MCP Server：{config.name}")
            servers.append(config)
            self._save()
        return config

    def update(self, name: str, changes: Dict[str, Any]) -> MCPServerConfig:
        """
        改一个 server 的配置。

        `env` / `headers` 里回传掩码 = 保持原值（前端拿不到真值，见模块注释）。
        """
        with self._lock:
            servers = self.load()
            idx = next((i for i, s in enumerate(servers) if s.name == name), -1)
            if idx < 0:
                raise KeyError(f"MCP Server 不存在: {name}")
            old = servers[idx]
            merged = dict(old.to_json())
            for key, value in changes.items():
                if key == "env" and isinstance(value, dict):
                    merged["env"] = _merge_secret(
                        {str(k): str(v) for k, v in value.items()}, old.env
                    )
                elif key == "headers" and isinstance(value, dict):
                    merged["headers"] = _merge_secret(
                        {str(k): str(v) for k, v in value.items()}, old.headers
                    )
                elif key in {"name", "display_name", "command", "cwd", "url", "tool_prefix", "note",
                             "transport", "danger_level"}:
                    merged[key] = str(value or "").strip()
                elif key in {"args", "allowed_agents"}:
                    merged[key] = [str(a) for a in (value or [])]
                elif key == "enabled":
                    merged[key] = _as_bool(value, old.enabled)
            config = MCPServerConfig.from_dict(merged)
            config.validate()
            if config.name != name and any(s.name == config.name for s in servers if s is not old):
                # 改名撞车会在文件里留下两个同名条目，而连接池与注册中心都按名字索引 ——
                # 症状是"删掉一个，另一个的工具也没了"。改名允许，撞名不允许。
                raise ValueError(f"已存在同名 MCP Server：{config.name}")
            servers[idx] = config
            self._save()
        return config

    def delete(self, name: str) -> None:
        with self._lock:
            servers = self.load()
            rest = [s for s in servers if s.name != name]
            if len(rest) == len(servers):
                raise KeyError(f"MCP Server 不存在: {name}")
            self._servers = rest
            self._save()

    def set_enabled(self, name: str, enabled: bool) -> MCPServerConfig:
        with self._lock:
            servers = self.load()
            idx = next((i for i, s in enumerate(servers) if s.name == name), -1)
            if idx < 0:
                raise KeyError(f"MCP Server 不存在: {name}")
            target = servers[idx]
            target.enabled = enabled
            self._save()
            return target


_store: Optional[MCPServerStore] = None
_store_lock = threading.Lock()


def get_mcp_store() -> MCPServerStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = MCPServerStore()
        return _store


__all__ = [
    "DANGER_LEVELS",
    "MASK",
    "MAX_TOOLS_PER_SERVER",
    "MCPServerConfig",
    "MCPServerStore",
    "TRANSPORTS",
    "get_mcp_store",
]
