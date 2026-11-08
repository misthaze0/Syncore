"""
项目 ↔ 知识来源的边界映射（ADR 0022 阶段 5b）。

★ 规则一句话

一个数字员工能检索到哪些文档 = **它所属项目声明过的前缀** ∪ **没有任何项目声明的部分**。
「没声明 = 公共」而不是「没声明 = 看不见」：后者会让用户今天这套全局知识库
在开启隔离的那一刻对所有员工同时失效，而且表现为「它突然不认识我的资料了」。

★ 为什么键是 `source` 前缀，不是文件路径

过滤点在检索结果上（`rag/retriever` 的 RRF 融合之后），那一刻手上只有 `source`，
没有原始路径。`source` 的形状见 `rag/incremental.derive_source`：
`<监听目录名>/<相对路径>`。所以边界也照这个形状写，例如 `项目A/` 或 `项目A/需求文档/`。
图路命中不带 doc_id（`graph/models.py`），按 doc_id 过滤会让每一条越界图命中静默漏网 ——
ADR 0022 把这条写成了硬约束。

★ 失效边界（对用户不许撒谎的地方）

这是「**取用隔离**」：越界内容检索不到。但向量仍在同一个 Milvus collection 里，
直接打库仍拿得到。单机桌面场景够用；要 store-side 隔离就得接受全量重嵌入。
界面文案必须写「该员工仅可检索本项目资料」，不能写「数据已按项目隔离」。

★ 为什么不进 workspace.db

它描述的是索引侧的归属，与员工/群组之间没有外键关系。为一张两列的表加一次
SQLite 迁移不值，而它坏了的正确退路正好是「当成没有边界」—— 一个空对象就能表达。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from app.config import settings

logger = logging.getLogger(__name__)

STORE_VERSION = 1

#: 单个项目最多声明多少条前缀（够铺几个大目录，又能挡住误把整盘目录树粘进来）
MAX_PREFIXES_PER_WORKSPACE = 50
#: 单条前缀的长度上限
MAX_PREFIX_LENGTH = 200

_lock = threading.RLock()
#: 文件签名 (mtime_ns, size) → 解析结果。检索在每次提问的热路径上，
#: 每次都读盘不值；用签名做失效，改完文件下一轮就生效，不必重启。
_cache: Dict[str, Any] = {"signature": None, "boundaries": {}}


def _signature(path) -> Optional[tuple[int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def normalize_prefix(raw: Any) -> str:
    """把一条边界写成了 `项目A\\需求文档\\` 之类的也归一成 `项目A/需求文档`（POSIX 形状）。"""
    text = str(raw or "").strip().replace("\\", "/").strip("/")
    return text


def validate_boundaries(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    校验并归一 `{workspace_id: [前缀, ...]}`。

    非法输入一律抛 ValueError —— 这是写路径，静默丢掉一条边界等于
    「保存成功了但其实没生效」，比报错更难查。
    """
    if not isinstance(payload, dict):
        raise ValueError("边界表必须是一个对象")
    out: Dict[str, List[str]] = {}
    for workspace_id, raw in payload.items():
        key = str(workspace_id or "").strip()
        if not key:
            raise ValueError("项目 id 不能为空")
        if not isinstance(raw, list):
            raise ValueError(f"项目 {key} 的边界必须是前缀列表")
        cleaned: List[str] = []
        for item in raw:
            prefix = normalize_prefix(item)
            if not prefix:
                continue
            if len(prefix) > MAX_PREFIX_LENGTH:
                raise ValueError(f"前缀过长（>{MAX_PREFIX_LENGTH} 字符）：{prefix[:40]}…")
            if prefix not in cleaned:
                cleaned.append(prefix)
        if len(cleaned) > MAX_PREFIXES_PER_WORKSPACE:
            raise ValueError(
                f"项目 {key} 声明了 {len(cleaned)} 条边界，上限 {MAX_PREFIXES_PER_WORKSPACE} 条"
            )
        out[key] = cleaned
    return out


def load_boundaries() -> Dict[str, List[str]]:
    """
    读出生效的边界表；文件不存在/损坏一律退化成空表。

    空表 = 「谁都没声明」= 没有边界 = 所有员工看得见全部。这是唯一安全的退化方向：
    把「读不到配置」当成「什么都看不到」会让整个检索看起来坏了。
    """
    path = settings.knowledge_scope_path
    signature = _signature(path)
    with _lock:
        if signature is None:
            _cache["signature"] = None
            _cache["boundaries"] = {}
            return {}
        if _cache["signature"] == signature:
            return dict(_cache["boundaries"])

    raw: Dict[str, Any] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("boundaries"), dict):
            raw = data["boundaries"]
        elif isinstance(data, dict):
            # 允许手写的极简形状：顶层直接就是 {项目: [前缀]}
            raw = {k: v for k, v in data.items() if k not in ("version", "updated_at")}
        else:
            logger.warning("知识边界文件结构异常，按「没有边界」处理: %s", path)
    except Exception as e:  # noqa: BLE001
        logger.warning("知识边界文件不可读，按「没有边界」处理: %s → %s", path, e)
        raw = {}

    try:
        boundaries = validate_boundaries(raw)
    except ValueError as e:
        logger.warning("知识边界文件内容不合法，按「没有边界」处理: %s", e)
        boundaries = {}

    with _lock:
        _cache["signature"] = signature
        _cache["boundaries"] = dict(boundaries)
    return boundaries


def save_boundaries(boundaries: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """
    原子写入边界表并让缓存失效（先写临时文件再 `os.replace`，与指纹清单同一做法）。

    返回归一化之后的内容 —— 调用方应当以此为准回显，而不是回显用户传来的原文。
    """
    clean = validate_boundaries(boundaries)
    path = settings.knowledge_scope_path
    payload = {
        "version": STORE_VERSION,
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "boundaries": clean,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    with _lock:
        _cache["signature"] = None
        _cache["boundaries"] = {}
    logger.info("知识边界已更新（%s 个项目声明了范围）: %s", len(clean), path)
    return clean


def set_workspace(workspace_id: str, prefixes: List[str]) -> Dict[str, List[str]]:
    """把某个项目的边界整体替换为 `prefixes`（空列表 = 该项目不再声明任何范围）。"""
    current = load_boundaries()
    key = str(workspace_id or "").strip()
    if not key:
        raise ValueError("项目 id 不能为空")
    current[key] = [p for p in (normalize_prefix(x) for x in prefixes) if p]
    return save_boundaries(current)


def drop_workspace(workspace_id: str) -> Dict[str, List[str]]:
    """项目被删除时清掉它的声明，否则映射表会积累无主前缀并把资料判成「越界」。"""
    current = load_boundaries()
    current.pop(str(workspace_id or "").strip(), None)
    return save_boundaries(current)


def owners_of(source: str, boundaries: Optional[Dict[str, List[str]]] = None) -> Set[str]:
    """这个来源被哪些项目声明了（可能是多个：一份文件同时放进两个项目目录是合法的）"""
    table = boundaries if boundaries is not None else load_boundaries()
    name = str(source or "")
    return {
        workspace_id
        for workspace_id, prefixes in table.items()
        if any(_matches(name, prefix) for prefix in prefixes)
    }


def _matches(source: str, prefix: str) -> bool:
    """
    按**路径边界**匹配，而不是裸字符串前缀。

    `项目A` 必须匹配 `项目A/需求.md`，但不能匹配 `项目AB/需求.md` ——
    后者会让「给项目 A 划范围」顺手把隔壁项目 B 的资料也算进去，
    而这正是隔离要防的那件事。
    """
    if not source or not prefix:
        return False
    return source == prefix or source.startswith(prefix + "/")


def makes_scope(workspace_id: str) -> Optional[Callable[[str], bool]]:
    """
    构造某个项目的可见性判定；**没有任何声明时返回 None**。

    返回 None 而不是「全放行」的函数，是为了让上层能把「本轮没有边界」这件事
    区分出来：没有边界时不必超采 top_k×3，也不必记 scope_filtered_count。
    """
    table = load_boundaries()
    if not any(table.values()):
        return None

    key = str(workspace_id or "").strip()

    def allows(source: str) -> bool:
        owners = owners_of(source, table)
        if not owners:
            return True  # 没有项目声明 → 公共资料，人人可见
        return key in owners

    return allows


def scope_for_employee(employee_id: str) -> Optional[Callable[[str], bool]]:
    """
    数字员工 → 本轮检索边界。返回 None 表示「不设边界」。

    ★ 三种 None 必须合并成一个，因为上层要按它决定要不要超采 3 倍

    员工 id 为空（主对话、普通后台任务）、员工查不到、该项目没声明范围 ——
    对检索来说都是「这一轮没有边界」，但**不能**把它当成「全放行」的函数传下去：
    那样每轮都会白付超采的成本，还会往可观测记录里写一个恒为 0 的筛除数，
    让「边界生效了吗」这个问题再也没有答案。

    ★ 未启用的员工照样按它的项目收窄

    `employee_runtime` 里工具白名单对禁用员工是失效的（跟随 base_agent 默认），
    这里刻意**不**跟随那个行为：边界属于「这个项目的内容归谁」，
    与「这个员工当前能不能上岗」是两件事。
    一个被临时停用的员工，不该反过来变成能读隔壁项目资料的通道。
    """
    if not employee_id:
        return None
    try:
        from app.workspace.employee_runtime import get_employee_workspace_id

        workspace_id = get_employee_workspace_id(employee_id)
    except Exception as e:  # noqa: BLE001
        # 与 persona / 记忆同一纪律：边界是旁路能力，读不到就退回「没有边界」，
        # 绝不能让一次 SQLite 抖动把整轮问答打死。
        logger.warning("解析员工所属项目失败（本轮不设知识边界）: %s → %s", employee_id, e)
        return None
    if not workspace_id:
        return None
    return makes_scope(workspace_id)


def invalidate() -> None:
    """清掉进程内缓存（测试与手工改文件后用）。"""
    with _lock:
        _cache["signature"] = None
        _cache["boundaries"] = {}


def status() -> Dict[str, Any]:
    """给界面/排障看的现状"""
    table = load_boundaries()
    path = settings.knowledge_scope_path
    return {
        "path": str(path),
        "exists": path.is_file(),
        "workspaces_declared": sum(1 for v in table.values() if v),
        "boundaries": table,
    }


__all__ = [
    "STORE_VERSION",
    "drop_workspace",
    "invalidate",
    "load_boundaries",
    "makes_scope",
    "normalize_prefix",
    "owners_of",
    "save_boundaries",
    "scope_for_employee",
    "set_workspace",
    "status",
    "validate_boundaries",
]
