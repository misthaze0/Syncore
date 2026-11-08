"""
增量索引 —— 用「内容指纹清单」找出变更，只重建变了的那部分

★ 为什么是「扫描 + 指纹」而不是操作系统的文件监听事件

文件监听（inotify / FSEvents / ReadDirectoryChangesW）有两个绕不过去的问题：

1. **会丢事件**。进程没在跑的那段时间里发生的变更，事后不会有任何通知补上。
   而知识库目录恰恰就是「后端关着的时候往里丢文件」这种典型用法。
2. **平台与编辑器差异**。三套系统 API；而且编辑器保存一个文件常常是
   「写临时文件 → 改名覆盖」，会触发一串事件，去重逻辑反而成了新的坑。

扫描 + 内容指纹把这两个问题一起消掉：变更检测只依赖
「**当前磁盘状态 vs 上次记录的指纹**」，因此天然幂等、天然能追赶任何遗漏，
也不需要引入 watchdog 之类的新依赖。

代价是变更的发现有一个扫描间隔的延迟。对知识库场景这完全可以接受，
甚至更合适：用户一次丢进 10 个文件，扫一轮批量处理完，
而不是触发 10 轮各自为战的入库。

★ 指纹为什么必须含内容哈希

`mtime + size` 够快，但会在两个方向上出错：
- 编辑器保存了**相同内容**（mtime 变了、内容没变）→ 白跑一轮 Embedding（浪费钱）
- `git checkout` / 复制粘贴把 mtime 重置成更早的时间 → **漏掉真实变更**（更糟）

所以 mtime+size 只当**快速预筛**，真正的判据是 sha256。

★ 改短的文件需要清理旧尾块

`indexer.index_file()` 走的是确定性主键 + upsert，对**块数不变**的重复入库是幂等的。
但文件**改短**时（比如从 10 块变成 6 块），主键是 `sha1(source::chunk_index)`，
所以只会覆盖 chunk 0~5，原先的 chunk 6~9 **原封不动留在库里**。

那些是孤儿块：内容是**旧版本**的，却依然能被检索命中、被注入 Prompt。
症状极隐蔽 —— 用户改了文档，提问却仍然得到改写之前的答案。
现在由 indexer 先暂存全部向量，再 upsert 并按 chunk_index 清理旧尾块。
解析或向量化失败不会提前删除旧索引；提交阶段的存储失败会记录到文档状态，供重试修复。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

from app.config import settings
from app.core.milvus_client import milvus_manager
from app.rag.es_client import es_manager
from app.rag.indexer import IndexResult, index_document
from app.rag.parser import SUPPORTED_SUFFIXES

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1

# 算哈希时的读取块大小（大文件不必一次读进内存）
_HASH_CHUNK_SIZE = 1 << 20

# 同一时刻只允许一轮扫描在跑：后台定时任务与 API 手动触发可能撞车，
# 并发扫描会各自读到陈旧清单、重复入库、甚至互相删掉对方刚写的块。
_scan_lock = asyncio.Lock()


# ---------- 数据结构 ----------
@dataclass
class FileEntry:
    """清单里记录的一个文件（指纹 + 上次入库结果）"""

    source: str
    path: str
    size: int
    mtime: float
    sha256: str
    chunks: int = 0
    indexed_at: str = ""
    keyword_indexed: bool | None = None
    warnings: List[str] = field(default_factory=list)
    error: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "sha256": self.sha256,
            "chunks": self.chunks,
            "indexed_at": self.indexed_at,
            "keyword_indexed": self.keyword_indexed,
            "warnings": self.warnings,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, source: str, data: Dict[str, Any]) -> "FileEntry":
        return cls(
            source=source,
            path=str(data.get("path", "")),
            size=int(data.get("size", 0) or 0),
            mtime=float(data.get("mtime", 0.0) or 0.0),
            sha256=str(data.get("sha256", "")),
            chunks=int(data.get("chunks", 0) or 0),
            indexed_at=str(data.get("indexed_at", "")),
            keyword_indexed=data.get("keyword_indexed"),
            warnings=list(data.get("warnings") or []),
            error=str(data.get("error") or ""),
        )


@dataclass
class ScanPlan:
    """一次扫描得出的「要做什么」，本身不产生副作用"""

    added: List[tuple[str, Path]] = field(default_factory=list)
    modified: List[tuple[str, Path]] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    unchanged: int = 0
    scanned_files: int = 0
    # 一个来源标识被多个路径占住（监听目录同名或互相嵌套时才会出现）
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    skipped_overflow: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------- 指纹 ----------
def hash_file(path: Path) -> str:
    """流式计算 sha256（不把整个文件读进内存）"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(_HASH_CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path: Path, source: str) -> FileEntry:
    """计算一个文件的指纹（此时还不知道切了几块）"""
    stat = path.stat()
    return FileEntry(
        source=source,
        path=str(path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        sha256=hash_file(path),
    )


# ---------- 清单读写 ----------
def _empty_manifest() -> Dict[str, Any]:
    return {"version": MANIFEST_VERSION, "updated_at": "", "files": {}, "last_scan": None}


def load_manifest() -> Dict[str, Any]:
    """
    读取指纹清单。

    ⚠️ 任何异常都降级为「空清单」而不是抛出去。清单损坏的后果应该是
    「这一轮把所有文件当成新增、重跑一次入库」（幂等，可自愈），
    而不是「增量索引功能整个不可用」。
    """
    path = settings.index_manifest_path
    if not path.is_file():
        return _empty_manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("指纹清单不可读，按空清单处理（本轮会全量重扫）: %s", e)
        return _empty_manifest()

    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        logger.warning("指纹清单结构异常，按空清单处理: %s", path)
        return _empty_manifest()

    files = {}
    for key, value in data["files"].items():
        try:
            if isinstance(value, dict):
                files[str(key)] = FileEntry.from_json(str(key), value)
        except (TypeError, ValueError):
            logger.warning("跳过损坏的知识库清单条目：%s", key)
    return {
        "version": int(data.get("version", MANIFEST_VERSION) or MANIFEST_VERSION),
        "updated_at": str(data.get("updated_at", "")),
        "files": files,
        "last_scan": data.get("last_scan"),
    }


def save_manifest(manifest: Dict[str, Any]) -> None:
    """
    原子写入清单：先写临时文件再 `os.replace`。

    直接覆盖写的话，进程在写到一半时被杀（Ctrl-C、容器重启）会留下
    半截 JSON —— 下次加载解析失败 → 退化成空清单 → 全量重扫。
    虽然能自愈，但要白白重跑一轮 Embedding。
    """
    path = settings.index_manifest_path
    files = manifest.get("files", {})
    payload = {
        "version": MANIFEST_VERSION,
        "updated_at": _now_iso(),
        "files": {k: (v.to_json() if isinstance(v, FileEntry) else v) for k, v in files.items()},
        "last_scan": manifest.get("last_scan"),
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        logger.warning("写入知识库清单失败: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise OSError("知识库清单保存失败，请检查磁盘空间与写入权限") from e


# ---------- 扫描（纯计算，无副作用） ----------
def _excluded_paths() -> set[str]:
    """
    扫描时必须排除的路径（规范化后的绝对路径字符串）。

    ⚠️ 这是本模块唯一一个「能把自己搞死」的地方，必须显式排除：

    指纹清单本身是 `.json` —— **恰好落在允许入库的白名单里**。
    如果它位于被监听的目录内（非常容易发生：把 `INDEX_WATCH_DIRS` 配成 `./data`，
    而清单默认就是 `./data/index_manifest.json`），就会形成自指循环：

        扫描 → 重写清单 → 清单内容变了 → 下一轮扫描把它当「已修改文档」→
        重新向量化 → 又重写清单 → ……

    这个循环**不会报任何错**，只会让 Embedding 调用量随扫描轮次无限增长。
    同理排除长期记忆的存储文件（也是 .json）与运行时覆写文件
    （`data/runtime_config.json` —— 它同样是 .json，而且**界面每改一次模型它就会变**，
    真被扫进去的话，改一次模型就会触发一轮重新向量化）。

    ⚠️ 后台子任务的存储是**目录级**排除，不是文件级
    （Supervisor 架构，见 decisions/0019）。`data/tasks/` 下每个任务一个
    `.jsonl`，而 `.jsonl` 恰好也在入库白名单里。不排掉的话：
    派发一个任务 → 写 `<task_id>.jsonl` → 下一轮扫描把它当新文档 →
    重新向量化 → 子 Agent 又推进一步进度 → 又写一行 → ……
    与上面同一种自指循环，而且**触发频率高得多**（后台任务一直在推进）。
    文件级排除在这里不够用 —— 任务 id 是动态的，没有一个可以提前列出来的路径。
    """
    excluded: set[str] = set()
    for path in (
        settings.index_manifest_path,
        settings.memory_store_path,
        settings.runtime_config_path,
    ):
        try:
            excluded.add(os.path.normcase(os.path.abspath(str(path))))
        except Exception:  # noqa: BLE001
            continue

    # 目录级排除：整个子任务存储目录（见上文说明）
    try:
        excluded.add(os.path.normcase(os.path.abspath(str(settings.subtask_dir))))
    except Exception:  # noqa: BLE001
        pass
    return excluded


def _excluded_dirs() -> set[str]:
    """
    整个目录都要跳过的路径（规范化后的绝对路径字符串）。

    与 `_excluded_paths` 分开是因为用途不同：前者判「这个文件是不是自己写的」，
    这里判「这个目录的内容一律不看」。子任务的 `task_id` 是运行时生成的，
    没有一个可以提前枚举的路径，只能按目录排除。
    """
    dirs: set[str] = set()
    try:
        dirs.add(os.path.normcase(os.path.abspath(str(settings.subtask_dir))))
    except Exception:  # noqa: BLE001
        pass
    return dirs


def derive_source(root: Path, path: Path) -> str:
    """
    监听目录里一个文件的来源标识 = `<监听目录名>/<相对路径>`（POSIX 分隔）。

    ★ 为什么不再是裸文件名（ADR 0022 的前置修复）

    `source` 同时是清单的键、Milvus/ES 的删除与过滤键、图回链的 join 键。
    用裸文件名时，两个项目里的 `需求.md` 会互相屏蔽（冲突组整组跳过，两边都检索不到）。
    今天这只是可用性缺陷，一旦按项目隔离，同名文件从异常变成常态 ——
    「项目 A 与项目 B 互相把对方的资料抹掉」不可接受。路径感知之后两者各占一个键，
    项目归属也才有地方挂。

    手动上传的文档用裸文件名（`POST /files/upload` 没有目录概念），
    与这里的键天然不会撞：监听来源一定含 `/`，上传来源一定不含。

    ⚠️ 改这个函数等于改掉所有既有 `source` 值，牵连 doc_id / ES `_id` / 图回链
    （ADR 0005 的同源性不变量），必须与一次性重索引一起做，不能指望增量自己收敛。
    """
    try:
        rel = path.relative_to(root)
    except ValueError:  # 不在 root 下面（调用方给错了），退化成文件名
        rel = Path(path.name)
    return f"{root.name}/{rel.as_posix()}"


def _is_under(path_str: str, roots: Sequence[Path]) -> bool:
    """清单里记的路径是否落在这些监听目录之下"""
    if not path_str:
        return False
    try:
        target = Path(path_str).resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if target == resolved or resolved in target.parents:
            return True
    return False


def _iter_candidate_files(
    dirs: Sequence[Path],
) -> tuple[List[tuple[str, Path]], int, Dict[str, List[str]]]:
    """
    列出待入库的 (来源标识, 路径)，并检测「同一个键被两个文件占住」的冲突。

    Returns:
        (待处理项, 因超上限被丢弃的数量, {来源标识: [冲突路径, ...]})
    """
    seen: Dict[str, Path] = {}
    seen_abs: set[str] = set()
    conflicts: Dict[str, List[str]] = {}
    found: List[tuple[str, Path]] = []
    dropped = 0
    excluded = _excluded_paths()
    excluded_dirs = _excluded_dirs()

    for root in dirs:
        if not root.is_dir():
            logger.warning("增量索引目录不存在，已跳过: %s", root)
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            # 跳过本模块自己的状态文件（见 _excluded_paths 的自指循环说明）
            absolute = os.path.normcase(os.path.abspath(str(p)))
            if absolute in excluded or absolute in seen_abs:
                # seen_abs：两个监听目录嵌套时（`D:/知识` 与 `D:/知识/项目A`），
                # 同一个文件会拿到两个不同的来源标识并**入库两遍** —— 裸文件名时代
                # 靠同名冲突误打误撞挡住了，改成路径感知后必须自己挡住。先出现的目录赢。
                continue
            # 跳过后台子任务目录（见 _excluded_dirs：任务 id 是动态的，
            # 没法逐个文件排除，只能按目录）
            if any(absolute.startswith(d + os.sep) for d in excluded_dirs):
                continue
            # 跳过隐藏文件与编辑器临时文件（.swp / ~ 结尾 / .tmp）
            if p.name.startswith(".") or p.name.endswith(("~", ".tmp", ".swp")):
                continue
            if len(found) >= settings.INDEX_WATCH_MAX_FILES:
                dropped += 1
                continue
            source = derive_source(root, p)
            if source in seen:
                conflicts.setdefault(source, [str(seen[source])]).append(str(p))
            else:
                seen[source] = p
            seen_abs.add(absolute)
            found.append((source, p))

    if conflicts:
        logger.warning(
            "发现 %s 组来源标识冲突：同一个 source 被多个路径占住，本轮整组跳过。"
            "通常是两个监听目录名相同或互相嵌套。示例：%s",
            len(conflicts),
            list(conflicts.items())[:2],
        )
    if dropped:
        logger.warning(
            "文件数超过 INDEX_WATCH_MAX_FILES=%s，本轮有 %s 个文件未处理（提高上限或缩小目录）",
            settings.INDEX_WATCH_MAX_FILES,
            dropped,
        )

    return found, dropped, conflicts


def scan(dirs: Sequence[Path] | None = None) -> ScanPlan:
    """
    扫描目录并与清单比对，得出本轮变更（**不产生任何副作用**）。

    做成纯函数是为了可单测：给定「目录内容 + 清单」，输出必须完全确定，
    不需要 Milvus / ES / Embedding 任何一个。
    """
    dirs = list(dirs) if dirs is not None else settings.index_watch_dirs_list
    manifest = load_manifest()
    known: Dict[str, FileEntry] = manifest.get("files", {})

    files, dropped, conflicts = _iter_candidate_files(dirs)

    plan = ScanPlan(scanned_files=len(files), skipped_overflow=dropped)
    plan.warnings = [f"监听目录不可用，已保留其索引：{p}" for p in dirs if not p.is_dir()]
    plan.conflicts = [{"source": k, "paths": v} for k, v in conflicts.items()]
    # 用集合做 O(1) 判定：冲突组里的来源整组跳过（无法判断该保留哪一份）
    conflicted_sources = set(conflicts)

    on_disk: set[str] = set()
    for source, path in files:
        if source in conflicted_sources:
            continue
        on_disk.add(source)

        old = known.get(source)
        if old is None:
            plan.added.append((source, path))
            continue

        if old.error or old.keyword_indexed is False:
            plan.modified.append((source, path))
            continue

        stat = path.stat()
        # 快速预筛：size 与 mtime 都一致就不必读文件算哈希
        if old.size == stat.st_size and abs(old.mtime - stat.st_mtime) < 1e-6:
            plan.unchanged += 1
            continue

        # 慢路径：真算内容哈希。编辑器「原样保存」在这里被识别为无变更
        if old.sha256 and hash_file(path) == old.sha256:
            plan.unchanged += 1
            continue

        plan.modified.append((source, path))

    # 清单里有、磁盘上没有 → 已删除。
    # ⚠️ 只判监听目录管辖范围内的条目：手动上传的文档落在 `data/uploads`，
    # 永远不在监听目录里，若把它当成「已删除」，一次扫描就会把用户刚上传的资料
    # 从 Milvus 和 ES 里连带清单一起清掉 —— 扫描的删除判定只对监听目录里的文件有权威。
    for source, entry in known.items():
        if source in on_disk or source in conflicted_sources:
            continue
        if not _is_under(entry.path, [root for root in dirs if root.is_dir()]):
            continue
        # Unvisited files (overflow/exclusions) must never be mistaken for deletions.
        if Path(entry.path).is_file():
            continue
        plan.deleted.append(source)

    return plan


# ---------- 应用变更（有副作用） ----------
async def _purge(source: str) -> Dict[str, int]:
    """把一个来源从两个存储里彻底删掉，返回各自删除条数"""
    milvus_deleted = await asyncio.to_thread(milvus_manager.delete_by_source, source, strict=True)
    es_deleted = await es_manager.delete_by_source(source, strict=True)
    if settings.GRAPH_ENABLED:
        from app.graph.store import graph_store

        await graph_store.delete_by_source(source, strict=True)
    return {"milvus": milvus_deleted, "es": es_deleted}


async def apply_plan(plan: ScanPlan) -> Dict[str, Any]:
    """
    执行扫描计划：删除 → 重建 → 更新清单。

    文档更新由 indexer 暂存向量、覆盖新块、清理旧尾块。
    逐文件记录失败并继续处理其余文件，清单保留错误条目以供重试。
    """
    manifest = load_manifest()
    files: Dict[str, FileEntry] = manifest.get("files", {})

    report: Dict[str, Any] = {
        "added": [],
        "modified": [],
        "deleted": [],
        "failed": [],
        "indexed_chunks": 0,
        "reused_unchanged": plan.unchanged,
        "scanned_files": plan.scanned_files,
        "conflicts": plan.conflicts,
        "skipped_overflow": plan.skipped_overflow,
        "warnings": plan.warnings,
    }

    # ---------- 1. 彻底删除（磁盘上已不存在的来源） ----------
    for source in plan.deleted:
        try:
            counters = await _purge(source)
            files.pop(source, None)
            report["deleted"].append({"source": source, **counters})
        except Exception as exc:
            files[source].error = f"删除未完成：{exc}"
            report["failed"].append({"source": source, "error": str(exc)})

    # ---------- 2. 新增 + 改写 ----------
    # 新增与改写共用暂存后提交的管道，不能提前清空旧索引。
    for kind, items in (("added", plan.added), ("modified", plan.modified)):
        for source, path in items:
            entry = None
            try:
                entry = await asyncio.to_thread(fingerprint, path, source)
                if entry.size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
                    raise ValueError(f"文件超过 {settings.MAX_FILE_SIZE_MB} MB 上限")
                result = await index_document(str(path), source)
                await _invalidate_graph(source, result)
            except Exception as e:  # noqa: BLE001
                logger.warning("增量索引：%s 处理异常: %s", source, e, exc_info=True)
                report["failed"].append({"source": source, "error": str(e)})
                if source in files:
                    files[source].error = str(e)
                elif entry is not None:
                    entry.error = str(e)
                    files[source] = entry
                continue

            entry.chunks = result.chunks
            entry.indexed_at = _now_iso()
            entry.keyword_indexed = result.keyword_indexed
            entry.warnings = result.warnings
            files[source] = entry
            report["indexed_chunks"] += result.chunks
            report[kind].append({"source": source, "path": str(path), "chunks": result.chunks})
            report["warnings"].extend(f"{source}：{w}" for w in result.warnings)

    save_manifest({**manifest, "files": files})
    return report


# ---------- 对外入口 ----------
async def record_indexed(path: Path, source: str, chunks: int) -> None:
    """
    把一个「刚刚成功入库」的文件写进清单。

    手动上传（`POST /files/upload`）走的是这条：不登记的话清单里就没有这个来源，
    于是「清单登记数 vs 存储实测块数」这组互相印证的数字永久性对不上，
    而且这份文档在界面上既列不出来也删不掉。

    等 `_scan_lock` 是为了不和正在跑的扫描交错读改清单——两边都是
    「整表读 → 整表写」，交错必然丢掉对方的结果。扫描在入库阶段可能跑上一两分钟，
    但文件此刻已经进库了，这里只是补登记，晚一点写不损害正确性。
    """
    async with _scan_lock:
        await _record_indexed(path, source, chunks)


async def _record_indexed(path: Path, source: str, chunks: int, result: IndexResult | None = None) -> None:
    """`record_indexed` 的主体，调用方必须已持有 `_scan_lock`。"""
    if not path.is_file():
        logger.debug("登记指纹失败（文件不可读）: %s", source)
        raise FileNotFoundError(f"原文件不存在：{source}")
    entry = await asyncio.to_thread(fingerprint, path, source)
    manifest = load_manifest()
    files: Dict[str, FileEntry] = manifest.get("files", {})
    entry.chunks = chunks
    entry.indexed_at = _now_iso()
    if result is not None:
        entry.keyword_indexed = result.keyword_indexed
        entry.warnings = result.warnings
    files[source] = entry
    save_manifest({"version": MANIFEST_VERSION, "files": files, "last_scan": manifest.get("last_scan")})


async def scan_and_apply(dirs: Sequence[Path] | None = None) -> Dict[str, Any]:
    """
    扫描 → 应用 → 记录本轮摘要（供 API / CLI / 后台任务共用）。

    用锁串行化：后台定时扫描与 API 手动触发可能同时发生，
    并发跑会各自基于陈旧清单决策，导致重复入库或互相删除。
    """
    if _scan_lock.locked():
        logger.info("已有扫描在进行中，本次跳过")
        return {"ok": False, "busy": True, "reason": "已有入库或扫描正在进行中", "added": [],
                "modified": [], "deleted": [], "failed": [], "has_changes": False}

    async with _scan_lock:
        started = time.perf_counter()
        plan = await asyncio.to_thread(scan, dirs)
        report = await apply_plan(plan)
        report["ok"] = not bool(report["failed"] or report["warnings"] or plan.conflicts or plan.skipped_overflow)
        report["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        report["finished_at"] = _now_iso()
        report["has_changes"] = plan.has_changes

        # 把本轮摘要也存进清单，这样重启后仍能回答「上次扫描是什么时候」
        manifest = load_manifest()
        save_manifest({**manifest, "last_scan": report})
        return report


def plan_to_json(plan: ScanPlan) -> Dict[str, Any]:
    """把扫描计划序列化成可返回给前端的结构（不含任何副作用）"""
    return {
        "has_changes": plan.has_changes,
        "scanned_files": plan.scanned_files,
        "unchanged": plan.unchanged,
        "added": [{"source": s, "path": str(p)} for s, p in plan.added],
        "modified": [{"source": s, "path": str(p)} for s, p in plan.modified],
        "deleted": [{"source": s} for s in plan.deleted],
        "conflicts": plan.conflicts,
        "skipped_overflow": plan.skipped_overflow,
        "warnings": plan.warnings,
    }


def status() -> Dict[str, Any]:
    """增量索引当前状态（供 /api/index/status 与启动日志使用）"""
    manifest = load_manifest()
    files: Dict[str, FileEntry] = manifest.get("files", {})
    return {
        "watch_enabled": bool(settings.INDEX_WATCH_ENABLED),
        "watch_dirs": [str(p) for p in settings.index_watch_dirs_list],
        "interval_seconds": settings.INDEX_WATCH_INTERVAL_SECONDS,
        "manifest_version": manifest.get("version", MANIFEST_VERSION),
        "manifest_updated_at": manifest.get("updated_at", ""),
        "tracked_files": len(files),
        "tracked_chunks": sum(e.chunks for e in files.values()),
        "last_scan": manifest.get("last_scan"),
        "busy": _scan_lock.locked(),
        "supported_extensions": sorted(SUPPORTED_SUFFIXES),
        "max_file_size_mb": settings.MAX_FILE_SIZE_MB,
        "es_enabled": bool(settings.ES_ENABLED),
    }


def documents() -> List[Dict[str, Any]]:
    """清单里登记的每一份文档（供设置 → 知识库列出「已经进去的东西」）"""
    files: Dict[str, FileEntry] = load_manifest().get("files", {})
    return [
        {
            "source": e.source,
            "path": e.path,
            "size": e.size,
            "chunks": e.chunks,
            "indexed_at": e.indexed_at,
            # 来源文件是否还在原处。不在也照常列出来——正因为文件已经没了，
            # 增量扫描不会再管它，用户唯一能让它从知识库里消失的出口就是这个页面。
            "on_disk": Path(e.path).is_file() if e.path else False,
            "keyword_indexed": e.keyword_indexed,
            "warnings": e.warnings,
            "error": e.error,
            "status": "error" if e.error else "warning" if e.warnings else "indexed" if e.chunks else "empty",
            "origin": "upload" if "/" not in e.source else "directory",
            "extension": Path(e.source).suffix.lower(),
        }
        for e in sorted(files.values(), key=lambda x: x.indexed_at, reverse=True)
    ]


async def remove_document(source: str) -> Dict[str, Any]:
    """
    把一份文档从两个存储和清单里一起删掉。

    删不到就不写清单，避免「清单说没了、库里还检索得到」——那正是
    「入库先删后建」这条不变量在防的状态。

    等 `_scan_lock`：正在跑的扫描会在结束时整表覆盖清单，交错必丢结果。
    """
    async with _scan_lock:
        manifest = load_manifest()
        if source not in manifest.get("files", {}):
            return {"ok": False, "not_found": True, "source": source, "reason": "知识库里没有这个来源"}
        try:
            counters = await _purge(source)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除来源 '%s' 失败: %s", source, e, exc_info=True)
            manifest["files"][source].error = f"删除未完成：{e}"
            save_manifest(manifest)
            return {"ok": False, "source": source, "reason": f"清理索引失败: {e}"}

        manifest = load_manifest()
        files: Dict[str, FileEntry] = manifest.get("files", {})
        known = source in files
        removed = counters["milvus"] + counters["es"]
        if not known and removed == 0:
            # 清单里没有、两个存储也没删到东西 —— 说明这个名字不存在，
            # 不能回一个「成功」，否则拼错的来源名看起来像已经删过了。
            return {"ok": False, "not_found": True, "source": source, "reason": "知识库里没有这个来源"}
        files.pop(source, None)
        save_manifest({
            "version": MANIFEST_VERSION,
            "files": files,
            "last_scan": manifest.get("last_scan"),
        })
        return {
            "ok": True,
            "source": source,
            "known": known,
            "milvus": counters["milvus"],
            "es": counters["es"],
        }


async def _invalidate_graph(source: str, result: IndexResult) -> None:
    if settings.GRAPH_ENABLED:
        from app.graph.store import graph_store

        try:
            await graph_store.delete_by_source(source, strict=True)
        except Exception as exc:
            logger.warning("旧图谱清理失败：%s", exc)
            result.warnings.append("关联图谱未完全更新，请重新构建图谱")


async def ingest_upload(staged_path: Path, source: str) -> Dict[str, Any]:
    """Serialize upload, replacement and manifest writes with scans and deletion."""
    async with _scan_lock:
        known = load_manifest().get("files", {})
        if os.name == "nt":
            source = next((name for name in known if "/" not in name and name.casefold() == source.casefold()), source)
        incoming = await asyncio.to_thread(fingerprint, staged_path, source)
        old = known.get(source)
        target = settings.upload_path / source
        if (old and old.chunks > 0 and old.sha256 == incoming.sha256 and not old.error
                and not old.warnings and (not settings.ES_ENABLED or old.keyword_indexed is True) and target.is_file()):
            await asyncio.to_thread(os.replace, staged_path, target)
            await _record_indexed(target, source, old.chunks, IndexResult(old.chunks, old.keyword_indexed))
            return {"chunks": old.chunks, "unchanged": True, "warnings": [], "keyword_indexed": old.keyword_indexed}
        try:
            result = await index_document(str(staged_path), source)
            await _invalidate_graph(source, result)
            await asyncio.to_thread(os.replace, staged_path, target)
            await _record_indexed(target, source, result.chunks, result)
        except Exception as exc:
            manifest = load_manifest()
            if source in manifest.get("files", {}):
                manifest["files"][source].error = f"更新未完成：{exc}"
                save_manifest(manifest)
            raise
        return {"chunks": result.chunks, "unchanged": False, "warnings": result.warnings,
                "keyword_indexed": result.keyword_indexed}


async def reindex_document(source: str) -> Dict[str, Any]:
    async with _scan_lock:
        manifest = load_manifest()
        old = manifest.get("files", {}).get(source)
        if old is None:
            raise KeyError(source)
        path = Path(old.path)
        if not path.is_file():
            raise FileNotFoundError("原文件已不在原处，请重新上传文档")
        if path.stat().st_size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise ValueError(f"文件超过 {settings.MAX_FILE_SIZE_MB} MB 上限")
        before = await asyncio.to_thread(fingerprint, path, source)
        try:
            result = await index_document(str(path), source)
            await _invalidate_graph(source, result)
            before.chunks = result.chunks
            before.indexed_at = _now_iso()
            before.keyword_indexed = result.keyword_indexed
            before.warnings = result.warnings
            manifest["files"][source] = before
            save_manifest(manifest)
        except Exception as exc:
            old.error = str(exc)
            manifest["files"][source] = old
            save_manifest(manifest)
            raise
        return {"ok": True, "source": source, "chunks": result.chunks, "warnings": result.warnings}


__all__ = [
    "MANIFEST_VERSION",
    "FileEntry",
    "ScanPlan",
    "derive_source",
    "fingerprint",
    "hash_file",
    "load_manifest",
    "save_manifest",
    "scan",
    "apply_plan",
    "scan_and_apply",
    "record_indexed",
    "plan_to_json",
    "documents",
    "remove_document",
    "status",
]
