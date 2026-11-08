"""
GraphRAG —— 实体关系图 + 多跳检索（第三路召回）

★ 它在整条检索链路里的位置

    查询
      ├─ 向量召回（Milvus）      语义相近
      ├─ 关键词召回（ES/BM25）   字面精确
      └─ 图召回（Neo4j）         ★ 关系可达（多跳）
                ↓
           RRF 融合（三路只看名次，量纲不可比的问题天然免疫）
                ↓
           Cross-Encoder 精排 → Top-N → 注入 Prompt

三路**都产出 chunk 级结果**，因此可以直接丢进同一个 `rrf_fuse`。
这是本模块最重要的接口约定，详见 `models.py`。

★ 分层

    models.py     实体 / 三元组 / 图召回块的数据模型（纯数据）
    extractor.py  LLM 抽取三元组 + 可穷举的纯解析函数
    store.py      Neo4j 连接、写删查、降级（唯一碰图数据库的地方）
    retriever.py  查询实体匹配 + 多跳 BFS + 投影回 chunk（遍历逻辑可脱机单测）
    builder.py    离线构建（复用入库管道的分块，保证 provenance 对齐）

★ 对外只暴露三件事

    graph_store                     能力状态与图写入/查询
    retrieve_graph()                图路召回（供 rag/retriever 调用）
    build_from_*()                  构建入口（CLI / API 调用）

★ 降级边界

Neo4j 是**可选增益**：没启用、连不上、图是空的、查询里没有已知实体 ——
四种情况 `retrieve_graph()` 都返回空列表，混合检索自然退化为两路。
**任何异常都不会向上抛**，与 Milvus / ES / Redis 的容错策略完全一致。
"""

from __future__ import annotations

from app.graph.builder import (
    BuildReport,
    build_from_chunks,
    build_from_dirs,
    build_from_paths,
    build_from_triples,
    chunks_from_paths,
)
from app.graph.extractor import extract_many, extract_triples, parse_triples
from app.graph.models import (
    ENTITY_TYPES,
    GraphChunk,
    GraphStats,
    Triple,
    display_entity,
    normalize_entity,
    normalize_relation,
)
from app.graph.retriever import (
    match_entities,
    retrieve_graph,
    traverse,
)
from app.graph.store import GraphStore, graph_store

__all__ = [
    "ENTITY_TYPES",
    "BuildReport",
    "GraphChunk",
    "GraphStats",
    "GraphStore",
    "Triple",
    "build_from_chunks",
    "build_from_dirs",
    "build_from_paths",
    "build_from_triples",
    "chunks_from_paths",
    "display_entity",
    "extract_many",
    "extract_triples",
    "graph_store",
    "match_entities",
    "normalize_entity",
    "normalize_relation",
    "parse_triples",
    "retrieve_graph",
    "traverse",
]
