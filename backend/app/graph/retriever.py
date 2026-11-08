"""
多跳图检索 —— 把「实体关系图」变成第三路召回

★ 它解决的是另外两路结构上做不到的事

向量路与 BM25 路都是**单跳**的：它们只看"这一块文档与查询像不像"。

    查询「星图模块超时多久算失败」
      ├─ BM25：找同时含有"星图/模块/超时/失败"的块 → 答句所在的块
      │        可能一个查询词都没有（它写的是"代号 ZX-77 的构建时限"）
      └─ 向量：找语义最像的块 → 通常命中"讲星图模块"的那一块
               但那一块里根本没有答案

真正的答案需要**两跳**：查询 →(星图模块)→ 代号 ZX-77 →(构建时限)→ 答案块。
第二跳的落点与查询**零字面重合**，所以单跳检索无论怎么调参都够不到它。
这正是引入图结构唯一不可替代的价值，也是本模块存在的全部理由。

★ 为什么查询期用确定性匹配而不是模型

"查询里出现了哪些实体"是一个**可以确定性回答**的问题：图里已经有一份实体名清单，
拿查询去匹配它就行（子串匹配 + ASCII 词边界）。用模型做这件事会带来三个损失：
- 每轮问答多一次往返（且结果会抖动）
- 新增一种"模型没认出来 → 图路空手而归"的静默失败
- 无法单测

—— 与「显式记忆走正则而不是模型」「BM25 的价值是精确匹配的确定性保证」是同一种判断。

★ 打分为什么用衰减而不是简单的 hop 加权计数

`1/(h+1)` 让"直接提到查询实体的块"永远排在"两跳之外才够到的块"前面。
这很重要：多跳的价值是**补充**，不是**覆盖**。如果让远端的块与近端同权，
一个连线密集的热门实体会把它的所有邻居块都顶上来，把真正直接相关的块挤掉 ——
症状是"图检索一开，简单问题的答案反而排到后面去了"。

★ 共现加成解决什么问题

「同一块里同时出现了多个查询命中实体」是极强的相关性信号：
用户问"A 和 B 的关系"，那么同时提到 A、B 的那一块几乎一定是答案所在。
图结构本身表达不了这件事（它是两个独立节点），但它可以从 MENTIONED_IN 读出来。

★ 怎么保证可单测

`traverse()` 不直接访问 Neo4j —— 取邻居的函数由调用方注入（`fetch_neighbors`）。
单测里传一个内存图的取数函数，就能把跳数、去重、衰减、共现加成全部穷举，
一行网络代码都不用碰。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence

from app.config import settings
from app.graph.models import GraphChunk
from app.graph.store import graph_store

logger = logging.getLogger(__name__)

#: 取邻居的函数签名：(实体键列表) -> 边记录列表
NeighborFetcher = Callable[[Sequence[str]], Awaitable[List[Dict[str, Any]]]]

#: 共现加成的强度：块里每多一个命中实体，分数乘以 (1 + BOOST)
COOCCURRENCE_BOOST = 0.5

#: ASCII 实体做子串匹配时的边界断言用一个哨兵字符占位（见 match_entities）
_ASCII_TOKEN_CHARS = "a-z0-9_"


@dataclass
class TraversalResult:
    """一次多跳展开的结果（全部是 chunk 级结论）"""

    #: ref(`source#index`) → 累计分数
    scores: Dict[str, float] = field(default_factory=dict)
    #: ref → 元信息（source / chunk_index / 最小跳数 / 实体路径 / 关系链）
    meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: 命中的种子实体（归一化键 → 展示名）
    seeds: Dict[str, str] = field(default_factory=dict)
    #: 每一跳实际到达的实体数（用于观测“图有没有连起来”）
    reached_by_hop: List[int] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.scores


# ---------- 查询期实体匹配（纯函数） ----------
def match_entities(
    query: str,
    index: Dict[str, str],
    *,
    min_chars: int = 2,
) -> Dict[str, str]:
    """
    在查询里找出图中已知的实体（确定性匹配，零模型调用）。

    两条判据合并：
    1. **子串匹配**：实体名整体出现在查询里。这是中文场景的主力 ——
       「星图模块超时多久算失败」与实体「星图模块」是包含关系，
       但分词后未必得到一个完全相同的词元。
    2. 结果按**长度倒序**保留：长实体优先，避免「星图」把「星图模块」的命中
       吃掉（两者都会命中，都应该进种子，但如果只保留一个必须留长的）。

    ⚠️ ASCII 实体必须检查词边界。否则实体 `rag` 会在 `storage` 里命中，
    实体 `port` 会在 `report` 里命中 —— 这类误命中不会报错，
    只会让图路莫名其妙地召回一批不相干的块。
    """
    if not query or not index:
        return {}

    lowered = query.lower()
    matched: Dict[str, str] = {}

    for key, name in index.items():
        if not key or len(key) < max(1, min_chars):
            continue
        if not _contains_entity(lowered, key):
            continue
        matched[key] = name

    # 长实体优先（dict 保序，后面排序会稳定）
    return dict(sorted(matched.items(), key=lambda kv: -len(kv[0])))


def _contains_entity(lowered_query: str, key: str) -> bool:
    """
    判断实体键是否作为一个**独立片段**出现在查询里。

    纯中文键：直接子串匹配（中文没有词边界的概念，切分了反而会漏）。
    含 ASCII 的键：要求两侧不是 `[a-z0-9_]`，即真正的词边界。
    """
    if not key:
        return False
    if not key.isascii():
        return key in lowered_query

    pattern = rf"(?<![{_ASCII_TOKEN_CHARS}]){re.escape(key)}(?![{_ASCII_TOKEN_CHARS}])"
    return re.search(pattern, lowered_query) is not None


# ---------- 多跳遍历（纯逻辑，取数由调用方注入） ----------
def hop_weight(hop: int) -> float:
    """跳数衰减：0 → 1.0，1 → 0.5，2 → 0.333…"""
    return 1.0 / (hop + 1)


async def traverse(
    seed_keys: Sequence[str],
    *,
    fetch_neighbors: NeighborFetcher,
    max_hops: int = 2,
    seed_names: Optional[Dict[str, str]] = None,
) -> TraversalResult:
    """
    从种子实体出发做 BFS，把每一条边投影回**它来自的那一块文档**。

    Args:
        seed_keys: 查询匹配到的实体键
        fetch_neighbors: 取一跳邻居的异步函数（生产环境传 graph_store.neighbors）
        max_hops: 最大展开跳数（1 = 只看直接邻居）
        seed_names: 实体键 → 展示名，用于拼可读的路径

    Returns:
        TraversalResult。种子块（hop=0）也计入 —— 它们与查询直接相关，
        是图路对"简单问题"的贡献部分。
    """
    result = TraversalResult(seeds=dict(seed_names or {}))
    if not seed_keys:
        return result

    visited: Dict[str, int] = {key: 0 for key in seed_keys}
    frontier: List[str] = list(seed_keys)
    #: 实体键 → 从查询走过来的实体链（用于展示“这条来源是顺着哪条关系找来的”）
    paths: Dict[str, List[str]] = {key: [result.seeds.get(key, key)] for key in seed_keys}

    for hop in range(1, max(1, max_hops) + 1):
        if not frontier:
            break
        edges = await fetch_neighbors(frontier)
        next_frontier: List[str] = []
        for edge in edges or []:
            if not isinstance(edge, dict):
                continue
            a_key = str(edge.get("a_key") or "")
            b_key = str(edge.get("b_key") or "")
            source = str(edge.get("source") or "")
            if not source or not a_key or not b_key:
                continue

            weight = hop_weight(hop) * _confidence(edge.get("confidence"))
            rel = str(edge.get("relation") or "")
            parent_path = paths.get(a_key) or [a_key]
            child_name = str(edge.get("b_name") or b_key)
            child_path = [*parent_path, child_name]

            # 这条边的 provenance 块（a 侧与 b 侧都算 —— 边的两端共同提供了这个事实）
            _add_chunk(result, source, edge.get("chunk_index"), weight,
                       hop=hop, path=child_path, relations=_relations_for(parent_path, rel))

            if b_key not in visited:
                visited[b_key] = hop
                paths[b_key] = child_path
                next_frontier.append(b_key)
            elif len(child_path) < len(paths.get(b_key) or []):
                # 找到更短的路径就替换：界面展示应当给最短解释
                paths[b_key] = child_path

        result.reached_by_hop.append(len(next_frontier))
        frontier = next_frontier

    return result


def _relations_for(parent_path: List[str], relation: str) -> List[str]:
    """
    让关系链长度与实体链对齐。

    路径是 [e0, e1, ..., en]（n 个实体），关系是 n 条边。走到第 hop 跳时
    已经确定了 hop 条关系。这里补齐占位，保证前端可以成对渲染 e_i —r_i→ e_{i+1}。
    """
    need = max(0, len(parent_path) - 1) + 1
    return [""] * (need - 1) + [relation]


def _confidence(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return max(0.05, min(1.0, value))


def _add_chunk(
    result: TraversalResult,
    source: str,
    chunk_index: Any,
    weight: float,
    *,
    hop: int,
    path: List[str],
    relations: List[str],
) -> None:
    """累加一块文档的图路得分（同一块被多条边支持时分数叠加）"""
    try:
        index = int(chunk_index)
    except (TypeError, ValueError):
        return
    ref = f"{source}#{index}"

    result.scores[ref] = result.scores.get(ref, 0.0) + weight
    existing = result.meta.get(ref)
    if existing is None or hop < existing.get("hops", 99):
        result.meta[ref] = {
            "source": source,
            "chunk_index": index,
            "hops": hop,
            "path": list(path),
            "relations": list(relations),
        }


def apply_cooccurrence_boost(
    result: TraversalResult,
    mentions: Iterable[Dict[str, Any]],
) -> None:
    """
    共现加成：同一块里同时出现多个**种子实体**时，把它的分数抬上去。

    `mentions` 是 `{source, chunk_index, entity_keys: [...]}` 的记录，
    由 store 的 `chunks_of_entities` 提供。

    只统计种子实体（不含二跳实体）：二跳实体是图引进来的，用它们做共现
    会形成正反馈 —— 连线密集的实体越连越强，与"衰减"的设计意图相反。
    """
    seed_keys = set(result.seeds)
    for row in mentions or []:
        if not isinstance(row, dict):
            continue
        keys = {str(k) for k in (row.get("entity_keys") or [])} & seed_keys
        if len(keys) < 2:
            continue
        ref = f"{row.get('source', '')}#{row.get('chunk_index', 0)}"
        if ref not in result.scores:
            continue
        result.scores[ref] *= 1.0 + COOCCURRENCE_BOOST * (len(keys) - 1)


# ---------- 排序与投影 ----------
def rank_chunks(result: TraversalResult, top_k: int) -> List[GraphChunk]:
    """把遍历结果转成有序的 GraphChunk 列表（尚未回填正文）"""
    if result.empty:
        return []

    ordered = sorted(result.scores.items(), key=lambda kv: (-kv[1], kv[0]))
    out: List[GraphChunk] = []
    for ref, score in ordered[: max(0, top_k)]:
        info = result.meta.get(ref) or {}
        source, _, index = ref.rpartition("#")
        out.append(GraphChunk(
            source=info.get("source", source),
            chunk_index=int(info.get("chunk_index", index or 0) or 0),
            score=round(float(score), 6),
            hops=int(info.get("hops", 0)),
            path=list(info.get("path") or []),
            via_relations=[r for r in (info.get("relations") or []) if r],
        ))
    return out


# ---------- 对外入口（生产链路） ----------
async def retrieve_graph(query: str, top_k: int = 20) -> List[Dict[str, Any]]:
    """
    图路召回（多跳）。

    降级行为：未启用 / Neo4j 不可用 / 图是空的 / 查询里没有已知实体
    —— 四种情况都返回空列表，由 `retrieve_hybrid` 自然退化为两路融合。
    **任何异常都不向上抛**。

    Returns:
        与 `retrieve_vector` / `retrieve_bm25` 同构的 hit 列表，
        额外带 `graph_hops` / `graph_path` / `graph_relations` 供前端展示推理路径。
    """
    if not graph_store.enabled:
        return []

    try:
        index = await graph_store.entity_index()
        if not index:
            return []

        seeds = match_entities(
            query, index, min_chars=settings.GRAPH_MIN_ENTITY_CHARS
        )
        if not seeds:
            return []

        result = await traverse(
            list(seeds),
            fetch_neighbors=graph_store.neighbors,
            max_hops=settings.GRAPH_MAX_HOPS,
            seed_names=seeds,
        )
        # 种子实体直接提到的块（hop=0）：它们是与查询最直接相关的那部分
        mentions = await graph_store.chunks_of_entities(list(seeds))
        for row in mentions:
            _add_chunk(
                result,
                str(row.get("source", "")),
                row.get("chunk_index"),
                hop_weight(0) * len(set(row.get("entity_keys") or [])),
                hop=0,
                path=[seeds.get(k, k) for k in (row.get("entity_keys") or [])],
                relations=[],
            )
        apply_cooccurrence_boost(result, mentions)

        chunks = rank_chunks(result, top_k)
        if not chunks:
            return []

        await hydrate(chunks)
        hits = [c.to_hit() for c in chunks if c.text]
        logger.info(
            "图路召回完成: query=%r | 种子实体=%s | 返回=%s 条 | 跳数分布=%s",
            query[:30], list(seeds.values())[:4], len(hits),
            [c.hops for c in chunks][:8],
        )
        return hits
    except Exception as e:  # noqa: BLE001
        logger.warning("图路召回失败（降级为两路融合）: %s", e)
        return []


async def hydrate(chunks: Sequence[GraphChunk]) -> None:
    """
    回填正文。

    ★ 为什么正文不在图里存一份

    图上只存"实体—关系—来源块引用"，正文只有一个权威来源（Milvus / ES）。
    在图里复制一份正文会立刻引入孤儿块问题：文档改了，图里的副本还是旧的，
    而它照样会被召回、被注入 Prompt —— 与不变量 24 描述的症状一模一样。
    所以这里按确定性主键 `chunk_id(source, chunk_index)` 回查正文。

    两级回退：Milvus → ES。都拿不到时该块**被丢弃**（没有正文的召回结果
    对下游毫无价值，塞进去只会让精排拿到空字符串）。
    """
    if not chunks:
        return
    from app.core.doc_id import chunk_id

    by_id = {chunk_id(c.source, c.chunk_index): c for c in chunks}
    filled: set[int] = set()

    try:
        from app.core.milvus_client import milvus_manager

        rows = await milvus_manager.get_by_ids(list(by_id))
        for row in rows or []:
            doc_id = row.get("doc_id")
            target = by_id.get(doc_id if isinstance(doc_id, int) else _as_int(doc_id))
            if target is None:
                continue
            target.text = str(row.get("text", ""))
            target.page = int(row.get("page", 0) or 0)
            filled.add(id(target))
    except Exception as e:  # noqa: BLE001
        logger.debug("从 Milvus 回填图召回的正文失败，尝试 ES: %s", e)

    missing = [c for c in chunks if id(c) not in filled]
    if not missing:
        return

    try:
        from app.rag.es_client import es_manager

        rows = await es_manager.mget_by_keys(
            [(c.source, c.chunk_index) for c in missing]
        )
        for row in rows or []:
            target = by_id.get(_as_int(row.get("doc_id")))
            if target is None:
                continue
            target.text = str(row.get("text", ""))
            target.page = int(row.get("page", 0) or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("从 ES 回填图召回的正文失败（该块将被丢弃）: %s", e)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


__all__ = [
    "COOCCURRENCE_BOOST",
    "TraversalResult",
    "apply_cooccurrence_boost",
    "hop_weight",
    "hydrate",
    "match_entities",
    "rank_chunks",
    "retrieve_graph",
    "traverse",
]
