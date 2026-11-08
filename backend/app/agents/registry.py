"""
智能体注册表 —— 「一个专员是谁」的单一事实来源。

★ 为什么必须有这一层

改造前，一个专员的身份散在四处，彼此独立、必然漂移：

| 维度 | 原来在哪 |
|---|---|
| 显示名 / 领域 / 职责 | `core/prompts.py::AGENT_DESCRIPTIONS` |
| 调度时用什么能力名找到它 | `supervisor.py::CAPABILITY_TO_AGENT` |
| 能碰哪些工具 | 各工具注册时自报的 `allowed_agents` |
| 要不要跑知识库检索 | `runner.py` 里写死的 `agent_name == "rag_agent"` |

结果是「加一个专员改四个地方」，而且**改漏一处不报错**：
漏了 `AGENT_DESCRIPTIONS` → 子 Agent 的 prompt 里 `{domain}` 变空串，
它不知道自己是谁；漏了能力映射 → 调度永远选不到它。
这里把四维收进一份 `AgentSpec`，代码常量退化为**种子默认值**。

★ 与「数字员工」的分工（不是一回事，别混）

专员（本模块）是**平台能力**：异构的一等公民，各自有模型、工具面、检索策略，
改动会影响所有对话。数字员工（`workspace/`）是**一次雇佣**：拿某个专员当模板
（`base_agent`），叠一层自己的 persona 与更窄的工具白名单。
所以员工的 `tools_allowed` 只能在专员的权限之上**收窄**，不能放宽 —— 见 `effective_tools`。

★ 为什么落盘成 JSON 而不是 SQLite

与人设、提示词模板同一取舍（见 `persona/store.py` 的模块注释）：
小体量、跨请求存活、坏了不能拖垮主流程、要能 git diff。
专员数量是个位数，为它建表是过度设计。

★ 生效时机

System Prompt 每轮重新构造，所以改完**下一轮即生效**，不需要重启。
唯一例外是已经在跑的子任务 —— 它拿的是派发那一刻的配置，这是对的。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from app.config import settings
from app.persona.timeutil import now_iso

logger = logging.getLogger(__name__)

STORE_VERSION = 1

#: 专员内部标识：小写字母开头，允许数字与下划线，2-32 位。
#: 它同时是文件名安全的字符集（`data/agents.json` 里当 key），所以不放行连字符与大写。
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: 各字段的长度上限。超限**截断**而不是报错 —— 与 `clamp_employee_payload` 同一口径：
#: 数据入口一处收敛，调用方与存储层都不必再各自校验一遍。
MAX_DISPLAY = 24
MAX_DOMAIN = 20
MAX_RESPONSIBILITY = 200
MAX_PLANNER_HINT = 120
MAX_CAPABILITIES = 8
MAX_CAPABILITY_LEN = 12
MAX_TOOLS = 64
MAX_MODEL = 80

#: 派发失败时的默认兜底专员（文件与知识是最通用、最安全的那一路）
DEFAULT_FALLBACK = "file_agent"


def _clamp(s: Any, max_len: int) -> str:
    return str(s if s is not None else "").strip()[:max_len]


@dataclass
class AgentSpec:
    """一个专员的完整定义。"""

    #: 内部标识（`file_agent`）。工具表的 `allowed_agents`、任务记录的 `agent` 字段都存它。
    name: str
    #: 显示名（「知识库专员」）。进子 Agent 的 System Prompt，也进界面。
    display_name: str
    #: 领域一句话（「知识检索」）。
    domain: str
    #: 职责（「基于知识库内容回答用户问题」）。
    responsibility: str
    #: ★ 调度视角的能力说明。它**故意不等于** `responsibility`：
    #: 前者是「Supervisor 该在什么时候想到我」（"检索知识库与文档，回答基于已有资料的问题"），
    #: 后者是「我该怎么干活」。用同一句话会让调度 prompt 里塞满执行细节。
    planner_hint: str = ""
    #: 人话能力名（「查资料」「知识库」）。模型在计划里选的是这些词，不是内部标识 ——
    #: 见 `SUPERVISOR_PROMPT` 的注释：让模型记 `file_agent` 只会让它挑名字好听的。
    capabilities: list[str] = field(default_factory=list)
    #: 显式工具白名单。空 = 跟随工具表各自的 `allowed_agents`（默认那一路）；
    #: 非空 = 以这份清单为准。语义与员工的 `tools_allowed` 一致。
    tools_allowed: list[str] = field(default_factory=list)
    #: 该专员使用的模型。空串 = 跟随全局（`.env` / 运行时覆写的 `LLM_MODEL`）。
    model: str = ""
    #: 采样温度。None = 跟随全局 `LLM_TEMPERATURE`（`.env`）。
    #: 曾经 runner 写死 0（工具循环的确定性便于重放与评估），但写死会误杀
    #: 只接受单一温度的模型（kimi-k2.7-code 对 temperature != 1 直接 400），
    #: 所以默认值交还给配置：用这类模型时把 `LLM_TEMPERATURE` 配成它接受的值。
    temperature: float | None = None
    #: 工具步数上限。None = 跟随 `settings.AGENT_MAX_TOOL_STEPS`。
    max_steps: int | None = None
    #: 是否在规划前跑知识库检索。改造前是 `agent_name == "rag_agent"` 的硬编码。
    retrieval: bool = False
    #: 常驻 = 主对话里的调度者，不进可派发清单。
    resident: bool = False
    #: 停用后：调度清单里消失、派发到它的子任务被退回兜底专员。
    #: 不删定义，是为了让「先停一停看看效果」不丢历史配置。
    enabled: bool = True
    #: 代码里种出来的专员。内置的可改属性、不可删除 —— 删了主链路就没有专员可用了。
    builtin: bool = True
    updated_at: str = ""

    # ---------- 派生 ----------
    @property
    def dispatchable(self) -> bool:
        """能不能被 Supervisor 当子任务派出去。"""
        return self.enabled and not self.resident

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any], *, builtin: bool) -> "AgentSpec":
        """从落盘字典还原。字段缺失 → 用默认值（**不报错**，见模块注释的容错口径）。"""
        name = _clamp(data.get("name"), 32).lower()
        caps = _clean_capabilities(data.get("capabilities"))
        return cls(
            name=name,
            display_name=_clamp(data.get("display_name"), MAX_DISPLAY) or name,
            domain=_clamp(data.get("domain"), MAX_DOMAIN),
            responsibility=_clamp(data.get("responsibility"), MAX_RESPONSIBILITY),
            planner_hint=_clamp(data.get("planner_hint"), MAX_PLANNER_HINT),
            capabilities=caps,
            tools_allowed=_clean_tools(data.get("tools_allowed")),
            model=_clamp(data.get("model"), MAX_MODEL),
            temperature=_clean_temperature(data.get("temperature")),
            max_steps=_clean_max_steps(data.get("max_steps")),
            retrieval=bool(data.get("retrieval", False)),
            resident=bool(data.get("resident", False)),
            enabled=bool(data.get("enabled", True)),
            builtin=builtin,
            updated_at=_clamp(data.get("updated_at"), 40),
        )

    def planner_line(self) -> str:
        """调度 prompt 里的一行：`- 查资料：检索知识库与文档，…`"""
        hint = self.planner_hint or self.responsibility or self.domain
        return f"- {self.capabilities[0] if self.capabilities else self.name}：{hint}"


# ---------- 字段清洗（唯一入口，PUT/POST/种子共用） ----------

def _clean_capabilities(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        cap = _clamp(item, MAX_CAPABILITY_LEN)
        # 纯数字的能力名不可能是「人话」，而它会被拼进调度 prompt 并占一个路由位。
        if not cap or cap.isdigit():
            continue
        if cap not in out:
            out.append(cap)
        if len(out) >= MAX_CAPABILITIES:
            break
    return out


def _clean_tools(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        tool = _clamp(item, 64)
        if tool and tool not in out:
            out.append(tool)
        if len(out) >= MAX_TOOLS:
            break
    return out


def _clean_temperature(raw: Any) -> float | None:
    """None / 空串 = 跟随现状；越界夹到 [0, 2]。"""
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return min(2.0, max(0.0, v))


def _clean_max_steps(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return min(50, max(1, v))


#: 可写字段 → 清洗器。`name` / `builtin` 不在里面（内置专员的标识是主键，改它等于删了重建）。
_EDITABLE: dict[str, Any] = {
    "display_name": lambda v: _clamp(v, MAX_DISPLAY),
    "domain": lambda v: _clamp(v, MAX_DOMAIN),
    "responsibility": lambda v: _clamp(v, MAX_RESPONSIBILITY),
    "planner_hint": lambda v: _clamp(v, MAX_PLANNER_HINT),
    "capabilities": _clean_capabilities,
    "tools_allowed": _clean_tools,
    "model": lambda v: _clamp(v, MAX_MODEL),
    "temperature": _clean_temperature,
    "max_steps": _clean_max_steps,
    "retrieval": lambda v: bool(v),
    "enabled": lambda v: bool(v),
}


def clamp_agent_payload(data: dict[str, Any]) -> dict[str, Any]:
    """把「新建/编辑专员」的入参收敛成可落盘的安全字典（只返回显式给出的字段）。"""
    out: dict[str, Any] = {}
    for key, cleaner in _EDITABLE.items():
        if key in data:
            out[key] = cleaner(data[key])
    return out


# ---------- 内置专员的种子 ----------

def _seeds() -> list[AgentSpec]:
    """
    四个领域管家 + 主控。种子只是**默认值**：`data/agents.json` 里的用户改动
    在 `_load()` 里覆盖同名种子，界面也能增删自定义专员。
    """
    return [
        AgentSpec(
            name="supervisor",
            display_name="调度者",
            domain="任务调度",
            responsibility="判断直接回答还是派发后台任务",
            planner_hint="",
            capabilities=[],
            resident=True,
            retrieval=False,
        ),
        AgentSpec(
            name="pc_agent",
            display_name="电脑管家",
            domain="系统运维",
            responsibility="执行系统命令、巡检电脑状态、管理进程与服务",
            planner_hint="电脑系统层面的运维：执行命令、查看状态、管理进程",
            capabilities=["系统运维", "执行命令", "电脑", "pc"],
        ),
        AgentSpec(
            name="file_agent",
            display_name="文件管家",
            domain="数字资产",
            responsibility="读写、创建、管理本地文件，检索知识库资料",
            planner_hint="创建 / 读取 / 修改 / 删除本地文件，生成文档，检索知识库",
            capabilities=["写文件", "文件", "知识库", "file"],
            retrieval=True,
        ),
        AgentSpec(
            name="software_agent",
            display_name="软件管家",
            domain="软件与工具",
            responsibility="安装、启动、更新与卸载软件和开发工具",
            planner_hint="软件与开发工具的安装、启动、更新与卸载",
            capabilities=["装软件", "软件", "工具", "software"],
        ),
        AgentSpec(
            name="browser_agent",
            display_name="浏览器管家",
            domain="网页交互",
            responsibility="联网搜索、抓取与阅读网页内容",
            planner_hint="搜索网上信息、打开并阅读网页内容",
            capabilities=["搜网页", "网页", "联网", "browser"],
        ),
    ]


class AgentRegistry:
    """专员定义的持久化（单文件 JSON + stat 缓存 + 原子写）。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._resolved: Path | None = None
        self._lock = threading.RLock()
        self._cache: dict[str, AgentSpec] | None = None
        self._stamp: tuple[float, int] | None = None

    @property
    def path(self) -> Path:
        if self._resolved is None:
            self._resolved = self._path or settings.agents_store_path
        return self._resolved

    # ---------- 读 ----------
    def all(self) -> list[AgentSpec]:
        """
        全部专员。顺序即界面顺序：内置在前 → 调度者在前 → 名字字典序。

        ★ 为什么调度者要排第一而不是跟着字典序
        `sort by name` 会把 supervisor 甩到最后（s > c/e/f/r），
        而它是这条链路的入口 —— 界面上「谁在最上面」传达的就是「谁先接手」。
        """
        specs = list(self._load().values())
        specs.sort(key=lambda s: (not s.builtin, not s.resident, s.name))
        return specs

    def get(self, name: str) -> AgentSpec | None:
        return self._load().get(_clamp(name, 32).lower())

    def dispatchable(self) -> list[AgentSpec]:
        return [s for s in self.all() if s.dispatchable]

    def planner_capabilities(self) -> list[AgentSpec]:
        """调度 prompt 里出现的那些专员（启用的、非常驻的、且给了能力名的）。"""
        return [s for s in self.dispatchable() if s.capabilities]

    def capability_map(self) -> dict[str, str]:
        """人话能力名 → 专员标识。含 `file_agent → file_agent` 这种自指，
        因为模型有时会直接吐内部名（改造前的表就是这么写的）。"""
        out: dict[str, str] = {}
        for spec in self.all():
            out[spec.name] = spec.name
            for cap in spec.capabilities:
                # 先注册的先占住：种子表里「知识库」只属于 file_agent，
                # 用户新建专员时若抢注同名能力，会得到一条告警而不是静默改道。
                if cap in out and out[cap] != spec.name:
                    logger.warning("能力名「%s」已被 %s 占用，%s 上的这条不生效", cap, out[cap], spec.name)
                    continue
                out[cap] = spec.name
        return out

    def fallback_name(self) -> str:
        """兜底专员：优先 `file_agent`，它被停用了就退到任一可派发专员。

        ⚠️ 这里绝不能返回空串：兜底路径是「模型给了个不认识的专员」时走的，
        再给一个不存在的名字会让子 Agent 拿到空工具集 + 空身份，
        **不报错**地干不了活（改造前 `_normalize` 注释里点名的同一个失效）。
        """
        specs = self._load()
        rag = specs.get(DEFAULT_FALLBACK)
        if rag is not None and rag.dispatchable:
            return DEFAULT_FALLBACK
        for spec in specs.values():
            if spec.dispatchable:
                return spec.name
        return DEFAULT_FALLBACK

    def resolve_capability(self, raw: str) -> str:
        """把模型给的能力名收敛成**启用的**专员标识；认不出的一律退兜底。"""
        key = _clamp(raw, 40)
        if not key:
            return self.fallback_name()
        table = self.capability_map()
        hit = table.get(key) or table.get(key.lower())
        if hit is None:
            return self.fallback_name()
        spec = self._load().get(hit)
        if spec is None or not spec.dispatchable:
            return self.fallback_name()
        return hit

    def effective_model(self, state_model: str, name: str) -> str:
        """本轮模型的优先级：用户顶栏显式选的 > 专员自己的 > 全局。

        ★ 为什么用户的选择排在专员前面
        顶栏换模型是**这一轮**的明示意图（「这次用大模型答」），
        专员配置是长期默认。反过来会让用户觉得顶栏的模型选择没生效。
        """
        picked = _clamp(state_model, MAX_MODEL)
        if picked:
            return picked
        spec = self.get(name)
        return spec.model if spec and spec.model else ""

    def effective_temperature(self, name: str) -> float | None:
        spec = self.get(name)
        return spec.temperature if spec else None

    def effective_max_steps(self, name: str) -> int:
        spec = self.get(name)
        limit = spec.max_steps if spec and spec.max_steps else int(settings.AGENT_MAX_TOOL_STEPS)
        return max(1, limit)

    def uses_retrieval(self, name: str) -> bool:
        """这个专员要不要跑知识库检索（替代 `agent_name == "rag_agent"` 的硬编码）。"""
        spec = self.get(name)
        return bool(spec and spec.retrieval)

    def tools_for(self, name: str) -> list[str] | None:
        """显式白名单；None = 跟随工具表各自的 `allowed_agents`。"""
        spec = self.get(name)
        if spec is None or not spec.tools_allowed:
            return None
        return list(spec.tools_allowed)

    def describe_for_prompt(self, name: str) -> dict[str, str]:
        """给 `SUB_AGENT_PROMPT` 用的三元组（保持与 `AGENT_DESCRIPTIONS` 同样的取值口）。"""
        spec = self.get(name)
        if spec is None:
            # ⚠️ 认不出的专员**不能**静默给空串 —— 那正是改造前
            # `AGENT_DESCRIPTIONS.get()` 缺条目时的症状：prompt 里 `{domain}` 变空，
            # 子 Agent 不知道自己是谁，而且不报错。这里退回一个如实的通用身份。
            return {
                "agent_name": _clamp(name, MAX_DISPLAY) or "专员",
                "domain": "通用",
                "responsibility": "按任务描述完成工作，做不到的如实说明",
            }
        return {
            "agent_name": spec.display_name,
            "domain": spec.domain or "通用",
            "responsibility": spec.responsibility or "回答用户问题",
        }

    # ---------- 写 ----------
    def upsert(self, name: str, changes: dict[str, Any]) -> AgentSpec | None:
        """按字段合并更新。内置专员改属性、自定义专员同样走这里。"""
        with self._lock:
            specs = self._load(write=True)
            key = _clamp(name, 32).lower()
            current = specs.get(key)
            if current is None:
                return None
            merged = replace(current, **{k: v for k, v in changes.items() if k in _EDITABLE})
            merged.updated_at = _now_iso()
            specs[key] = merged
            self._save(specs)
            return merged

    def create(self, name: str, changes: dict[str, Any]) -> tuple[AgentSpec | None, str]:
        """新建自定义专员。返回 `(spec, 错误)` —— 成功时错误为空串。"""
        # ★ 先校验再截断。反过来（先 `_clamp(name, 32)`）会把一个 40 字符的名字
        # 悄悄切成 32 字符然后**接受** —— 用户以为建好了 `translate_agent_extra_long…`，
        # 实际存进去的是被腰斩的那一串，而派发时模型给的是全名，永远对不上。
        key = str(name or "").strip().lower()
        if not NAME_RE.match(key):
            return None, "标识只能用小写字母、数字与下划线，且以字母开头（2-32 位）"
        with self._lock:
            specs = self._load(write=True)
            if key in specs:
                return None, f"专员 {key} 已存在"
            if sum(1 for s in specs.values() if not s.builtin) >= MAX_CUSTOM_AGENTS:
                return None, f"自定义专员最多 {MAX_CUSTOM_AGENTS} 个"
            spec = AgentSpec(
                name=key,
                display_name="",
                domain="",
                responsibility="",
                builtin=False,
            )
            merged = replace(spec, **{k: v for k, v in changes.items() if k in _EDITABLE})
            if not merged.display_name:
                merged.display_name = key
            if not merged.capabilities:
                merged.capabilities = [key]
            merged.updated_at = _now_iso()
            specs[key] = merged
            self._save(specs)
            return merged, ""

    def delete(self, name: str) -> tuple[bool, str]:
        """删除。内置专员删不掉 —— 主链路的调度与兜底都假定了它们的存在。"""
        with self._lock:
            specs = self._load(write=True)
            key = _clamp(name, 32).lower()
            spec = specs.get(key)
            if spec is None:
                return False, "专员不存在"
            if spec.builtin:
                return False, "内置专员不能删除，可以停用或恢复默认"
            specs.pop(key)
            self._save(specs)
            return True, ""

    def reset(self, name: str) -> tuple[AgentSpec | None, str]:
        """把某个专员恢复成代码里的种子；自定义专员的「恢复」就是删除。"""
        with self._lock:
            specs = self._load(write=True)
            key = _clamp(name, 32).lower()
            spec = specs.get(key)
            if spec is None:
                return None, "专员不存在"
            if not spec.builtin:
                specs.pop(key)
                self._save(specs)
                return None, ""
            for seed in _seeds():
                if seed.name == key:
                    specs[key] = replace(seed, updated_at=_now_iso())
                    self._save(specs)
                    return specs[key], ""
            return None, "该专员没有可恢复的默认值"

    def reload(self) -> None:
        """丢弃缓存（单测与外部改文件后用）。"""
        with self._lock:
            self._cache = None
            self._stamp = None

    # ---------- 落盘 ----------
    def _load(self, *, write: bool = False) -> dict[str, AgentSpec]:
        path = self.path
        if self._cache is not None and not write:
            if self._stamp == self._stat_stamp(path):
                return self._cache
        if not path.is_file():
            self._cache = {s.name: s for s in _seeds()}
            self._stamp = None
            return self._cache
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("agents") if isinstance(raw, dict) else None
            if not isinstance(entries, dict):
                raise ValueError("agents 字段缺失或不是对象")
            seed_map = {s.name: s for s in _seeds()}
            specs: dict[str, AgentSpec] = {}
            for key, value in entries.items():
                if not isinstance(value, dict):
                    continue
                name = _clamp(key, 32).lower()
                if not NAME_RE.match(name):
                    logger.warning("忽略非法专员名: %r", key)
                    continue
                seed = seed_map.get(name)
                specs[name] = AgentSpec.from_json({**value, "name": name}, builtin=seed is not None)
            # ★ 种子补齐：代码里新加的内置专员，老文件里没有 —— 要能自动出现，
            # 否则升级后调度清单少一路能力，而且没有任何地方会提示。
            for name, seed in seed_map.items():
                specs.setdefault(name, seed)
            self._cache = specs
            self._stamp = self._stat_stamp(path)
            return specs
        except Exception as e:  # noqa: BLE001
            logger.warning("专员注册表不可读，按代码默认值继续（本次不覆盖磁盘内容）: %s", e)
            self._cache = {s.name: s for s in _seeds()}
            self._stamp = None
            return self._cache

    def _save(self, specs: dict[str, AgentSpec]) -> None:
        path = self.path
        payload = {
            "version": STORE_VERSION,
            "updated_at": _now_iso(),
            "agents": {k: s.to_json() for k, s in sorted(specs.items())},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            self._cache = None
            self._stamp = None
        except Exception as e:  # noqa: BLE001
            logger.warning("写入专员注册表失败（本次修改不会持久化）: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _stat_stamp(path: Path) -> tuple[float, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size)


#: 自定义专员的数量上限。专员会进调度 prompt，无上限的话一个 prompt
#: 里塞几十路能力，模型的选取质量必然掉 —— 而且界面也放不下。
MAX_CUSTOM_AGENTS = 12


def planner_capability_lines(specs: Iterable[AgentSpec]) -> str:
    """把可派发专员渲染成调度 prompt 的能力清单（每行 `- 能力：说明`）。"""
    lines = [spec.planner_line() for spec in specs]
    return "\n".join(lines) if lines else "- （当前没有可派发的专员，请直接回答用户）"


_registry: AgentRegistry | None = None


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry


_now_iso = now_iso


__all__ = [
    "DEFAULT_FALLBACK",
    "MAX_CUSTOM_AGENTS",
    "NAME_RE",
    "AgentRegistry",
    "AgentSpec",
    "clamp_agent_payload",
    "get_registry",
    "planner_capability_lines",
]
