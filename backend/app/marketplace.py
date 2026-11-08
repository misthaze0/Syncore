"""
插件市场 —— 目录（发现）、安装、卸载、启停

★ 三个面共用一份条目模型

界面上「发现 / 已安装 / 连接器」三个面读的是同一个 `MarketEntry`，
只是过滤条件不同：发现 = 目录源里的条目（未安装的在前），
已安装 = 本机真实存在的（注册表 + 活扫描），连接器 = kind=mcp 的那批。
不做三套模型，是因为"装了没 / 开着没 / 要不要配 / 能不能升"这四个状态
必须和目录条目对得上 —— 两套模型必然漂移成"界面说装了、她说没有"。

★ 目录源有两个，失败不静默

- 内置：`backend/market/index.json` + `packs/`（随仓库走，离线可装）。
- 远程：`MARKET_INDEX_URL`（可选）。拉失败只影响远程那一源，
  内置清单照常返回，失败原因进 `sources[].error` 给界面显示红条 ——
  静默降级的症状是"市场今天怎么是空的"，没人能解释。

★ 安装 = 落盘 + 注册表，能力本身仍走原有加载器

技能装完就是 `data/skills/<name>/` 里的一个文件夹，由 `SkillStore` 的
目录指纹自己认；MCP 装完就是 `servers.json` 里的一条，由 `MCPManager.apply()`
对账。市场不发明第二条加载链路 —— 两条链路的症状是"市场说装了、治理页没有"。

★ 安全边界

- zip 只许 http(s)、有大小上限、解压前逐条校验路径（zip-slip）。
- 安装目标只允许 `data/skills/` 下的新目录；**内置技能目录永远不写不删**
  （bundled 技能在已安装视图里 `uninstallable=false`，只能停用）。
- 组合包成员不单独卸：要卸先卸包，否则成员和包的账对不上。
- `install_ref` 一次性、十分钟过期：安装必须跟在"看过这条目录"后面，
  也避免一个被记住的 URL 参数变成无限安装入口。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from app.builtin_plugins import get_builtin, market_entries

from app.config import settings

logger = logging.getLogger(__name__)

KINDS = ("plugin", "skill", "mcp")
INSTALL_KINDS = ("pack-dir", "zip-url", "mcp-server")

#: install_ref 的寿命。够用户读完详情点一下安装，不够留给隔夜的重放。
REF_TTL_SECONDS = 600

#: 一个 zip 里最多允许多少个文件（zip 炸弹的第二道防线）
MAX_ZIP_ENTRIES = 2000

#: 上传插件包里的清单文件名（决定这个 zip 是插件包还是技能包）
PLUGIN_MANIFEST = "plugin.json"

_DIR_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff.-]+")

#: 目录条目 canonical 的形状，和 MCP 的 name 规则一致（落盘就是目录名）
_CANONICAL_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")

#: 粘贴进来的 MCP 配置里，这些键各家写法不同但意思一样
_TRANSPORT_ALIASES = {
    "stdio": "stdio",
    "streamable_http": "streamable_http",
    "streamable-http": "streamable_http",
    "streamablehttp": "streamable_http",
    "http": "streamable_http",
    "sse": "sse",
}

#: 本机 MCP 配置认识的键。粘贴进来的配置里其余的会被丢掉，并在响应里点名
_MCP_SERVER_KEYS = {
    "name",
    "display_name",
    "transport",
    "enabled",
    "command",
    "args",
    "env",
    "cwd",
    "url",
    "headers",
    "tool_prefix",
    "danger_level",
    "allowed_agents",
    "note",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_dir_name(name: str, fallback: str) -> str:
    """技能目录名：去掉路径分隔符等危险字符，空结果回退到 canonical"""
    cleaned = _DIR_NAME_RE.sub("-", (name or "").strip()).strip("-.")
    return cleaned or fallback


class MarketError(ValueError):
    """市场层的可预期错误（界面直接显示这句话）"""


@dataclass
class MarketInstallSpec:
    """一条目录条目"怎么装"的声明"""

    kind: str = ""  # pack-dir | zip-url | mcp-server
    path: str = ""  # pack-dir：相对 market/packs/ 的目录名
    url: str = ""  # zip-url
    server: Dict[str, Any] = field(default_factory=dict)  # mcp-server：MCPServerPayload 形状

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MarketInstallSpec":
        raw = raw or {}
        return cls(
            kind=str(raw.get("kind") or "").strip(),
            path=str(raw.get("path") or "").strip(),
            url=str(raw.get("url") or "").strip(),
            server=dict(raw.get("server") or {}),
        )


@dataclass
class MarketEntry:
    kind: str
    canonical: str
    name: str
    description: str = ""
    source: str = "official"
    publisher: str = ""
    version: str = "0.0.0"
    category: str = ""
    tags: List[str] = field(default_factory=list)
    components: List[Dict[str, Any]] = field(default_factory=list)
    install: MarketInstallSpec = field(default_factory=MarketInstallSpec)
    danger_level: str = "safe"
    required_env: List[str] = field(default_factory=list)
    popularity: int = 0
    updated_at: str = ""
    #: 目录页「精选」筛选用；纯展示位，不参与任何权限判断
    featured: bool = False

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], *, default_source: str) -> "MarketEntry":
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in KINDS:
            raise MarketError(f"目录条目 kind「{kind}」不认识（可选：{' / '.join(KINDS)}）")
        canonical = str(raw.get("canonical") or raw.get("canonical_name") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not canonical or not name:
            raise MarketError("目录条目缺 canonical 或 name")
        return cls(
            kind=kind,
            canonical=canonical,
            name=name,
            description=str(raw.get("description") or "").strip(),
            source=str(raw.get("source") or default_source).strip(),
            publisher=str(raw.get("publisher") or "").strip(),
            version=str(raw.get("version") or "0.0.0").strip(),
            category=str(raw.get("category") or "").strip(),
            tags=[str(t) for t in (raw.get("tags") or [])],
            components=[dict(c) for c in (raw.get("components") or []) if isinstance(c, dict)],
            install=MarketInstallSpec.from_dict(raw.get("install") or {}),
            danger_level=str(raw.get("danger_level") or "safe").strip().lower(),
            required_env=[str(e) for e in (raw.get("required_env") or [])],
            popularity=int(raw.get("popularity") or 0),
            updated_at=str(raw.get("updated_at") or "").strip(),
            featured=bool(raw.get("featured") or False),
        )


def _component_key(kind: str, ref: str) -> str:
    return f"{kind}:{ref}"


class MarketStore:
    """目录 + 注册表 + install_ref 的读写（进程内缓存，锁保护落盘）"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._index: Optional[Dict[str, MarketEntry]] = None
        self._sources: Optional[List[Dict[str, Any]]] = None
        self._installed: Optional[Dict[str, Any]] = None
        self._refs: Dict[str, Tuple[str, float]] = {}

    # ---------- 目录源 ----------

    def load_index(self, *, force: bool = False) -> Tuple[Dict[str, MarketEntry], List[Dict[str, Any]]]:
        with self._lock:
            if self._index is not None and not force:
                return self._index, self._sources or []

            entries: Dict[str, MarketEntry] = {}
            sources: List[Dict[str, Any]] = []

            bundled = self._read_source(
                source_id="bundled",
                kind="bundled",
                location=str(settings.market_index_path),
                payload_getter=lambda: self._read_bundled_index(),
                default_source="official",
            )
            sources.append(bundled["source"])
            for entry in bundled["entries"]:
                entries[entry.canonical] = entry

            if settings.MARKET_INDEX_URL:
                remote = self._read_source(
                    source_id="remote",
                    kind="remote",
                    location=settings.MARKET_INDEX_URL,
                    payload_getter=lambda: self._fetch_remote_index(),
                    default_source="remote",
                )
                sources.append(remote["source"])
                # 远程覆盖内置同名条目：运营侧改描述/升版本不该发版
                for entry in remote["entries"]:
                    entries[entry.canonical] = entry

            self._index = entries
            self._sources = sources
            return entries, sources

    def _read_source(
        self,
        *,
        source_id: str,
        kind: str,
        location: str,
        payload_getter,
        default_source: str,
    ) -> Dict[str, Any]:
        entries: List[MarketEntry] = []
        error: Optional[str] = None
        try:
            raw_entries = payload_getter()
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                try:
                    entries.append(MarketEntry.from_dict(raw, default_source=default_source))
                except MarketError as e:
                    # 一条坏条目不该拖垮整个源：记在源错误里，其它条目照常
                    error = f"{error}；{e}" if error else str(e)
        except Exception as e:  # noqa: BLE001 —— 源读不出来是运行期的事
            error = f"{type(e).__name__}: {e}"
            logger.warning("市场源 %s 读取失败: %s", source_id, error)
        return {
            "entries": entries,
            "source": {
                "id": source_id,
                "kind": kind,
                "url": location,
                "ok": error is None,
                "error": error,
                "count": len(entries),
                "fetched_at": _now(),
            },
        }

    def _read_bundled_index(self) -> List[Dict[str, Any]]:
        path = settings.market_index_path
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("entries") or [])

    def _fetch_remote_index(self) -> List[Dict[str, Any]]:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(settings.MARKET_INDEX_URL)
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, list):
            return data
        return list((data or {}).get("entries") or [])

    def refresh(self) -> List[Dict[str, Any]]:
        _, sources = self.load_index(force=True)
        return sources

    # ---------- 注册表 ----------

    def _load_installed(self) -> Dict[str, Any]:
        with self._lock:
            if self._installed is not None:
                return self._installed
            path = settings.market_installed_path
            data: Dict[str, Any] = {"components": {}, "bundles": {}}
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    data = {
                        "components": dict(raw.get("components") or {}),
                        "bundles": dict(raw.get("bundles") or {}),
                    }
                except (OSError, json.JSONDecodeError) as e:
                    logger.error("市场注册表读取失败（按空注册表继续，不改写文件）: %s", e)
                    return data
            self._installed = data
            return data

    def _save_installed(self) -> None:
        assert self._installed is not None
        settings.market_installed_path.write_text(
            json.dumps(self._installed, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 活状态（她此刻真实有什么） ----------

    def _live_skills(self) -> Dict[str, Dict[str, Any]]:
        from app.skills import get_skill_store

        store = get_skill_store()
        out: Dict[str, Dict[str, Any]] = {}
        for skill in store.scan():
            out[skill.name] = {
                "origin": skill.origin,
                "description": skill.description,
                "enabled": bool(store.is_enabled(skill.name)),
                "usable": bool(skill.usable),
                "shadowed": bool(skill.shadowed),
                "error": str(skill.error or ""),
                "dir": str(skill.dir_path),
            }
        return out

    def _live_mcps(self) -> Dict[str, Dict[str, Any]]:
        from app.mcp_store import get_mcp_store

        out: Dict[str, Dict[str, Any]] = {}
        for cfg in get_mcp_store().load():
            out[cfg.name] = {
                "display_name": cfg.display_name or cfg.name,
                "enabled": bool(cfg.enabled),
                "transport": cfg.transport,
                "env_keys": sorted(cfg.env.keys()),
                "danger_level": cfg.danger_level,
                "note": cfg.note,
            }
        return out

    def _mcp_runtime(self) -> Dict[str, Dict[str, Any]]:
        """连接状态在进程内会话池里；读不到就当"未知"，不猜成"已连接" """
        try:
            from app.agents.tools.mcp_adapter import mcp_manager

            status = mcp_manager.status()
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP 运行状态读不到: %s", e)
            return {}
        # `status()` 的 `servers` 是**逐 server 的字典列表**（每项含 connected/error/tools）；
        # 同名的顶层 `connected` 是**个数**、`failed` 是列表 —— 别把它们当成同一形状用。
        out: Dict[str, Dict[str, Any]] = {}
        for srv in status.get("servers") or []:
            name = srv.get("name")
            if not name:
                continue
            out[name] = {
                "connected": bool(srv.get("connected")),
                "error": srv.get("error") or None,
                "tools": len(srv.get("registered") or srv.get("tools") or []),
            }
        return out

    # ---------- 状态合并 ----------

    def state_for(self, entry: MarketEntry) -> Dict[str, Any]:
        builtin = get_builtin(entry.canonical)
        if builtin:
            return {
                "installed": True, "installed_version": builtin["version"], "enabled": True,
                "update_available": False, "configuration_required": False,
                "status_note": "随应用内置和更新；文件操作遵循当前权限设置",
                "uninstallable": False, "toggleable": False, "builtin": True,
                "via_bundle": None, "attention": False,
            }
        data = self._load_installed()
        components = data.get("components") or {}
        bundles = data.get("bundles") or {}

        installed = False
        installed_version: Optional[str] = None
        enabled = False
        configuration_required = False
        status_note = ""
        uninstallable = False
        via_bundle: Optional[str] = None
        #: 「要用户动手」和「只是说明一下」是两件事：内置技能的那句提示不算需处理
        problem = False

        if entry.kind == "skill":
            live = self._live_skills().get(entry.canonical)
            key = _component_key("skill", entry.canonical)
            record = components.get(key)
            installed = bool(record) or bool(live)
            if record:
                installed_version = str(record.get("version") or "")
                via_bundle = record.get("via")
            if live:
                enabled = bool(live["enabled"]) and bool(live["usable"]) and not live["shadowed"]
                uninstallable = live["origin"] == "user"
                if live["shadowed"]:
                    status_note = "被同名技能遮住，生效的是另一个"
                    problem = True
                elif not live["usable"]:
                    status_note = live["error"] or "SKILL.md 读不出可用指令"
                    problem = True
                elif live["origin"] == "bundled":
                    status_note = "随应用内置，只能停用不能卸载"
            elif record:
                # 注册表说装过、目录里没了：别装作没事
                enabled = False
                status_note = "注册表记录在但目录里没有了"
                problem = True
        elif entry.kind == "mcp":
            live = self._live_mcps().get(entry.canonical)
            record = components.get(_component_key("mcp", entry.canonical))
            installed = bool(live) or bool(record)
            if record:
                installed_version = str(record.get("version") or "")
                via_bundle = record.get("via")
            if live:
                enabled = bool(live["enabled"])
                uninstallable = True
                notes: List[str] = []
                missing = [k for k in entry.required_env if k not in live["env_keys"]]
                if missing:
                    configuration_required = True
                    notes.append(f"缺配置：{', '.join(missing)}")
                runtime = self._mcp_runtime().get(entry.canonical)
                if runtime:
                    if runtime.get("connected"):
                        notes.append(f"{runtime.get('tools', 0)} 个工具")
                    elif live["enabled"] and not configuration_required:
                        # 缺配置时"连不上"是必然结果，不是新信息 —— 只说能修的那件
                        notes.append(runtime.get("error") or "连不上")
                        problem = True
                status_note = "；".join(notes)
            elif record:
                enabled = False
                status_note = "注册表记录在但 MCP 配置里没有了"
                problem = True
        elif entry.kind == "plugin":
            bundle = bundles.get(entry.canonical)
            installed = bool(bundle)
            if bundle:
                installed_version = str(bundle.get("version") or "")
                uninstallable = True
                states = [self.state_for(self._component_entry(c)) for c in entry.components]
                enabled = bool(states) and all(s["enabled"] for s in states)
                broken = [s for s in states if not s["installed"]]
                if broken:
                    status_note = "组合包里有成员缺失"
                # 包里任何一个成员要人管，这个包就要人管
                problem = bool(broken) or any(s["attention"] for s in states)

        if via_bundle:
            # 成员只能跟着包一起卸（`uninstall()` 会直接拒），
            # 所以界面上不能摆一个点了必报错的「卸载」按钮。
            uninstallable = False
            bundle_note = f"组合包「{via_bundle}」的成员，要卸请卸整个包"
            status_note = f"{status_note}；{bundle_note}" if status_note else bundle_note

        update_available = bool(
            installed and installed_version and entry.version and installed_version != entry.version
        )
        return {
            "installed": installed,
            "installed_version": installed_version,
            "update_available": update_available,
            "enabled": enabled,
            "configuration_required": configuration_required,
            "status_note": status_note,
            "uninstallable": uninstallable,
            "via_bundle": via_bundle,
            # 「需处理」= 缺配置 / 可升级 / 坏了。停用是用户自己的选择，不算。
            "attention": bool(configuration_required or update_available or problem),
        }

    def _component_entry(self, spec: Dict[str, Any]) -> MarketEntry:
        """组合包成员的迷你条目（只够算状态用）"""
        kind = str(spec.get("kind") or "skill")
        ref = str(spec.get("ref") or spec.get("name") or "")
        entries, _ = self.load_index()
        base = entries.get(ref)
        if base is not None:
            return base
        return MarketEntry(
            kind=kind if kind in KINDS else "skill",
            canonical=ref,
            name=str(spec.get("name") or ref),
            # Local members have no catalog version to compare against.
            version=str(spec.get("version") or ""),
            install=MarketInstallSpec.from_dict(spec.get("install") or {}),
        )

    # ---------- 查询 ----------

    def _installed_entries(self, entries: Dict[str, MarketEntry]) -> List[MarketEntry]:
        """One inventory for local entries, shared by listing and detail endpoints."""
        live_skill_names = set(self._live_skills())
        live_mcp_names = set(self._live_mcps())
        seen: Dict[str, MarketEntry] = {}
        for name in live_skill_names:
            base = entries.get(name)
            if base is not None and base.kind == "skill":
                seen[name] = base
                continue
            live = self._live_skills()[name]
            seen[name] = MarketEntry(
                kind="skill",
                canonical=name,
                name=name,
                description=live["description"],
                source="official" if live["origin"] == "bundled" else "local",
                version=(self._load_installed().get("components") or {})
                .get(_component_key("skill", name), {})
                .get("version", ""),
            )
        for name in live_mcp_names:
            base = entries.get(name)
            if base is not None and base.kind == "mcp":
                seen[name] = base
                continue
            live_mcp = self._live_mcps()[name]
            seen[name] = MarketEntry(
                kind="mcp",
                canonical=name,
                name=live_mcp.get("display_name") or name,
                description=live_mcp.get("note") or "本机添加的连接器",
                source="custom",
            )
        for bundle_id, bundle in (self._load_installed().get("bundles") or {}).items():
            base = entries.get(bundle_id)
            if base is not None and base.kind == "plugin":
                seen[bundle_id] = base
                continue
            # 目录里已经没有这个包（远程源下架 / 改名 / 上传安装的插件本来就不在目录里）：
            # 照样列出来，成员按注册表记的算 —— 不列 = 界面上看不见、又卸不掉的幽灵。
            seen[bundle_id] = MarketEntry(
                kind="plugin",
                canonical=bundle_id,
                name=str(bundle.get("name") or bundle_id),
                description=str(bundle.get("description") or "本地添加的组合包"),
                source="custom",
                version=str(bundle.get("version") or ""),
                components=[
                    {"kind": key.split(":", 1)[0], "ref": key.split(":", 1)[1]}
                    for key in (bundle.get("components") or [])
                    if ":" in key
                ],
            )
        for entry in market_entries():
            seen[entry.canonical] = entry
        return list(seen.values())

    def search(
        self,
        *,
        view: str = "discover",
        kinds: Optional[List[str]] = None,
        q: str = "",
        category: str = "",
        featured: bool = False,
        sort: str = "popular",
        page: int = 1,
        page_size: int = 24,
        with_refs: bool = True,
    ) -> Dict[str, Any]:
        entries, sources = self.load_index()
        pool: List[MarketEntry] = []
        if view == "discover":
            pool = list({**entries, **{e.canonical: e for e in market_entries()}}.values())
        else:
            pool = self._installed_entries(entries)

        # 先按 kind 收窄：分类轨道必须只数这一 tab 里真有的分类，
        # 否则条目一多就会出现「点了得到一片空」的 chip。
        scoped = [e for e in pool if not kinds or e.kind in kinds]

        needle = q.strip().casefold()
        counts: Dict[str, int] = {}
        featured_total = 0
        filtered: List[MarketEntry] = []
        for entry in scoped:
            if needle:
                haystack = " ".join(
                    [entry.name, entry.description, entry.publisher, entry.canonical, *entry.tags]
                ).casefold()
                if needle not in haystack:
                    continue
            if featured and not entry.featured:
                continue
            if entry.featured:
                featured_total += 1
            if entry.category:
                # 计数不含「当前已选分类」这一层：chip 上的数字回答的是
                # 「点它会看到几条」，把自己减掉就成了每个 chip 都少一条。
                counts[entry.category] = counts.get(entry.category, 0) + 1
            if category and entry.category != category:
                continue
            filtered.append(entry)

        if sort == "newest":
            filtered.sort(key=lambda e: (e.updated_at, e.name), reverse=True)
        else:
            filtered.sort(key=lambda e: (e.popularity, e.name), reverse=True)

        total = len(filtered)
        start = (max(page, 1) - 1) * page_size
        page_items = filtered[start : start + page_size]

        items = [self.public(entry, with_ref=with_refs) for entry in page_items]
        failures = [s for s in sources if s.get("error")]
        return {
            "items": items,
            # 按条数从多到少排（同数按名字）：分类 chip 和页头那句「聚焦」都吃这个顺序，
            # 只按名字排会让「聚焦」前三变成字典序里碰巧排前的三个冷门分类。
            "categories": [
                {"name": k, "count": v}
                for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
            "featured_total": featured_total,
            "sources": sources,
            "failures": failures,
            "total": total,
            "page": max(page, 1),
            "page_size": page_size,
            "has_more": start + page_size < total,
        }

    def public(self, entry: MarketEntry, *, with_ref: bool = True) -> Dict[str, Any]:
        state = self.state_for(entry)
        payload: Dict[str, Any] = {
            "kind": entry.kind,
            "canonical": entry.canonical,
            "name": entry.name,
            "description": entry.description,
            "source": entry.source,
            "publisher": entry.publisher,
            "version": entry.version,
            "category": entry.category,
            "tags": list(entry.tags),
            "components": [
                {
                    "kind": str(c.get("kind") or "skill"),
                    "ref": str(c.get("ref") or c.get("name") or ""),
                    "name": str(c.get("name") or c.get("ref") or ""),
                }
                for c in entry.components
            ],
            "install_kind": entry.install.kind,
            "danger_level": entry.danger_level,
            "required_env": list(entry.required_env),
            "popularity": entry.popularity,
            "updated_at": entry.updated_at,
            "featured": entry.featured,
            "install_ref": None,
            **state,
        }
        builtin = get_builtin(entry.canonical)
        if builtin:
            from app.agents.tools.registry import tool_registry
            payload["provided_tools"] = [tool.model_dump() for tool in tool_registry.list_all()
                                         if tool.name in builtin["tools"]]
        if with_ref and not state["installed"] and (
            entry.kind == "plugin" or entry.install.kind in INSTALL_KINDS
        ):
            payload["install_ref"] = self.issue_ref(entry.canonical)
        return payload

    def get_public(self, canonical: str) -> Optional[Dict[str, Any]]:
        builtin = next((e for e in market_entries() if e.canonical == canonical), None)
        if builtin:
            return self.public(builtin)
        entries, _ = self.load_index()
        entry = entries.get(canonical)
        if entry is None:
            entry = next((item for item in self._installed_entries(entries) if item.canonical == canonical), None)
        return self.public(entry) if entry is not None else None

    # ---------- install_ref ----------

    def issue_ref(self, canonical: str) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._purge_refs()
            self._refs[token] = (canonical, time.monotonic() + REF_TTL_SECONDS)
        return token

    def _purge_refs(self) -> None:
        now = time.monotonic()
        for token in [t for t, (_, exp) in self._refs.items() if exp < now]:
            self._refs.pop(token, None)

    def consume_ref(self, token: str) -> MarketEntry:
        with self._lock:
            self._purge_refs()
            hit = self._refs.pop(token or "", None)
        if hit is None:
            raise MarketError("安装凭证无效或已过期，回目录页重新点一次安装")
        canonical, _ = hit
        entries, _ = self.load_index()
        entry = entries.get(canonical)
        if entry is None:
            raise MarketError(f"目录里已经没有这条了：{canonical}")
        return entry

    # ---------- 安装 ----------

    def install_entry(self, entry: MarketEntry, *, via: Optional[str] = None) -> Dict[str, Any]:
        """装一条目录条目。返回 {"needs_apply": bool, "installed": [component keys]}"""
        if get_builtin(entry.canonical):
            raise MarketError("该插件已随应用内置，无需重复安装")
        needs_apply = False
        done: List[str] = []
        data = self._load_installed()
        try:
            if entry.kind == "plugin":
                if (data.get("bundles") or {}).get(entry.canonical):
                    raise MarketError(f"组合包已经装过了：{entry.name}")
                if not entry.components:
                    raise MarketError("组合包没有声明任何成员")
                for spec in entry.components:
                    child = self._component_entry(spec)
                    result = self.install_entry(child, via=entry.canonical)
                    needs_apply = needs_apply or result["needs_apply"]
                    done.extend(result["installed"])
                with self._lock:
                    data = self._load_installed()
                    data["bundles"][entry.canonical] = {
                        "version": entry.version,
                        "installed_at": _now(),
                        "components": list(done),
                    }
                    self._save_installed()
            else:
                key = _component_key(entry.kind, entry.canonical)
                if self.state_for(entry)["installed"]:
                    raise MarketError(f"已经装过了：{entry.name}")
                if entry.install.kind not in INSTALL_KINDS:
                    # 目录写漏了 install.kind 时，往下走会得到"stdio 需要 command"这类
                    # 莫名其妙的校验错 —— 那说的是实现细节，不是"目录这条写坏了"。
                    raise MarketError(
                        f"目录条目没声明怎么装（install.kind 应为 {' / '.join(INSTALL_KINDS)}）："
                        f"{entry.canonical}"
                    )
                if entry.kind == "skill":
                    self._install_skill(entry)
                elif entry.kind == "mcp":
                    self._install_mcp(entry)
                    needs_apply = True
                else:
                    raise MarketError(f"kind「{entry.kind}」不能直接安装")
                with self._lock:
                    data = self._load_installed()
                    data["components"][key] = {
                        "version": entry.version,
                        "installed_at": _now(),
                        "via": via,
                    }
                    self._save_installed()
                done.append(key)
        except Exception:
            # 装到一半失败：把这次装上的回滚掉，留下半个包比失败更糟
            for key in reversed(done):
                kind, _, ref = key.partition(":")
                try:
                    self._remove_component(kind, ref)
                except Exception as e:  # noqa: BLE001
                    logger.error("回滚 %s 失败: %s", key, e)
            with self._lock:
                # 磁盘回滚了，注册表也得跟着回滚：只删目录会留下"记录在、东西没了"
                # 的孤儿条目 —— 它在界面上永远显示已安装，还挡住了下次重装。
                data = self._load_installed()
                for key in done:
                    data["components"].pop(key, None)
                data["bundles"].pop(entry.canonical, None)
                self._save_installed()
            raise
        return {"needs_apply": needs_apply, "installed": done}

    def _install_skill(self, entry: MarketEntry) -> Path:
        spec = entry.install
        if spec.kind == "pack-dir":
            src = settings.market_packs_dir / spec.path
            if not (src / "SKILL.md").is_file():
                raise MarketError(f"内置技能包不完整：{spec.path}")
            return self._place_skill_dir(src, entry.canonical)
        if spec.kind == "zip-url":
            root, tmp = self._download_and_extract(spec.url)
            try:
                return self._place_skill_dir(root, entry.canonical)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        raise MarketError(f"技能条目不支持的安装方式：{spec.kind or '（空）'}")

    def _place_skill_dir(self, src: Path, canonical: str) -> Path:
        from app.skills import _parse_skill

        skill_md = src / "SKILL.md"
        if not skill_md.is_file():
            raise MarketError("包里没有 SKILL.md")
        parsed = _parse_skill(skill_md, "user")
        if not parsed.usable:
            raise MarketError(f"SKILL.md 不可用：{parsed.error or '未知原因'}")
        dest = settings.skills_dir / _safe_dir_name(parsed.name, canonical)
        if dest.exists():
            raise MarketError(f"技能目录已存在：{dest.name}（先卸载或改名）")
        shutil.copytree(src, dest)
        return dest

    def _download_and_extract(self, url: str) -> Tuple[Path, Path]:
        """下载并解压到临时目录。返回 (技能根目录, 临时根目录)；临时目录由调用方清理"""
        if not url.lower().startswith(("http://", "https://")):
            raise MarketError("技能包地址必须以 http:// 或 https:// 开头")
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="xjms-market-"))
        zip_path = tmp / "package.zip"
        limit = settings.MARKET_MAX_DOWNLOAD_BYTES
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    written = 0
                    with zip_path.open("wb") as fh:
                        for chunk in resp.iter_bytes(64 * 1024):
                            written += len(chunk)
                            if written > limit:
                                raise MarketError(
                                    f"技能包超过上限 {limit // (1024 * 1024)}MB，已中止"
                                )
                            fh.write(chunk)
            extract_dir = tmp / "content"
            extract_dir.mkdir()
            self._safe_unzip(zip_path, extract_dir)
            zip_path.unlink(missing_ok=True)
            root = self._find_skill_root(extract_dir)
        except MarketError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(tmp, ignore_errors=True)
            raise MarketError(f"下载技能包失败：{type(e).__name__}: {e}") from e
        return root, tmp

    @staticmethod
    def _find_skill_root(extract_dir: Path) -> Path:
        """解出来的内容里找到技能根目录（SKILL.md 所在那一层）"""
        if (extract_dir / "SKILL.md").is_file():
            return extract_dir
        candidates = [
            p for p in sorted(extract_dir.iterdir())
            if p.is_dir() and (p / "SKILL.md").is_file()
        ]
        if not candidates:
            raise MarketError("压缩包里没有找到 SKILL.md")
        if len(candidates) > 1:
            raise MarketError("压缩包里放了多个技能目录，一次只能装一个")
        return candidates[0]

    def _extract_zip_bytes(self, content: bytes) -> Tuple[Path, Path]:
        """把界面上传的 zip 字节解到临时目录。返回 (内容根, 临时根)，临时根归调用方清理"""
        if len(content) > settings.MARKET_MAX_DOWNLOAD_BYTES:
            raise MarketError(
                f"上传的包超过上限 {settings.MARKET_MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB"
            )
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="xjms-market-"))
        try:
            zip_path = tmp / "package.zip"
            zip_path.write_bytes(content)
            extract_dir = tmp / "content"
            extract_dir.mkdir()
            self._safe_unzip(zip_path, extract_dir)
            zip_path.unlink(missing_ok=True)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return extract_dir, tmp

    @staticmethod
    def _inside(extract_dir: Path, rel: str) -> Path:
        """插件清单里的相对路径 → 解压目录内的真实路径（越界即拒）"""
        raw = (rel or "").strip().replace("\\", "/")
        if not raw or raw.startswith("/") or ".." in raw.split("/"):
            raise MarketError(f"插件清单里的路径必须是包内的相对路径：{rel!r}")
        target = (extract_dir / raw).resolve()
        root = extract_dir.resolve()
        if target != root and not str(target).startswith(str(root) + os.sep):
            raise MarketError(f"插件清单里的路径跑到了包外面：{rel!r}")
        return target

    def _safe_unzip(self, zip_path: Path, dest: Path) -> None:
        dest_resolved = dest.resolve()
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise MarketError(f"zip 文件数超过上限 {MAX_ZIP_ENTRIES}")
            for info in infos:
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise MarketError(f"zip 含越界路径，已拒绝：{name}")
                target = (dest / name).resolve()
                if target != dest_resolved and not str(target).startswith(
                    str(dest_resolved) + os.sep
                ):
                    raise MarketError(f"zip 含越界路径，已拒绝：{name}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)

    def _install_mcp(self, entry: MarketEntry) -> None:
        from app.mcp_store import get_mcp_store

        payload = dict(entry.install.server or {})
        if not payload.get("name"):
            payload["name"] = entry.canonical
        payload.setdefault("display_name", entry.name)
        payload.setdefault("danger_level", entry.danger_level)
        payload.setdefault("note", f"来自插件市场：{entry.name}")
        try:
            get_mcp_store().save(payload)
        except ValueError as e:
            raise MarketError(str(e)) from e

    # ---------- 卸载 / 启停 ----------

    def uninstall(self, canonical: str) -> Dict[str, Any]:
        if get_builtin(canonical):
            raise MarketError("内置插件随应用更新，不支持单独卸载")
        entries, _ = self.load_index()
        entry = entries.get(canonical)
        data = self._load_installed()
        bundle = (data.get("bundles") or {}).get(canonical)

        # 以注册表为准判断"是不是组合包"：目录源是会变的（远程条目下架、改名），
        # 而当初装进去的成员关系记在本地 —— 只认目录会让这类包永远卸不掉。
        if bundle is not None or (entry is not None and entry.kind == "plugin"):
            if bundle is None:
                raise MarketError(f"组合包没装过：{canonical}")
            needs_apply = False
            for key in reversed(list(bundle.get("components") or [])):
                kind, _, ref = key.partition(":")
                if self._remove_component(kind, ref):
                    needs_apply = True
            with self._lock:
                # 成员的注册表记录要跟着包一起清掉：只删目录会留下"记录在、东西没了"
                # 的孤儿条目，界面上它永远显示已安装。
                for key in bundle.get("components") or []:
                    data["components"].pop(key, None)
                data["bundles"].pop(canonical, None)
                self._save_installed()
            return {"needs_apply": needs_apply}

        kind = entry.kind if entry is not None else self._guess_kind(canonical)
        key = _component_key(kind, canonical)
        record = (data.get("components") or {}).get(key)
        live_names = self._live_mcps() if kind == "mcp" else self._live_skills()
        if record and canonical not in live_names:
            # 孤儿账：注册表说装过，磁盘/配置里已经没有了（被手动删掉、或上次回滚没清干净）。
            # 这种既装不上（"已经装过了"）也卸不掉（"内置能力只能停用"）—— 只能清账。
            with self._lock:
                data["components"].pop(key, None)
                self._save_installed()
            return {"needs_apply": False}
        via = (record or {}).get("via")
        if via:
            raise MarketError(f"它是组合包「{via}」的成员，要卸请先卸整个包")
        state = self.state_for(entry or MarketEntry(kind=kind, canonical=canonical, name=canonical))
        if not state["installed"]:
            raise MarketError(f"没有安装：{canonical}")
        if not state["uninstallable"]:
            raise MarketError("内置能力只能停用，不能卸载")
        needs_apply = self._remove_component(kind, canonical)
        with self._lock:
            data["components"].pop(key, None)
            self._save_installed()
        return {"needs_apply": needs_apply}

    def _guess_kind(self, canonical: str) -> str:
        if canonical in (self._load_installed().get("bundles") or {}):
            return "plugin"
        if canonical in self._live_mcps():
            return "mcp"
        return "skill"

    def _remove_component(self, kind: str, ref: str) -> bool:
        """返回是否动了 MCP（调用方需要跟着对账一次）"""
        if kind == "skill":
            live = self._live_skills().get(ref)
            if live is None:
                return False
            if live["origin"] != "user":
                raise MarketError("内置技能只能停用，不能卸载")
            shutil.rmtree(Path(live["dir"]), ignore_errors=True)
            from app.skills import get_skill_store

            get_skill_store().scan(force=True)
            return False
        if kind == "mcp":
            from app.mcp_store import get_mcp_store

            try:
                get_mcp_store().delete(ref)
            except KeyError:
                return False
            return True
        raise MarketError(f"不认识的能力类型：{kind}")

    def set_enabled(self, canonical: str, enabled: bool) -> Dict[str, Any]:
        if get_builtin(canonical):
            raise MarketError("内置插件作为基础能力提供，使用范围遵循当前工具权限设置")
        entries, _ = self.load_index()
        entry = entries.get(canonical)
        data = self._load_installed()
        bundle = (data.get("bundles") or {}).get(canonical)
        needs_apply = False

        if bundle is not None:
            # 成员名单以注册表为准（理由同 `uninstall`）：目录可能已经变了。
            # 某个成员已经不在（被手动删了）时跳过并记日志，不让整包开关卡死 ——
            # 缺成员这件事由 `state_for` 的 status_note 说，不在这里报错。
            for key in bundle.get("components") or []:
                kind, _, ref = key.partition(":")
                if kind not in ("skill", "mcp"):
                    continue
                try:
                    needs_apply = self.set_enabled(ref, enabled)["needs_apply"] or needs_apply
                except MarketError as e:
                    logger.warning("组合包 %s 的成员 %s 启停跳过: %s", canonical, key, e)
            return {"needs_apply": needs_apply}

        kind = entry.kind if entry is not None else self._guess_kind(canonical)
        if kind == "plugin" and entry is not None:
            for spec in entry.components:
                child = self._component_entry(spec)
                needs_apply = self.set_enabled(child.canonical, enabled)["needs_apply"] or needs_apply
            return {"needs_apply": needs_apply}
        if kind == "skill":
            from app.skills import get_skill_store

            try:
                get_skill_store().set_enabled(canonical, enabled)
            except KeyError as e:
                raise MarketError(f"技能不存在：{canonical}") from e
            return {"needs_apply": False}
        if kind == "mcp":
            from app.mcp_store import get_mcp_store

            try:
                get_mcp_store().set_enabled(canonical, enabled)
            except KeyError as e:
                raise MarketError(f"MCP Server 不存在：{canonical}") from e
            return {"needs_apply": True}
        raise MarketError(f"不认识的能力类型：{kind}")

    # ---------- 手动安装（不在目录里的） ----------

    def adhoc_install_skill(self, *, url: str = "", path: str = "") -> Dict[str, Any]:
        """不在目录里的技能：给一个 zip 地址或本地目录，装完进注册表（source=custom）"""
        tmp: Optional[Path] = None
        try:
            if url:
                root, tmp = self._download_and_extract(url)
            elif path:
                src = self.resolve_local(path)
                if not (src / "SKILL.md").is_file():
                    raise MarketError(f"目录里没有 SKILL.md：{src}")
                root = src
            else:
                raise MarketError("给一个 zip 地址或本地目录")
            return self._install_skill_from_root(root)
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def resolve_local(path: str) -> Path:
        """界面填的本机路径：绝对路径直接用，裸名字当作 `market/packs/` 下的目录"""
        src = Path((path or "").strip().strip('"'))
        if not src.is_absolute():
            src = settings.market_packs_dir / src
        return src

    def upload_skill(self, filename: str, content: bytes) -> Dict[str, Any]:
        """
        「上传技能」：本地选一个 zip 技能包，或单个 `SKILL.md`。

        单个 md 也要先落成一个目录再装 —— 技能的材料文件约定就是
        「一个文件夹一个技能」，走同一条落盘路径就不用维护第二套安装逻辑。
        """
        name = (filename or "").strip().lower()
        if len(content) > settings.MARKET_MAX_DOWNLOAD_BYTES:
            raise MarketError(
                f"上传的文件超过上限 {settings.MARKET_MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB"
            )
        tmp: Optional[Path] = None
        try:
            if name.endswith(".zip"):
                extract_dir, tmp = self._extract_zip_bytes(content)
                root = self._find_skill_root(extract_dir)
            elif name.endswith(".md"):
                root, tmp = self._stage_single_md(content)
            else:
                raise MarketError("只收 .zip 技能包或单个 SKILL.md 文件")
            return self._install_skill_from_root(root)
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _stage_single_md(content: bytes) -> Tuple[Path, Path]:
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="xjms-market-"))
        root = tmp / "content"
        root.mkdir()
        (root / "SKILL.md").write_bytes(content)
        return root, tmp

    def _install_skill_from_root(self, root: Path) -> Dict[str, Any]:
        from app.skills import _parse_skill

        parsed = _parse_skill(root / "SKILL.md", "user")
        if not parsed.usable:
            raise MarketError(f"SKILL.md 不可用：{parsed.error or '未知原因'}")
        canonical = parsed.name
        if self._live_skills().get(canonical):
            raise MarketError(f"已经装过了：{canonical}")
        self._place_skill_dir(root, canonical)
        with self._lock:
            data = self._load_installed()
            data["components"][_component_key("skill", canonical)] = {
                "version": "custom",
                "installed_at": _now(),
                "via": None,
            }
            self._save_installed()
        return {"canonical": canonical}

    # ---------- 上传插件（组合包） ----------

    def upload_plugin(self, filename: str, content: bytes) -> Dict[str, Any]:
        """「上传插件」：带 `plugin.json` 清单的 zip"""
        if not (filename or "").strip().lower().endswith(".zip"):
            raise MarketError("插件包要是一个 .zip")
        extract_dir, tmp = self._extract_zip_bytes(content)
        try:
            return self._install_plugin_dir(extract_dir)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def install_plugin_from_path(self, path: str) -> Dict[str, Any]:
        """
        插件包也可以给本机路径（目录或 zip）。

        「通过 Syncore 创建」这条流程的产物就是本机一个目录：她写完插件让她自己
        报路径，比要求她再打一个 zip、用户去文件选择器里找一遍少两步。
        """
        src = self.resolve_local(path)
        if src.is_dir():
            return self._install_plugin_dir(src)
        if src.suffix.lower() == ".zip" and src.is_file():
            return self.upload_plugin(src.name, src.read_bytes())
        raise MarketError(f"这个路径不是一个插件包（要一个含 plugin.json 的目录，或 zip）：{src}")

    def _install_plugin_dir(self, extract_dir: Path) -> Dict[str, Any]:
        manifest_path = extract_dir / PLUGIN_MANIFEST
        if not manifest_path.is_file():
            # 打包时套了一层文件夹是常态（压缩整个目录而不是目录里的内容）：
            # 只有一层含清单的候选时才认，多个就让人自己选，别猜。
            hits = [
                p / PLUGIN_MANIFEST
                for p in sorted(extract_dir.iterdir())
                if p.is_dir() and (p / PLUGIN_MANIFEST).is_file()
            ]
            if len(hits) != 1:
                raise MarketError(f"插件包根目录要有 {PLUGIN_MANIFEST}（列到 {len(hits)} 份清单）")
            manifest_path = hits[0]
            extract_dir = manifest_path.parent
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise MarketError(f"{PLUGIN_MANIFEST} 不是合法 JSON：{e}") from e
        return self._install_plugin_bundle(manifest, extract_dir)

    def _install_plugin_bundle(self, manifest: Any, root: Path) -> Dict[str, Any]:
        if not isinstance(manifest, dict):
            raise MarketError(f"{PLUGIN_MANIFEST} 顶层要是一个对象")
        bundle_id = str(manifest.get("name") or "").strip()
        if not _CANONICAL_RE.match(bundle_id or "x"):
            raise MarketError(
                f"{PLUGIN_MANIFEST} 的 name 要小写字母/数字加连字符（当前「{bundle_id}」）"
            )
        display = str(manifest.get("display_name") or bundle_id).strip()
        version = str(manifest.get("version") or "1.0.0").strip()
        if (self._load_installed().get("bundles") or {}).get(bundle_id):
            raise MarketError(f"这个插件已经装过了：{display}（先卸载再装新版）")
        specs = manifest.get("components") or []
        if not isinstance(specs, list):
            raise MarketError(f"{PLUGIN_MANIFEST} 的 components 要是一个数组")
        mcp_specs = manifest.get("mcpServers") or []
        if not isinstance(mcp_specs, (dict, list)):
            raise MarketError(f"{PLUGIN_MANIFEST} 的 mcpServers 要是一个对象或数组")
        if not specs and not mcp_specs:
            raise MarketError(
                f"{PLUGIN_MANIFEST} 至少要有一个成员：components（技能/目录条目）或 mcpServers（连接器）"
            )

        done: List[str] = []
        needs_apply = False
        try:
            for spec in specs:
                if not isinstance(spec, dict):
                    raise MarketError("components 每一项要写成 {path: 包内目录} 或 {ref: 目录条目}")
                if spec.get("path"):
                    src = self._inside(root, str(spec["path"]))
                    if not (src / "SKILL.md").is_file():
                        raise MarketError(f"插件成员 {spec['path']} 里没有 SKILL.md")
                    done.append(self._install_bundle_skill(src, via=bundle_id, version=version))
                elif spec.get("ref"):
                    child = self._component_entry({"ref": str(spec["ref"])})
                    if child.kind == "plugin":
                        raise MarketError(f"插件成员不能又是插件：{child.canonical}")
                    result = self.install_entry(child, via=bundle_id)
                    needs_apply = needs_apply or result["needs_apply"]
                    done.extend(result["installed"])
                else:
                    raise MarketError("components 每一项要写成 {path: 包内目录} 或 {ref: 目录条目}")
            for name_hint, raw in _iter_mcp_specs(mcp_specs):
                done.append(self._install_bundle_mcp(name_hint, raw, via=bundle_id, version=version, bundle_name=display))
                needs_apply = True
            with self._lock:
                data = self._load_installed()
                data["bundles"][bundle_id] = {
                    "version": version,
                    "name": display,
                    "description": str(manifest.get("description") or ""),
                    "installed_at": _now(),
                    "components": list(done),
                }
                self._save_installed()
        except Exception:
            # 半个插件比装失败更糟：成员和包的账对不上以后，卸载也救不回来
            for key in reversed(done):
                kind, _, ref = key.partition(":")
                try:
                    self._remove_component(kind, ref)
                except Exception as e:  # noqa: BLE001
                    logger.error("回滚 %s 失败: %s", key, e)
            with self._lock:
                data = self._load_installed()
                for key in done:
                    data["components"].pop(key, None)
                data["bundles"].pop(bundle_id, None)
                self._save_installed()
            raise
        return {
            "canonical": bundle_id,
            "name": display,
            "installed": done,
            "needs_apply": needs_apply,
        }

    def _install_bundle_mcp(
        self, name_hint: str, raw: Any, *, via: str, version: str, bundle_name: str
    ) -> str:
        """
        `plugin.json` 里 `mcpServers` 声明的一条连接器。

        ★ 一律按最紧的一档装、且默认停用

        插件包里的 server 会以本机权限起一个进程，而清单是随包来的：
        装包的人没有逐字看过 command / url。把它当成"从别处抄来的配置"
        （danger + 默认关闭）处理，用户确认命令后再在连接器页启用 ——
        至于它该不该给专员用，仍由治理页的授权清单决定。
        """
        from app.mcp_store import get_mcp_store

        name, payload, _dropped = _normalize_mcp_server(name_hint, raw)
        if not raw.get("note"):
            payload["note"] = f"插件「{bundle_name}」随包提供的连接器"
        payload.setdefault("display_name", name)
        payload["danger_level"] = "danger"
        payload["enabled"] = False
        payload["allowed_agents"] = []
        try:
            get_mcp_store().save(payload)
        except ValueError as e:
            raise MarketError(f"插件里的连接器「{name}」装不上：{e}") from e
        key = _component_key("mcp", name)
        with self._lock:
            data = self._load_installed()
            data["components"][key] = {"version": version, "installed_at": _now(), "via": via}
            self._save_installed()
        return key

    def _install_bundle_skill(self, src: Path, *, via: str, version: str) -> str:
        from app.skills import _parse_skill

        parsed = _parse_skill(src / "SKILL.md", "user")
        if not parsed.usable:
            raise MarketError(
                f"插件成员 {src.name} 的 SKILL.md 不可用：{parsed.error or '未知原因'}"
            )
        if self._live_skills().get(parsed.name):
            raise MarketError(f"本机已经有一个同名技能：{parsed.name}（先卸掉它再装插件）")
        self._place_skill_dir(src, parsed.name)
        key = _component_key("skill", parsed.name)
        with self._lock:
            data = self._load_installed()
            data["components"][key] = {"version": version, "installed_at": _now(), "via": via}
            self._save_installed()
        return key

    # ---------- 粘贴 MCP 配置 ----------

    def import_mcp_config(self, text: str) -> Dict[str, Any]:
        """
        吃下「从别处抄来的 MCP 配置」：`{"mcpServers": {...}}`、`{"servers": {...}}`、
        `{"mcpServers": [...]}` 或一个单独的 server 对象都收。

        ★ 一条坏配置不拦其它条
        一次贴三家 server 是常态。全批回滚会让人以为"这个功能不支持这种写法"，
        而逐条落 + 逐条报因，才知道该改哪一行。已落盘的不动（配置写一条算一条）。
        """
        from app.mcp_store import get_mcp_store

        raw = (text or "").strip()
        if not raw:
            raise MarketError("配置是空的")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MarketError(f"这不是合法 JSON：{e}") from e
        if not isinstance(data, dict):
            raise MarketError("顶层要是一个对象（mcpServers 映射，或单个 server）")

        block = data.get("mcpServers")
        if block is None:
            block = data.get("servers")
        if block is None:
            servers: Dict[str, Any] = {"": data}
        elif isinstance(block, dict):
            servers = block
        elif isinstance(block, list):
            servers = {
                str(item.get("name") or ""): item
                for item in block
                if isinstance(item, dict)
            }
        else:
            raise MarketError("mcpServers 要是一个对象或数组")
        if not servers:
            raise MarketError("配置里一个 server 都没有")

        store = get_mcp_store()
        existing = {c.name for c in store.load()}
        created: List[str] = []
        failed: Dict[str, str] = {}
        ignored: Dict[str, List[str]] = {}
        for key, value in servers.items():
            name = str(key).strip()
            try:
                name, payload, dropped = _normalize_mcp_server(str(key), value)
                if name in existing:
                    raise MarketError(f"已经有同名 MCP 服务：{name}（要改去连接器页编辑）")
                store.save(payload)
            except MarketError as e:
                failed[name or str(key)] = str(e)
                continue
            except Exception as e:  # noqa: BLE001 —— store 的校验错原文就是给人看的
                failed[name or str(key)] = f"{type(e).__name__}: {e}"
                continue
            created.append(name)
            existing.add(name)
            if dropped:
                ignored[name] = dropped
        return {
            "created": created,
            "failed": failed,
            "ignored": ignored,
            "needs_apply": bool(created),
        }


def _iter_mcp_specs(block: Any) -> List[Tuple[str, Any]]:
    """`mcpServers` 的两种写法：映射（名字 → 配置）或数组（每项自带 name）"""
    if not block:
        return []
    if isinstance(block, dict):
        return [(str(k), v) for k, v in block.items()]
    out: List[Tuple[str, Any]] = []
    for item in block:
        if not isinstance(item, dict):
            raise MarketError("mcpServers 数组里的每一项要是一个对象")
        out.append((str(item.get("name") or ""), item))
    return out


def _normalize_mcp_server(
    name_hint: str, raw: Any
) -> Tuple[str, Dict[str, Any], List[str]]:
    """
    把各家写法的 MCP 配置折成本机 `MCPServerPayload` 的形状。

    不认识的键**丢掉并回报**，而不是整条报错：别家客户端会带 `autoApprove`
    这类本机没有对应机制的字段，为了一个用不上的键拒绝一条能用的配置，
    是界面在跟用户较劲。丢掉了要说明丢了哪些 —— 悄悄丢掉
    `autoApprove` 会让人以为"我已经给它放行过了"。
    """
    if not isinstance(raw, dict):
        raise MarketError(f"「{name_hint}」的配置不是一个对象")
    payload: Dict[str, Any] = {}
    dropped: List[str] = []
    for key, value in raw.items():
        if key == "type":
            mapped = _TRANSPORT_ALIASES.get(str(value).strip().lower())
            if mapped:
                payload["transport"] = mapped
            else:
                dropped.append(f"type={value}")
        elif key in _MCP_SERVER_KEYS:
            payload[key] = value
        else:
            dropped.append(key)
    name = str(payload.get("name") or name_hint or "").strip()
    if not name:
        raise MarketError("这条配置没有名字（键名或 name 字段至少给一个）")
    payload["name"] = name
    payload.setdefault("transport", "stdio" if payload.get("command") else "streamable_http")
    # 导入的配置默认按最紧的一档走：从别处抄来的 server 做了什么，本机没人知道
    payload.setdefault("danger_level", "danger")
    payload.setdefault("note", "从 JSON 配置导入")
    return name, payload, dropped


_store: Optional[MarketStore] = None
_store_lock = threading.Lock()


def get_market_store() -> MarketStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = MarketStore()
        return _store


# ---------- 「通过 Syncore 创建」的向导文案 ----------

def create_brief(kind: str) -> Dict[str, Any]:
    """
    新建任务时预填的那段话。

    ★ 为什么在后端拼而不是写死在前端
    里面全是路径和格式约束（技能目录在哪、name 必须等于目录名、清单叫什么名字）。
    这些是 `app/skills.py` 和这里的安装器说了算的；写在前端就是第二份真相，
    哪天目录规则改了，界面上的向导会把人往错的地方带。
    """
    if kind not in ("skill", "plugin"):
        raise MarketError(f"创建向导只支持 skill / plugin，收到「{kind}」")
    skills_dir = settings.skills_dir
    staging = settings.market_staging_dir
    if kind == "skill":
        prompt = f"""帮我做一个新技能，装进 Syncore 的技能库。

**先问清楚，别急着写**
用一两轮问明白：这件事要解决什么、我通常给助手什么输入（文件 / 粘贴的文本 / 一个链接）、
希望拿到什么产出。问不出来的按最省事的默认值定，并告诉我你定了什么。

**写到哪里**
`{skills_dir / "<技能名>" / "SKILL.md"}`
- 技能名：小写字母、数字、连字符；**目录名和 frontmatter 里的 name 必须逐字一致**
- frontmatter 至少两行：`name` 和 `description`
- `description` 是助手判断「这件事该不该用这个技能」的唯一线索 —— 写什么时候该用，不要写功能介绍
- 正文写给模型看的步骤：先做什么、输入不齐时怎么办、产出长什么样、哪些事不要做

**写完自检**
调 `skills_list` 确认这条读得出来（读不出来就是格式问题，改到能读出来为止），
然后拿我刚才描述的那件事真跑一遍给我看。
"""
        hint = f"技能会落在 {skills_dir}"
    else:
        prompt = f"""帮我做一个插件（一组一起装的技能），最后交给市场的「上传插件」。

**先问清楚**
这个插件管哪一类事，能拆成 2–4 个各自独立、单独也有用的技能。
每个技能单独问一句「我给它什么、它给我什么」。

**产出结构**
`{staging / "<插件名>"}/`
├── plugin.json
└── skills/<技能名>/SKILL.md   ← 每个技能一个目录

`plugin.json` 形如：
```json
{{
  "name": "<插件名>", "display_name": "给人看的名字", "version": "1.0.0",
  "category": "办公效率", "description": "这个插件管哪一类事",
  "components": [{{"path": "skills/<技能名>"}}],
  "mcpServers": {{
    "<连接器标识>": {{
      "display_name": "连接器中文名",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@模型上下文协议的包名"]
    }}
  }}
}}
```
`components` 也可以用 `{{"ref": "mcp-fetch"}}` 引用市场里已有的条目。
`mcpServers` 用来随包带一个新的连接器（对象或数组都行，数组每项要写 name）；
没有自带连接器就删掉 `mcpServers` 这一段。它装好后默认是**停用**的，
用户在连接器页看过启动命令再启用。
每个 SKILL.md 的规矩和单技能一样：目录名 = frontmatter 的 name，description 写触发场景。

**收尾**
写完把整个目录打成 zip 放到 `{staging}`，把 zip 的完整路径告诉我，
并说清这个插件装好之后能干什么、怎么唤起。市场里点「上传插件」选这个 zip 就算装上了。
"""
        hint = f"产物落在 {staging}，市场的「上传插件」可以直接填这个路径"
    return {"kind": kind, "prompt": prompt, "hint": hint}


__all__ = [
    "KINDS",
    "MarketEntry",
    "MarketError",
    "MarketStore",
    "create_brief",
    "get_market_store",
]
