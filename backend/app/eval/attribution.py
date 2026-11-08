"""
Badcase 归因 —— 把「这条错了」变成「错在哪一环、该怎么改」

★ 为什么归因比指标更重要

指标只告诉你从 33/34 掉到 32/34，不告诉你为什么。而检索链路上有四个
**性质完全不同**的可能失手点，每一个的修法都不一样：

    向量召回池 ──► BM25 召回池 ──► RRF 融合 ──► 精排 ──► 交给模型
       │              │              │           │          │
    嵌入模型/         分词/索引      权重/k 值    精排模型    提示词/
    分块尺度          是否为空                  判错方向    上下文组织

「混合检索的召回率掉了」这句话本身不指向任何修法。所以本模块把一次失败
**定位到具体一环**，并给出该环对应的排查方向。

★ 四级漏斗（这是归因的前提）

只拿最终结果无法归因 —— 目标没出现在最终 top-k 时，你无从知道它
「压根没被召回」还是「被召回了但被后面某一环挤掉」。所以必须把中间状态记下来：

    vector_pool  向量路 top_k 候选        （含目标？名次多少？）
    bm25_pool    BM25 路 top_k 候选
    fused        RRF 融合后的候选池
    final        精排后交给模型的 top-N

★ 三分类（与设计文档一致）

| 类别              | 判定条件                          | 含义                       |
|-------------------|-----------------------------------|----------------------------|
| `retrieval_miss`  | 两条召回路都没捞到                 | 检索层问题                 |
| `ranking_miss`    | 捞到了，但被融合或精排挤出 top-k    | 排序层问题                 |
| `generation_miss` | 检索正确，但回答没利用上下文        | 生成层问题                 |

前两类由漏斗直接判定；第三类需要答案质量判定的结果（见 judge.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# ---------- 类别与环节 ----------
CATEGORY_OK = "ok"
CATEGORY_RETRIEVAL_MISS = "retrieval_miss"
CATEGORY_RANKING_MISS = "ranking_miss"
CATEGORY_GENERATION_MISS = "generation_miss"

CATEGORY_LABELS = {
    CATEGORY_OK: "正常",
    CATEGORY_RETRIEVAL_MISS: "检索未召回",
    CATEGORY_RANKING_MISS: "召回了但被排序挤掉",
    CATEGORY_GENERATION_MISS: "召回了但回答没用上",
}

STAGE_VECTOR = "vector_pool"
STAGE_BM25 = "bm25_pool"
STAGE_GRAPH = "graph_pool"
STAGE_FUSION = "fusion"
STAGE_RERANK = "rerank"
STAGE_BACKFILL = "graph_backfill"
STAGE_GENERATION = "generation"

STAGE_LABELS = {
    STAGE_VECTOR: "向量召回",
    STAGE_BM25: "BM25 召回",
    STAGE_GRAPH: "图召回（多跳）",
    STAGE_FUSION: "RRF 融合",
    STAGE_RERANK: "精排",
    STAGE_BACKFILL: "多跳证据保底",
    STAGE_GENERATION: "生成",
}

#: 答案质量的判定阈值。0.7 是个偏宽松的取值 ——
#: 归因的目的是「找出值得人看的样本」，过严会把大量边缘样本灌进报告，反而没人看。
FAITHFULNESS_THRESHOLD = 0.7
RELEVANCY_THRESHOLD = 0.7

#: 每个环节对应的排查方向（写在归因结论里，让人不必回头翻文档）
STAGE_SUGGESTIONS = {
    STAGE_VECTOR: (
        "向量路未命中：优先查嵌入模型是否与该语料域匹配、分块是否把答案切散了、"
        "以及查询向量化是否失败（向量路候选为 0 时通常就是 Embedding 挂了）"
    ),
    STAGE_BM25: (
        "BM25 路未命中：先确认索引里有文档（候选恒为 0 基本就是索引为空，"
        "跑 scripts/index_samples.py 重建），再查分词是否把查询词切碎、"
        "领域词是否需要补进 DOMAIN_WORDS"
    ),
    STAGE_FUSION: (
        "目标进了某一路的候选池却没进融合结果：RRF 的固有性质 —— "
        "只被单路召回的文档只拿一份 1/(k+rank)，会被双路共识项挤出。"
        "缓解办法是加大每路 top_k 或减小 HYBRID_RRF_K，不要试图用分数归一化绕开"
    ),
    STAGE_GRAPH: (
        "目标只被图路召回，另外两路的候选池里根本没有它 —— 这是**多跳证据**的典型形态。"
        "它拿不到 RRF 的共识票，因此在纯 RRF 排序下必然排在「两路都命中」的块之后。"
        "这正是需要 `GRAPH_MIN_MULTIHOP`（多跳证据保底槽）的原因："
        "把图路独有的候选按跳数补进最终结果，而不是指望它靠投票挤进去"
    ),
    STAGE_BACKFILL: (
        "目标被图路召回、也已经进了融合池，但仍未出现在最终结果里："
        "多跳证据保底槽可能被条数上限（GRAPH_MIN_MULTIHOP）挡掉了，"
        "或者保底只对 `hops >= 1` 的候选生效而该条是 hop=0 的种子块。"
        "先看该样本的 graph_hops 与最终结果里保底块的条数"
    ),
    STAGE_RERANK: (
        "目标进了融合池但被精排挤掉：精排不是单调增益，Cross-Encoder 会判错。"
        "先看该样本的 rerank_score 与相邻项的差距，判断是模型能力问题还是 query 表述问题"
    ),
    STAGE_GENERATION: (
        "检索正确但回答没利用上下文：查 RAG_SYSTEM_PROMPT 是否约束了「只依据上下文作答」、"
        "注入条数（RERANK_TOP_N）是否太少、以及上下文是否被更相关的噪声挤占"
    ),
}


# ---------- 漏斗 ----------
@dataclass
class FunnelStages:
    """
    一条查询在检索漏斗各环节的状态。

    这是归因的**唯一输入** —— 把判定所需的信息全部显式化，
    归因函数因此可以是纯函数、可脱机单测，不需要任何基础设施。
    """

    in_vector_pool: bool = False
    in_bm25_pool: bool = False
    in_graph_pool: bool = False
    in_fused: bool = False
    in_final: bool = False

    vector_pool_size: int = 0
    bm25_pool_size: int = 0
    graph_pool_size: int = 0
    fused_size: int = 0
    final_size: int = 0

    vector_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    graph_rank: Optional[int] = None
    fused_rank: Optional[int] = None
    final_rank: Optional[int] = None

    @property
    def recalled_by_any_lane(self) -> bool:
        """是否被任意一路（含图路）召回。

        图路必须计入 —— 否则"只被图路找到"的样本会被归成
        `retrieval_miss`（看起来像检索能力不足），而它其实是**排序层**
        没能把图路的独有候选带上来，两种结论对应的修法完全不同。
        """
        return self.in_vector_pool or self.in_bm25_pool or self.in_graph_pool

    @property
    def recalled_only_by_graph(self) -> bool:
        """是否**仅**被图路召回（多跳证据的标志性特征）"""
        return self.in_graph_pool and not (self.in_vector_pool or self.in_bm25_pool)

    def describe(self) -> str:
        """一行漏斗快照，便于在报告里直接展开"""
        def mark(present: bool, rank: Optional[int], size: int) -> str:
            if not present:
                return f"✗(0/{size})" if size == 0 else f"✗(/{size})"
            return f"✓#{rank}(/{size})"

        return (
            f"向量 {mark(self.in_vector_pool, self.vector_rank, self.vector_pool_size)} | "
            f"BM25 {mark(self.in_bm25_pool, self.bm25_rank, self.bm25_pool_size)} | "
            f"图 {mark(self.in_graph_pool, self.graph_rank, self.graph_pool_size)} | "
            f"融合 {mark(self.in_fused, self.fused_rank, self.fused_size)} | "
            f"精排 {mark(self.in_final, self.final_rank, self.final_size)}"
        )


# ---------- 归因结果 ----------
@dataclass
class Attribution:
    """单条 Badcase 的归因结论"""

    item_id: str
    query: str
    kind: str = ""
    note: str = ""
    category: str = CATEGORY_OK
    stage: Optional[str] = None
    reason: str = ""
    suggestion: str = ""
    hints: List[str] = field(default_factory=list)
    stages: Optional[FunnelStages] = None
    #: 答案质量判定的原始分（generation_miss 时有值）
    scores: Dict[str, Any] = field(default_factory=dict)

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "query": self.query,
            "kind": self.kind,
            "note": self.note,
            "category": self.category,
            "category_label": self.category_label,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage or "", None),
            "reason": self.reason,
            "suggestion": self.suggestion,
            "hints": list(self.hints),
            "funnel": self.stages.describe() if self.stages else None,
            "scores": self.scores,
        }


# ---------- 归因逻辑 ----------
def classify_retrieval(
    item: Any,
    stages: FunnelStages,
    *,
    k: int,
    hit: bool,
) -> Attribution:
    """
    对一条查询的**检索阶段**做归因。

    Args:
        item: GoldenItem
        stages: 四级漏斗状态
        k: 判定「命中」的 top-k
        hit: 该条最终是否落在 top-k 内
    """
    base = Attribution(
        item_id=getattr(item, "id", ""),
        query=getattr(item, "query", ""),
        kind=getattr(item, "kind", ""),
        note=getattr(item, "note", ""),
        stages=stages,
    )

    if hit:
        base.category = CATEGORY_OK
        base.reason = "目标落在最终 top-k 内"
        return base

    # ---------- ① 检索未召回：所有路都没捞到 ----------
    if not stages.recalled_by_any_lane:
        base.category = CATEGORY_RETRIEVAL_MISS
        # 判断是哪一路的问题；全空说明更可能是查询侧或基础设施出了问题
        all_empty = (
            stages.vector_pool_size == 0
            and stages.bm25_pool_size == 0
            and stages.graph_pool_size == 0
        )
        if all_empty:
            base.stage = STAGE_VECTOR
            base.reason = "向量路、BM25 路与图路的候选池**均为空**，目标不可能被召回"
            base.hints.append(
                "所有路同时为空几乎不指向检索算法，而是基础设施问题："
                "Embedding/Milvus 不可用（向量路空）、ES 索引为空（BM25 路空）、"
                "Neo4j 不可用或图未构建（图路空）。先在 /api/health 看依赖状态"
            )
        elif stages.bm25_pool_size == 0:
            base.stage = STAGE_BM25
            base.reason = "BM25 候选池为空，且其余路也未召回目标"
            base.hints.append(
                "BM25 候选恒为 0 通常不是「这个词搜不到」，而是索引里没有文档 —— "
                "这是本项目已知的静默降级：ES 端口通、索引建好、但 0 条文档"
            )
        elif stages.vector_pool_size == 0:
            base.stage = STAGE_VECTOR
            base.reason = "向量路候选池为空，且其余路也未召回目标"
            base.hints.append("向量路候选恒为 0，优先查 Embedding 是否可用")
        else:
            base.stage = STAGE_VECTOR
            base.reason = (
                f"各路都有候选（向量 {stages.vector_pool_size} 条 / "
                f"BM25 {stages.bm25_pool_size} 条 / 图 {stages.graph_pool_size} 条）"
                "但都不包含目标，属于召回能力问题"
            )
            if stages.graph_pool_size == 0 and stages.in_graph_pool is False:
                base.hints.append(
                    "若该样本属于多跳类目：图路候选为 0 时优先确认「声明的图是否写进了探针空间」"
                    "（`/api/graph/status` 的 stats.relations 应为非零）"
                )
        base.suggestion = STAGE_SUGGESTIONS[base.stage]
        return base

    # ---------- ② 召回了但没进最终 top-k：排序问题 ----------
    base.category = CATEGORY_RANKING_MISS

    # ②-a 仅被图路召回：多跳证据的典型形态，与“两路共识被挤掉”是两回事
    if stages.recalled_only_by_graph:
        base.stage = STAGE_BACKFILL if stages.in_fused else STAGE_GRAPH
        where = "已进入融合候选池（第 %s 位）" % stages.fused_rank if stages.in_fused else "被图路召回"
        base.reason = (
            f"目标{where}，但向量与 BM25 两路都没有它（图路排名 #{stages.graph_rank}），"
            f"最终未进 top-{k}"
        )
        base.hints.append(
            "这条是**多跳证据**：它只被图路看见，而 RRF 按「被几路命中」投票，"
            "图路单独一票天然比不过两路共识的块。指望它靠 RRF 挤进 top-k 是不现实的，"
            "只能靠 `GRAPH_MIN_MULTIHOP` 的保底槽把它补进来"
        )
        base.suggestion = STAGE_SUGGESTIONS[base.stage]
        return base

    if stages.in_fused:
        base.stage = STAGE_RERANK
        base.reason = (
            f"目标已进入融合候选池（第 {stages.fused_rank} 位），"
            f"但被精排挤出了 top-{k}"
        )
        base.hints.append(
            "精排是增益项而非常量保证：它纠正的比破坏的多，但不是单调改进。"
            "判断是否需要处理，要看同批样本里精排纠正了多少条"
        )
    else:
        base.stage = STAGE_FUSION
        lanes = []
        if stages.in_vector_pool:
            lanes.append(f"向量#{stages.vector_rank}")
        if stages.in_bm25_pool:
            lanes.append(f"BM25#{stages.bm25_rank}")
        if stages.in_graph_pool:
            lanes.append(f"图#{stages.graph_rank}")
        base.reason = (
            f"目标被 {'、'.join(lanes) or '未知路'} 召回，"
            f"但在 RRF 融合阶段被挤出候选池"
        )
        base.hints.append(
            "这是 RRF 的固有性质而非缺陷：只被单路召回的文档只拿一份 1/(k+rank)，"
            "会被两路都召回、各自排名略靠后的文档用「两份之和」反超。"
            "⚠️ 不要为此写「混合召回 ≥ 任一单路」的断言 —— 数学上不成立"
        )
    base.suggestion = STAGE_SUGGESTIONS[base.stage]
    return base


def classify_generation(
    base: Attribution,
    *,
    judge_scores: Dict[str, Any],
    k: int,
) -> Attribution:
    """
    在检索已经正确的前提下，判断回答是否没能利用上下文。

    只在「检索命中」时才有意义 —— 检索就没召回时，回答不好是必然结果，
    把它归到生成层会掩盖真正的问题。
    """
    if base.category != CATEGORY_OK:
        return base

    faithfulness = judge_scores.get("faithfulness")
    relevancy = judge_scores.get("answer_relevancy")

    low_faith = isinstance(faithfulness, (int, float)) and faithfulness < FAITHFULNESS_THRESHOLD
    low_rel = isinstance(relevancy, (int, float)) and relevancy < RELEVANCY_THRESHOLD

    if not low_faith and not low_rel:
        return base

    base.category = CATEGORY_GENERATION_MISS
    base.stage = STAGE_GENERATION
    base.scores = dict(judge_scores)

    parts = []
    if low_faith:
        parts.append(f"忠实度 {faithfulness:.2f} < {FAITHFULNESS_THRESHOLD}")
    if low_rel:
        parts.append(f"答案相关性 {relevancy:.2f} < {RELEVANCY_THRESHOLD}")
    base.reason = "检索已命中且排在前列，但回答质量不达标（" + "；".join(parts) + "）"

    if low_faith:
        base.hints.append(
            "忠实度低 = 回答里出现了上下文支持不了的内容，"
            "通常是模型在「补全」而非「引用」—— 检查 RAG_SYSTEM_PROMPT 的约束措辞"
        )
    if low_rel:
        base.hints.append(
            "答案相关性低 = 答非所问。检索对了却答偏，通常是上下文顺序把噪声放在了前面，"
            "或注入条数过多稀释了关键段"
        )
    base.suggestion = STAGE_SUGGESTIONS[STAGE_GENERATION]
    return base


# ---------- 汇总 ----------
def summarize(attributions: Sequence[Attribution]) -> Dict[str, Any]:
    """按类别与环节汇总，并统计「如果每条都修好能挽回多少召回」"""
    total = len(attributions)
    if total == 0:
        return {"total": 0, "by_category": {}, "by_stage": {}, "badcases": 0}

    by_category: Dict[str, int] = {}
    by_stage: Dict[str, int] = {}
    for attr in attributions:
        by_category[attr.category] = by_category.get(attr.category, 0) + 1
        if attr.stage:
            by_stage[attr.stage] = by_stage.get(attr.stage, 0) + 1

    badcases = total - by_category.get(CATEGORY_OK, 0)
    return {
        "total": total,
        "badcases": badcases,
        "pass_rate": round(by_category.get(CATEGORY_OK, 0) / total, 4),
        "by_category": {
            CATEGORY_LABELS.get(cat, cat): count for cat, count in sorted(by_category.items())
        },
        "by_category_raw": by_category,
        "by_stage": {
            STAGE_LABELS.get(stage, stage): count for stage, count in sorted(by_stage.items())
        },
    }


def dominant_stage(attributions: Sequence[Attribution]) -> Optional[str]:
    """
    最主要的失手环节。

    只统计 Badcase（跳过 ok）。返回 `None` 表示没有 Badcase。
    用途是给出一条「本轮该先改哪里」的结论 —— 报告里最有价值的一行。
    """
    by_stage: Dict[str, int] = {}
    for attr in attributions:
        if attr.category != CATEGORY_OK and attr.stage:
            by_stage[attr.stage] = by_stage.get(attr.stage, 0) + 1
    if not by_stage:
        return None
    return max(by_stage.items(), key=lambda kv: kv[1])[0]


__all__ = [
    "Attribution",
    "FunnelStages",
    "CATEGORY_GENERATION_MISS",
    "CATEGORY_LABELS",
    "CATEGORY_OK",
    "CATEGORY_RANKING_MISS",
    "CATEGORY_RETRIEVAL_MISS",
    "FAITHFULNESS_THRESHOLD",
    "RELEVANCY_THRESHOLD",
    "STAGE_BACKFILL",
    "STAGE_BM25",
    "STAGE_FUSION",
    "STAGE_GENERATION",
    "STAGE_GRAPH",
    "STAGE_LABELS",
    "STAGE_RERANK",
    "STAGE_SUGGESTIONS",
    "STAGE_VECTOR",
    "classify_generation",
    "classify_retrieval",
    "dominant_stage",
    "summarize",
]
