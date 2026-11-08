"""
图谱构建 —— 把一批文档块变成图（离线批处理）

★ 分块必须复用入库管道的那一套（这是本模块最容易写错的地方）

图路要能参与 RRF，靠的是它投影回的 `source#chunk_index` 与 Milvus / ES 里的
块**一一对应**。如果构建图时自己写一遍分块逻辑（哪怕参数只是差了一点），
产出的 chunk_index 就会整体错位：

- 图路召回 `报告.md#3`，去 Milvus 回填正文时拿到的是**另一块**的内容；
- 而且它不报任何错 —— 检索结果看起来正常，只是答非所问。

所以这里不自己分块，直接调 `parser.parse_file` + `chunker.split_text`
（与 `indexer.index_file` 完全同一条路径）。**分块规则的唯一来源是 chunker**。

★ 两条构建路径

| 路径 | 用在哪 | 是否打模型 |
| :--- | :--- | :--- |
| `build_from_chunks()` | 生产：读知识库目录 → 抽取 | 是（每块一次） |
| `build_from_triples()` | 评估探针：把 Golden Set 声明的关系写进图 | 否 |

评估探针刻意**不走抽取**：评估要衡量的是"多跳检索与 RRF 融合带来的召回增益"，
如果探针图也由模型现抽，那么抽取质量就成了一个混淆变量 ——
指标掉了你分不清是检索改坏了还是模型这次抽得差。所以由 Golden Set
声明关系（`GoldenSet.triples`），把变量锁死。抽取本身的能力由
`scripts/verify_graphrag.py` 单独验收。

★ 重建前必须先清空间

`MERGE` 是幂等的，但**只对"关系集合不变"的情况幂等**。文档改了、关系变了，
旧关系不会自动消失（主键里含 source + chunk_index，新关系是新主键）。
所以 `clear=True` 时先清空整个 space，与增量索引「先按来源删干净再入库」同源。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.config import settings
from app.graph.extractor import extract_many
from app.graph.models import Triple
from app.graph.store import graph_store

logger = logging.getLogger(__name__)


@dataclass
class BuildReport:
    """一次构建的结果摘要（供 CLI / API / 验收脚本共用）"""

    space: str = ""
    chunks: int = 0
    triples: int = 0
    written: int = 0
    empty_chunks: int = 0
    cleared: int = 0
    duration_ms: float = 0.0
    failed: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "space": self.space,
            "chunks": self.chunks,
            "triples": self.triples,
            "written": self.written,
            "empty_chunks": self.empty_chunks,
            "cleared": self.cleared,
            "duration_ms": self.duration_ms,
            "failed": self.failed[:20],
            "error": self.error,
        }


async def build_from_triples(
    triples: Sequence[Triple],
    *,
    clear: bool = False,
) -> BuildReport:
    """
    直接写入已确定的三元组（不打模型）。评估探针与单测走这条路。

    `clear=True` 会先清空当前 space —— 评估每次跑批都要干净的图，
    否则上一轮的语料会残留，污染多跳召回结果。
    """
    report = BuildReport(space=graph_store.space)
    started = time.perf_counter()
    try:
        if clear:
            report.cleared = await graph_store.clear_space()
        report.triples = len(triples)
        report.written = await graph_store.upsert_triples(triples)
    except Exception as e:  # noqa: BLE001
        report.error = f"{type(e).__name__}: {e}"
        logger.warning("图谱构建失败（triples 直写）: %s", e)
    report.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    return report


async def build_from_chunks(
    chunks: Sequence[Dict[str, Any]],
    *,
    clear: bool = False,
    concurrency: Optional[int] = None,
) -> BuildReport:
    """
    从文档块构建图：逐块抽取三元组 → 写入 Neo4j。

    Args:
        chunks: [{"text", "source", "chunk_index"}, ...]，**chunk_index 必须与
                入库管道一致**（见模块头注释）
        clear: 是否先清空 space
    """
    report = BuildReport(space=graph_store.space, chunks=len(chunks))
    started = time.perf_counter()
    try:
        if clear:
            report.cleared = await graph_store.clear_space()

        usable = [c for c in chunks if str(c.get("text", "")).strip()]
        if not usable:
            report.error = "没有可抽取的文档块"
            return report

        triples = await extract_many(
            usable,
            concurrency=concurrency or settings.GRAPH_BUILD_CONCURRENCY,
        )
        report.triples = len(triples)
        report.empty_chunks = max(0, len(usable) - len({t.chunk_ref for t in triples}))
        report.written = await graph_store.upsert_triples(triples)
    except Exception as e:  # noqa: BLE001
        report.error = f"{type(e).__name__}: {e}"
        logger.warning("图谱构建失败（chunks 抽取）: %s", e)
    report.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    return report


async def chunks_from_paths(paths: Iterable[Any]) -> List[Dict[str, Any]]:
    """
    把文件解析 + 分块成与入库管道一致的 chunks。

    ⚠️ 这里调的是 `indexer` 用的那两个函数，不是自己实现的分块。
    """
    from pathlib import Path

    from app.rag.chunker import split_text
    from app.rag.parser import parse_file

    out: List[Dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        try:
            text = await parse_file(str(path), path.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("图谱构建：解析 %s 失败，已跳过: %s", path.name, e)
            continue
        if not text or not text.strip():
            continue
        for index, piece in enumerate(split_text(text)):
            out.append({"text": piece, "source": path.name, "chunk_index": index})
    return out


async def build_from_paths(
    paths: Sequence[Any],
    *,
    clear: bool = False,
    concurrency: Optional[int] = None,
) -> BuildReport:
    """从一组文件路径构建图（解析 → 分块 → 抽取 → 入库）"""
    chunks = await chunks_from_paths(paths)
    return await build_from_chunks(chunks, clear=clear, concurrency=concurrency)


async def build_from_dirs(
    dirs: Optional[Sequence[Any]] = None,
    *,
    clear: bool = False,
    concurrency: Optional[int] = None,
) -> BuildReport:
    """
    从知识库目录构建图（默认取 `INDEX_WATCH_DIRS`）。

    与增量索引共用同一批目录与同一套白名单，保证"能被索引进 Milvus 的文件"
    与"能进图的文件"是同一批 —— 否则会出现"检索得到、图里没有"的诡异不一致。
    """
    from pathlib import Path

    from app.rag.parser import SUPPORTED_SUFFIXES

    roots = [Path(p) for p in (dirs if dirs is not None else settings.index_watch_dirs_list)]
    files: List[Path] = []
    for root in roots:
        if not root.is_dir():
            logger.warning("图谱构建目录不存在，已跳过: %s", root)
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if path.name.startswith(".") or path.name.endswith(("~", ".tmp", ".swp")):
                continue
            files.append(path)

    logger.info("图谱构建：目录内共 %s 个文件待抽取", len(files))
    return await build_from_paths(files, clear=clear, concurrency=concurrency)


__all__ = [
    "BuildReport",
    "build_from_chunks",
    "build_from_dirs",
    "build_from_paths",
    "build_from_triples",
    "chunks_from_paths",
]
