"""
检索指标 —— 把「感觉变好了」变成「Recall@3 从 33/34 变成 34/34」

★ 指标选择与各自的盲区

| 指标        | 回答的问题                 | 盲区                             |
|-------------|----------------------------|----------------------------------|
| Recall@k    | 该找到的**找到了吗**        | 不关心排序，第 1 位和第 k 位等价   |
| Top-1 / MRR | **排序质量**如何            | 只奖励第一个正确项，不看后续       |
| nDCG@k      | 排序好坏（位置加权）         | 需要相关性分级；本集是二值，退化为位置折扣 |
| Precision@k | 塞进来的有多少是**有用**的   | 语料里正确答案只有 1 条时天然很低  |

所以**没有单一指标能代表检索质量**，必须一起看：
只看 Recall 会漏掉「找到了但排在最后一位」这种劣化，
只看 MRR 会漏掉「第 1 位很准但第 2~3 位全是噪声」这种劣化。

★ 为什么按 kind 分组统计

这是本项目最重要的一条统计口径。全集的平均召回率会把两类场景混在一起，
从而**掩盖真正的变化方向**：
BM25 在语义场景必然失手，向量在低信噪比标识符场景可能失手，
两者一混，「总召回率」的涨跌完全无法归因到具体改动上。
因此本模块所有汇总都强制按 `kind` 拆开 —— 这也是 Phase 3 对照实验
能得出「两路各自主责场景 100%」这个结论的前提。

★ 关于 k 的选择

生产环境 `RERANK_TOP_N=5`，所以 k 不应超过 5 —— 评估一个「用户根本看不到的位置」
是否命中没有意义。默认 k=3（对齐 Phase 3 的基线，保证可比性），可配置。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.eval.golden import make_doc_ref, parse_doc_ref

DEFAULT_K = 3


# ---------- 相关性判定 ----------
def hit_ref(hit: Dict[str, Any]) -> str:
    """把一条检索结果转成与 `expected` 同构的引用字符串"""
    source = str(hit.get("source", ""))
    index = hit.get("chunk_index")
    if index is None:
        return source
    try:
        return make_doc_ref(source, int(index))
    except (TypeError, ValueError):
        return source


def build_expected_sets(expected: Sequence[str]) -> Tuple[Set[str], Set[str]]:
    """
    期望集合拆成两层：
    - `refs`    精确到块（`源文件#序号`）
    - `sources` 只到文件（标注写成 `源文件` 时，该文件任意块都算命中）

    支持「只到文件」这一层是必要的：有些问题的正确答案散布在同一文件的多个块里，
    强行标注某一个块会让标注本身成为误差来源。
    """
    refs: Set[str] = set()
    sources: Set[str] = set()
    for item in expected:
        source, index = parse_doc_ref(item)
        if index is None:
            sources.add(source)
        else:
            refs.add(item)
    return refs, sources


def is_relevant(hit: Dict[str, Any], refs: Set[str], sources: Set[str]) -> bool:
    ref = hit_ref(hit)
    if ref in refs:
        return True
    source, _, _ = ref.rpartition("#")
    return source in sources


def first_relevant_rank(
    hits: Sequence[Dict[str, Any]],
    expected: Sequence[str],
    *,
    k: Optional[int] = None,
) -> int:
    """
    第一个相关文档的名次（从 1 开始）；没有则返回 0。

    所有指标的公共底座 —— 只算一次名次，其余指标都由它派生，
    避免「不同指标对同一条结果给出矛盾判断」这种最难查的一类 bug。
    """
    if not hits or not expected:
        return 0
    refs, sources = build_expected_sets(expected)
    limit = len(hits) if k is None else min(k, len(hits))
    for rank, hit in enumerate(hits[:limit], start=1):
        if is_relevant(hit, refs, sources):
            return rank
    return 0


# ---------- 单条指标 ----------
def recall_at_k(hits: Sequence[Dict[str, Any]], expected: Sequence[str], k: int = DEFAULT_K) -> bool:
    return first_relevant_rank(hits, expected, k=k) > 0


def reciprocal_rank(
    hits: Sequence[Dict[str, Any]], expected: Sequence[str], k: Optional[int] = None
) -> float:
    rank = first_relevant_rank(hits, expected, k=k)
    return 1.0 / rank if rank > 0 else 0.0


def precision_at_k(
    hits: Sequence[Dict[str, Any]], expected: Sequence[str], k: int = DEFAULT_K
) -> float:
    """前 k 条里相关项的比例。分母固定为 k（而不是 len(hits)），
    这样「召回条数不足」也会被如实计入惩罚，而不是被虚高的比例掩盖。"""
    if k <= 0 or not hits or not expected:
        return 0.0
    refs, sources = build_expected_sets(expected)
    top = hits[:k]
    relevant = sum(1 for hit in top if is_relevant(hit, refs, sources))
    return relevant / float(k)


def ndcg_at_k(
    hits: Sequence[Dict[str, Any]], expected: Sequence[str], k: int = DEFAULT_K
) -> float:
    """
    nDCG@k（二值相关性）。

    IDCG 的分母用 `min(len(expected), k)`：期望项多于 k 时，
    不可能全部排进前 k，用 len(expected) 做分母会永远拿不到高分，
    等于把「指标设计缺陷」记在检索头上。
    """
    if k <= 0 or not hits or not expected:
        return 0.0
    refs, sources = build_expected_sets(expected)

    dcg = 0.0
    for rank, hit in enumerate(hits[:k], start=1):
        if is_relevant(hit, refs, sources):
            dcg += 1.0 / math.log2(rank + 1)

    ideal_hits = min(len(expected), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


# ---------- 单条查询的结果 ----------
@dataclass
class QueryOutcome:
    """一条查询在某一个检索阶段下的完整结果（含归因所需的中间信息）"""

    item_id: str
    query: str
    kind: str
    note: str = ""
    expected: List[str] = field(default_factory=list)

    # ---------- 指标 ----------
    rank: int = 0                 # 第一个相关项的名次，0 = 未命中
    mrr: float = 0.0
    ndcg: float = 0.0
    precision: float = 0.0
    n_hits: int = 0

    # ---------- 归因用的阶段信息（由 runner 填充） ----------
    #: 该条是否出现在各召回路的候选里（用于区分「没召回」与「召回了没排上」）
    in_vector: bool = False
    in_bm25: bool = False
    in_graph: bool = False
    in_fused: bool = False
    in_final: bool = False
    vector_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    graph_rank: Optional[int] = None
    final_rank: Optional[int] = None

    # ---------- 性能 ----------
    duration_ms: float = 0.0
    error: Optional[str] = None

    @property
    def hit(self) -> bool:
        return self.rank > 0

    @property
    def top1(self) -> bool:
        return self.rank == 1


def evaluate_query(
    item: Any,
    hits: Sequence[Dict[str, Any]],
    *,
    k: int = DEFAULT_K,
    duration_ms: float = 0.0,
) -> QueryOutcome:
    """对一条查询算全部指标。`item` 需含 id / query / kind / expected / note。"""
    expected = list(getattr(item, "expected", []) or [])
    rank = first_relevant_rank(hits, expected, k=k)

    outcome = QueryOutcome(
        item_id=getattr(item, "id", ""),
        query=getattr(item, "query", ""),
        kind=getattr(item, "kind", ""),
        note=getattr(item, "note", ""),
        expected=expected,
        rank=rank,
        mrr=reciprocal_rank(hits, expected),
        ndcg=ndcg_at_k(hits, expected, k=k),
        precision=precision_at_k(hits, expected, k=k),
        n_hits=len(hits),
        duration_ms=round(duration_ms, 2),
    )

    # 阶段信息：直接从命中结果里读（检索结果自带 recalled_by 与原始名次）
    refs, sources = build_expected_sets(expected)
    for position, hit in enumerate(hits, start=1):
        if not is_relevant(hit, refs, sources):
            continue
        outcome.in_final = True
        outcome.final_rank = position
        recalled_by = set(hit.get("recalled_by") or [])
        outcome.in_vector = "vector" in recalled_by
        outcome.in_bm25 = "bm25" in recalled_by
        outcome.in_graph = "graph" in recalled_by
        outcome.vector_rank = hit.get("vector_rank")
        outcome.bm25_rank = hit.get("bm25_rank")
        outcome.graph_rank = hit.get("graph_rank")
        break

    return outcome


# ---------- 阶段汇总 ----------
@dataclass
class StageResult:
    """一个检索阶段（mode + 是否精排）在整套 Golden Set 上的表现"""

    label: str
    mode: str
    rerank: bool
    k: int
    outcomes: List[QueryOutcome] = field(default_factory=list)

    # ---------- 汇总 ----------
    def _subset(self, kind: Optional[str] = None) -> List[QueryOutcome]:
        if kind is None:
            return self.outcomes
        return [o for o in self.outcomes if o.kind == kind]

    def metrics(self, kind: Optional[str] = None, k: Optional[int] = None) -> Dict[str, Any]:
        subset = self._subset(kind)
        total = len(subset)
        if total == 0:
            return {"total": 0, "recall": 0, "top1": 0, "mrr": 0.0, "ndcg": 0.0,
                    "precision": 0.0, "recall_rate": 0.0, "top1_rate": 0.0, "avg_ms": 0.0}

        top_k = k or self.k
        recall = sum(1 for o in subset if o.rank > 0 and o.rank <= top_k)
        top1 = sum(1 for o in subset if o.rank == 1)
        return {
            "total": total,
            "recall": recall,
            "top1": top1,
            "recall_rate": round(recall / total, 4),
            "top1_rate": round(top1 / total, 4),
            "mrr": round(sum(o.mrr for o in subset) / total, 4),
            "ndcg": round(sum(o.ndcg for o in subset) / total, 4),
            "precision": round(sum(o.precision for o in subset) / total, 4),
            "avg_ms": round(sum(o.duration_ms for o in subset) / total, 2),
        }

    def summary(self) -> Dict[str, Any]:
        """整体 + 按 kind 拆分的完整汇总"""
        by_kind = {kind: self.metrics(kind) for kind in sorted({o.kind for o in self.outcomes})}
        return {
            "label": self.label,
            "mode": self.mode,
            "rerank": self.rerank,
            "k": self.k,
            "overall": self.metrics(),
            "by_kind": by_kind,
        }

    def failures(self, k: Optional[int] = None) -> List[QueryOutcome]:
        """未命中的样本（Badcase 的候选池）"""
        top_k = k or self.k
        return [o for o in self.outcomes if not (0 < o.rank <= top_k)]

    def latency_percentile(self, percentile: float = 0.95) -> float:
        """检索耗时百分位 —— 平均值会被少数慢查询拉平，掩盖真实体验"""
        values = sorted(o.duration_ms for o in self.outcomes)
        if not values:
            return 0.0
        idx = min(len(values) - 1, int(round(percentile * (len(values) - 1))))
        return round(values[idx], 2)


def format_line(stage: StageResult) -> str:
    """对齐 Phase 3 脚本的输出格式，便于与历史基线逐字对照"""
    overall = stage.metrics()
    parts = [f'{stage.label:<26} Recall@{stage.k}={overall["recall"]}/{overall["total"]}']
    for kind in sorted({o.kind for o in stage.outcomes}):
        m = stage.metrics(kind)
        parts.append(f'{kind} {m["recall"]}/{m["total"]}')
    parts.append(f'MRR={overall["mrr"]:.3f}')
    parts.append(f'Top1={overall["top1"]}/{overall["total"]}')
    return "  ".join(parts)


def aggregate_stages(stages: Iterable[StageResult]) -> Dict[str, Any]:
    return {stage.label: stage.summary() for stage in stages}


__all__ = [
    "DEFAULT_K",
    "QueryOutcome",
    "StageResult",
    "aggregate_stages",
    "build_expected_sets",
    "evaluate_query",
    "first_relevant_rank",
    "format_line",
    "hit_ref",
    "is_relevant",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]
