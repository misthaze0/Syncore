"""
RRF（Reciprocal Rank Fusion，倒数排名融合）—— 合并多路召回结果

★ 为什么用 RRF 而不是「分数加权求和」

1. **不同检索器的分数量纲不可比**：向量的余弦相似度落在 [-1, 1]，
   BM25 是无上界的正数（本项目实测同一库内从 0.5 到 7+ 都有）。
   想把它们加权相加，必须先做归一化，而 min-max 归一化又极度依赖
   当前这批结果的分值分布 —— 换一条 query、库大小变了，权重就需要重调，
   本质上是「用一个不稳定的中间量去校准另一个不稳定的量」。

2. **RRF 只用排名，不用分数**：把每路的第 1 名记为 1、第 2 名记为 2……
   再按 `1 / (k + rank)` 累加。这样无论各路分数怎么分布，
   只要「谁排在前面」这个信息是可靠的，融合结果就稳定。

3. **k 的作用**：k 越大，各名次之间的权重差越小（越平权）。
   k=60 来自原论文的实证推荐值，也是 ES / Weaviate / Milvus 等
   主流实现的默认值。k 太小会让单路的第一名「一票否决」其他路的意见。

参考: Cormack et al., "Reciprocal Rank Fusion outperforms Condorcet and
individual Rank Learning Methods" (SIGIR 2009)

⚠️ 已知局限：RRF 天然丢掉了「分差」信息。
   如果向量路第一名的相似度是 0.98、第二名 0.40，RRF 只看到「1 和 2」。
   这正是需要在融合之后再接一层 Rerank 精排的原因 ——
   精排用 Cross-Encoder 直接对 (query, doc) 联合编码，重新给出可信的绝对分。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

logger = logging.getLogger(__name__)


def hit_identity(hit: Dict[str, Any]) -> str:
    """
    为一条召回结果生成跨路可比的唯一标识（RRF 去重的依据）。

    优先用 `doc_id`（与 Milvus 主键同源）；
    没有时退回 `source + chunk_index`，保证在只有其中一路结果时也能工作。
    """
    doc_id = hit.get("doc_id")
    if doc_id is not None:
        return f"id:{doc_id}"
    return f"src:{hit.get('source', '')}::{hit.get('chunk_index', '')}"


def rrf_fuse(
    result_lists: Dict[str, Sequence[Dict[str, Any]]],
    *,
    k: int = 60,
    weights: Dict[str, float] | None = None,
    top_k: int | None = None,
) -> List[Dict[str, Any]]:
    """
    RRF 融合多路召回。

    Args:
        result_lists: {路名: 有序结果列表}，例如
                      {"vector": [...], "bm25": [...]}
                      列表顺序即排名（第 0 项视为第 1 名）
        k: RRF 平滑常数，默认 60
        weights: {路名: 权重}，缺省为 1.0；加权后 score = Σ w_i/(k+rank_i)
        top_k: 截断条数，None 表示全部返回

    Returns:
        融合后的结果列表（按 rrf_score 降序），每条在原字段基础上新增:
        - `rrf_score`: 融合分
        - `recalled_by`: 命中的路名列表，如 ["vector", "bm25"]
        - `retrieval`: "hybrid"（被多路命中）或该路名（单路命中）
        - `{路名}_rank` / `{路名}_score`: 各路中的排名与原始分，便于可观测与调试
    """
    weights = weights or {}
    merged: Dict[str, Dict[str, Any]] = {}
    # 路的顺序决定 rank 的稳定输出顺序（Python dict 保序）
    ordered_lanes = list(result_lists.keys())

    for lane in ordered_lanes:
        hits = result_lists.get(lane) or []
        weight = float(weights.get(lane, 1.0))
        for rank, hit in enumerate(hits, start=1):
            key = hit_identity(hit)
            entry = merged.get(key)
            if entry is None:
                # 首次见到该文档：以它的字段为基准（文本/来源等元信息在几路里应一致）
                entry = {**hit}
                entry["rrf_score"] = 0.0
                entry["recalled_by"] = []
                merged[key] = entry

            entry["rrf_score"] += weight / (k + rank)
            if lane not in entry["recalled_by"]:
                entry["recalled_by"].append(lane)

            entry[f"{lane}_rank"] = rank
            entry[f"{lane}_score"] = float(hit.get("score", 0.0) or 0.0)

    fused = list(merged.values())

    for entry in fused:
        lanes = entry.get("recalled_by") or []
        # 多路命中说明语义与关键词都认为它相关，是更强的信号；
        # 单路命中则保留该路名，前端据此展示「这条是靠什么召回的」
        entry["retrieval"] = "hybrid" if len(lanes) > 1 else (lanes[0] if lanes else "unknown")
        entry["rrf_score"] = round(float(entry["rrf_score"]), 8)

    fused.sort(key=lambda x: (-x["rrf_score"], str(hit_identity(x))))

    if top_k is not None and top_k > 0:
        fused = fused[:top_k]

    logger.debug(
        "RRF 融合完成: %s",
        {lane: len(result_lists.get(lane) or []) for lane in ordered_lanes},
    )
    return fused


def explain_fusion(fused: List[Dict[str, Any]], k: int = 60) -> List[str]:
    """
    生成人类可读的融合解释（调试 / 演示用，便于讲清楚每条的排名来源）。
    """
    lines: List[str] = [f"RRF(k={k}) 融合结果 Top{len(fused)}:"]
    for i, hit in enumerate(fused, start=1):
        lanes = hit.get("recalled_by") or []
        detail = " ".join(
            f"{lane}#{hit.get(f'{lane}_rank')}" for lane in lanes
        )
        lines.append(
            f"  {i}. rrf={hit.get('rrf_score'):.6f} [{detail}] "
            f"{str(hit.get('text', ''))[:40]!r}"
        )
    return lines


__all__ = ["rrf_fuse", "hit_identity", "explain_fusion"]
