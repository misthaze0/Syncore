"""
评估编排 —— 把 Golden Set、检索链路、指标与归因串成一次可复现的跑批

★ 为什么评估必须跑在**独立探针索引**上

沿用 `verify_real_pipeline.py` / `verify_hybrid_search.py` 的做法：
受控语料写进独立的 Milvus Collection 与 ES 索引，跑完即删。三个理由：

1. **不污染演示数据**。`knowledge_base` 是给用户看的知识库；
   往里塞 34 条人造语料会让「问什么都返回配置项速查表」。
2. **语料规模可控**。必须让语料条数 > 单路 top_k，否则两路召回集完全相同，
   混合检索的差异会被掩盖 —— 这是 Phase 3 踩过的坑。
3. **ground truth 可精确对齐**。探针语料由 Golden Set 自己生成，
   每条查询的正确答案是哪一块是构造时确定的，不依赖人工标注。

★ 为什么评估要走生产入口 `retrieve()` 而不是内联重写一遍链路

评估一个**自己重写的**检索实现，测出来的是那个实现的质量，不是线上的。
所以每一阶段都只改 `settings.RETRIEVAL_MODE` / `RERANK_ENABLED` 再调
`retrieve()` —— 与 `verify_hybrid_search.py` 完全一致，这也是它能与 Phase 3
基线直接对照的原因。

★ 归因为什么要多跑两次单路召回

漏斗需要「目标在各召回路候选池里的名次」，而 `retrieve_hybrid()` 只返回最终结果。
所以在最终阶段额外调一次 `retrieve_vector` / `retrieve_bm25` 记录中间状态。
代价是每条查询多两次检索（34 条约多 20 秒），换来的是**可归因** ——
相比「只报一个总召回率、错了也不知道改哪里」，这个代价是值得的。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.eval.attribution import (
    Attribution,
    FunnelStages,
    classify_generation,
    classify_retrieval,
    dominant_stage,
    summarize as summarize_attributions,
)
from app.eval.golden import GoldenItem, GoldenSet
from app.eval.judge import AnswerEvaluation, evaluate_answer, get_judge
from app.eval.metrics import (
    DEFAULT_K,
    StageResult,
    build_expected_sets,
    evaluate_query,
    is_relevant,
)

logger = logging.getLogger(__name__)

#: 探针资源名。刻意与 `verify_hybrid_search.py` 用**不同**的名字：
#: 两个脚本可能被同时运行，共用名字会互相 drop 掉对方的索引。
PROBE_COLLECTION = "_eval_probe"
PROBE_ES_INDEX = "xjms_eval_probe"


# ---------- 配置 ----------
@dataclass
class StageSpec:
    """一个对照阶段的定义"""

    label: str
    mode: str          # vector | bm25 | hybrid | graph | hybrid_graph
    rerank: bool
    #: 本阶段是否启用图路。
    #
    # ★ 为什么必须显式声明，而不是跟着 .env 走
    #
    # 评估要在**同一次运行里**跑出「有图」与「没图」两组数字。若图路开关
    # 跟着全局配置，那么加上图路之后，①~④ 这四个基线阶段的结果也会变 ——
    # 于是 Phase 3/4 的历史结论再也无法与本次逐字对照，
    # 「这次的 34/34 和上次的 34/34 是同一件事吗」这个问题就永远答不上来。
    #
    # 所以每个阶段自己声明要不要图，`run_stage` 按它临时改配置再还原。
    graph: bool = False


def default_stages() -> List[StageSpec]:
    """
    六阶段对照实验。

    前四个与 Phase 3 的对照实验**完全一致**（且图路关闭），保证历史结论可直接对照；
    后两个是 Phase 4b 为 GraphRAG 新增的对照：

        ⑤ 仅图（无精排）       —— 图路单独的能力边界
        ⑥ 混合 + 图 + 精排     —— 与 ④ 只差一个图路，差值即图路的贡献

    ⑤ 用独立模式名 `graph`、⑥ 用 `hybrid_graph`，而不是复用 `hybrid`：
    评估按 `(mode, rerank)` 索引阶段，复用模式名会让 ⑥ 覆盖掉 ④，
    「有图」与「没图」的数字再也分不开。
    """
    return [
        StageSpec("① 仅向量（无精排）", "vector", False),
        StageSpec("② 仅关键词（无精排）", "bm25", False),
        StageSpec("③ 混合 RRF（无精排）", "hybrid", False),
        StageSpec("④ 混合 + 精排（最终）", "hybrid", True),
        StageSpec("⑤ 仅图（多跳，无精排）", "graph", False, graph=True),
        StageSpec("⑥ 混合 + 图 + 精排", "hybrid_graph", True, graph=True),
    ]


#: 不启用图路的阶段标签（供 `run_eval.assert_baseline` 之外的调用方识别基线阶段）
BASELINE_STAGES = ("① 仅向量（无精排）", "② 仅关键词（无精排）",
                   "③ 混合 RRF（无精排）", "④ 混合 + 精排（最终）")


@dataclass
class EvalConfig:
    k: int = DEFAULT_K
    stages: List[StageSpec] = field(default_factory=default_stages)
    #: 单路召回条数。默认压到 5（与 Phase 3 一致）以放大两路差异 ——
    #: 用生产默认的 20/20 时两路召回集高度重合，混合与单路的差别会被抹平
    lane_top_k: Optional[int] = 5
    rerank_top_n: Optional[int] = None
    #: 归因的判定口径，缺省与 k 相同。
    #
    # 为什么要能单独设：k 越大越容易「全对」，于是归因无事可做。
    # 本项目最终链路在 Recall@3 上已经是 34/34，此时归因会输出「0 条 Badcase」——
    # 结论正确但没有信息量。把口径收紧到 Top-1（`attribution_k=1`）就能看出
    # 「哪几条召回了却没排在第 1 位」，那才是排序层真正值得优化的样本。
    #
    # ⚠️ 这只是**换一个判定口径**，不是制造失败：判据仍是「目标是否落在前 N 位」，
    # 与指标的 k 用同一套 `first_relevant_rank` 计算。
    attribution_k: Optional[int] = None
    #: 是否额外跑答案质量评估（会打真实模型，34 条约 2~4 分钟）
    run_answer_eval: bool = False
    #: 答案评估的样本上限（0 = 全部）
    answer_sample_limit: int = 0
    #: True 时保留探针索引（调试用；默认清理）
    keep_probe: bool = False
    #: 「（可选）只跑指标、跳过归因」——归因需要额外两次单路检索
    skip_attribution: bool = False

    @property
    def effective_attribution_k(self) -> int:
        """
        归因口径，夹到 `[1, k]` 区间。

        上限夹到 k 的原因：漏斗的「最终池」就是最终阶段 `top_k=k` 的那批结果，
        口径放到比 k 还大时，`final_rank` 根本取不到那些值 ——
        那会让每条查询都被判成命中，得到一个虚假的「零 Badcase」。
        """
        return max(1, min(self.attribution_k or self.k, self.k))


# ---------- 报告 ----------
@dataclass
class EvalReport:
    golden_name: str = ""
    scope: str = ""
    item_count: int = 0
    kind_counts: Dict[str, int] = field(default_factory=dict)
    k: int = DEFAULT_K
    duration_ms: float = 0.0
    stages: List[StageResult] = field(default_factory=list)
    attributions: List[Attribution] = field(default_factory=list)
    attribution_summary: Dict[str, Any] = field(default_factory=dict)
    #: 归因实际使用的判定口径（可能比 k 更严，见 EvalConfig.attribution_k）
    attribution_k: int = DEFAULT_K
    dominant_stage: Optional[str] = None
    answer_evals: List[AnswerEvaluation] = field(default_factory=list)
    answer_summary: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def final_stage(self) -> Optional[StageResult]:
        return self.stages[-1] if self.stages else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "golden": {
                "name": self.golden_name,
                "scope": self.scope,
                "items": self.item_count,
                "kinds": self.kind_counts,
            },
            "k": self.k,
            "duration_ms": self.duration_ms,
            "environment": self.environment,
            "stages": [s.summary() for s in self.stages],
            "attribution": {
                "summary": self.attribution_summary,
                "k": self.attribution_k,
                "dominant_stage": self.dominant_stage,
                "badcases": [a.to_dict() for a in self.attributions
                             if a.category != "ok"],
            },
            "answers": self.answer_summary,
            "notes": self.notes,
            "error": self.error,
        }


# ---------- 探针索引 ----------
async def setup_probe(golden: GoldenSet) -> int:
    """把受控语料写入独立探针索引，返回写入块数"""
    from app.core.embedding_factory import get_embeddings
    from app.core.milvus_client import milvus_manager
    from app.rag.es_client import es_manager

    milvus_manager.delete_collection()
    await es_manager.delete_index()
    milvus_manager.ensure_collection()

    chunks = [
        {
            "text": c.text,
            "source": c.source,
            "page": c.page,
            "chunk_index": c.chunk_index,
        }
        for c in golden.corpus
    ]
    vectors = await get_embeddings([c["text"] for c in chunks])
    await milvus_manager.upsert(vectors, chunks)

    if settings.ES_ENABLED:
        await es_manager.index_chunks(chunks)
    return len(chunks)


async def setup_probe_graph(golden: GoldenSet) -> int:
    """
    把 Golden Set 声明的三元组写进探针图空间，返回写入的关系条数。

    ★ 为什么探针的图也要按空间隔离

    图的命名空间绑定 `settings.MILVUS_COLLECTION`（见 `graph/store.py` 的说明），
    而评估在 `setup_probe` 阶段已经把它切到 `_eval_probe`。所以这里写进去的
    关系只会落在探针空间，跑完 `teardown_probe` 一并清掉 ——
    不会污染生产图谱（那会让演示时任何提问都带着评估语料的实体）。

    ★ 为什么不用模型现抽

    见 `golden.DeclaredTriple` 的说明：抽取质量是独立变量，评估要把它锁死。
    抽取本身的能力由 `scripts/verify_graphrag.py` 用真实模型单独验收。
    """
    if not golden.triples:
        return 0
    from app.graph.builder import build_from_triples
    from app.graph.models import Triple

    triples = [
        Triple(
            head=t.head, relation=t.relation, tail=t.tail,
            head_type=t.head_type, tail_type=t.tail_type,
            source=t.source, chunk_index=t.chunk_index,
        )
        for t in golden.triples
    ]
    report = await build_from_triples(triples, clear=True)
    if report.error:
        logger.warning("探针图构建失败（多跳阶段将拿不到结果）: %s", report.error)
    return report.written


async def teardown_probe() -> None:
    from app.core.milvus_client import milvus_manager
    from app.rag.es_client import es_manager

    try:
        milvus_manager.delete_collection()
    except Exception as e:  # noqa: BLE001
        logger.warning("清理探针 Collection 失败: %s", e)
    try:
        await es_manager.delete_index()
    except Exception as e:  # noqa: BLE001
        logger.warning("清理探针 ES 索引失败: %s", e)
    try:
        from app.graph.store import graph_store

        await graph_store.clear_space()
    except Exception as e:  # noqa: BLE001
        logger.warning("清理探针图谱失败: %s", e)


# ---------- 单阶段跑批 ----------
async def run_stage(
    golden: GoldenSet,
    spec: StageSpec,
    *,
    k: int,
    collect_funnel: bool = False,
) -> Tuple[StageResult, List[FunnelStages]]:
    """
    跑一个检索阶段。

    Args:
        collect_funnel: 为 True 时额外记录每条查询的四级漏斗（用于归因）。
                        会多调两次单路召回，只应在最终阶段开启。
    """
    from app.rag import retriever as retriever_module

    original_mode = settings.RETRIEVAL_MODE
    original_rerank = settings.RERANK_ENABLED
    original_graph = settings.GRAPH_ENABLED
    settings.RETRIEVAL_MODE = spec.mode
    settings.RERANK_ENABLED = spec.rerank
    # ★ 图路开关必须跟着 spec 走，不能跟着 .env 走。
    #
    # 模式名（`hybrid` / `hybrid_graph`）只决定了「走哪条分发分支」，
    # 而 `hybrid` 分支内部还会读 `settings.GRAPH_ENABLED` 决定要不要带上第三路
    # （见 `retrieve_hybrid` 的 `use_graph = settings.GRAPH_ENABLED if graph is None`）。
    # 生产环境里 `GRAPH_ENABLED=True`，若这里不关掉，
    # 阶段 ①~④ 会**静默地全部带上图路** —— 数字看着正常，但「有图 vs 没图」
    # 的对照直接失效，而且失败时没有任何症状。
    settings.GRAPH_ENABLED = spec.graph

    # 精排后端是单例，切 RERANK_ENABLED 后必须重置，否则会沿用上一个阶段的后端
    from app.rag.reranker import reset_reranker
    reset_reranker()

    stage = StageResult(label=spec.label, mode=spec.mode, rerank=spec.rerank, k=k)
    funnels: List[FunnelStages] = []

    try:
        for item in golden.items:
            t0 = time.perf_counter()
            try:
                hits = await retriever_module.retrieve(item.query, top_k=k, mode=spec.mode)
                error = None
            except Exception as e:  # noqa: BLE001
                logger.warning("查询失败 [%s] %r: %s", spec.label, item.query, e)
                hits, error = [], f"{type(e).__name__}: {e}"

            elapsed = (time.perf_counter() - t0) * 1000.0
            outcome = evaluate_query(item, hits, k=k, duration_ms=elapsed)
            outcome.error = error
            stage.outcomes.append(outcome)

            if collect_funnel:
                funnels.append(await _collect_funnel(item, hits, with_graph=spec.graph))

    finally:
        settings.RETRIEVAL_MODE = original_mode
        settings.RERANK_ENABLED = original_rerank
        settings.GRAPH_ENABLED = original_graph
        reset_reranker()

    return stage, funnels


async def _collect_funnel(
    item: GoldenItem,
    final_hits: Sequence[Dict[str, Any]],
    *,
    with_graph: bool = False,
) -> FunnelStages:
    """
    记录一条查询在「向量池 → BM25 池 → 图池 → 融合 → 精排」各级的状态。

    融合池是本地重算的（`rrf_fuse` 是纯函数），精排结果直接沿用已跑出的 `final_hits` ——
    这样既拿到了中间状态，又保证最终判定与生产链路一致（而不是重算一遍再假设它等价）。

    ★ `with_graph` 为什么必须由调用方传入，而不是读 `settings.GRAPH_ENABLED`

    归因跑在**最后那个阶段**上，而最后一个阶段的图路开关由 `StageSpec.graph` 决定。
    这里若直接读配置，在「临时改过配置又被还原」的时序下会读到还原后的值，
    漏斗与真实链路就会各说各话 —— 归因结论随之全错，而且看起来一切正常。

    ★ 融合池的重算必须带上图路

    `recalled_only_by_graph` 与 `in_fused` 的组合决定了这对样本的归因是
    `graph_backfill`（进了融合池但没进最终结果）还是 `graph`（连融合池都没进）。
    若本地重算只喂 vector+bm25，图路独有的候选永远不会出现在融合池里，
    所有多跳样本都会被误判成 `graph` —— 那会把「保底槽不够用」误诊成「图没建好」。
    """
    from app.rag import retriever as retriever_module
    from app.rag.fusion import rrf_fuse

    refs, sources = build_expected_sets(item.expected)

    def locate(pool: Sequence[Dict[str, Any]]) -> Tuple[bool, Optional[int]]:
        for rank, hit in enumerate(pool, start=1):
            if is_relevant(hit, refs, sources):
                return True, rank
        return False, None

    vector_pool = await retriever_module.retrieve_vector(item.query, settings.HYBRID_VECTOR_TOP_K)
    bm25_pool = await retriever_module.retrieve_bm25(item.query, settings.HYBRID_BM25_TOP_K)
    # 与生产同口径取池：`retrieve_hybrid` 用的就是 GRAPH_TOP_K
    graph_pool: List[Dict[str, Any]] = (
        await retriever_module.retrieve_graph_lane(item.query, settings.GRAPH_TOP_K)
        if with_graph else []
    )

    lanes: Dict[str, List[Dict[str, Any]]] = {"vector": vector_pool, "bm25": bm25_pool}
    weights: Dict[str, float] = {
        "vector": settings.HYBRID_VECTOR_WEIGHT,
        "bm25": settings.HYBRID_BM25_WEIGHT,
    }
    if with_graph:
        lanes["graph"] = graph_pool
        weights["graph"] = settings.GRAPH_WEIGHT

    fused_pool: List[Dict[str, Any]] = []
    if any(lanes.values()):
        fused_pool = rrf_fuse(lanes, k=settings.HYBRID_RRF_K, weights=weights)

    in_vec, vec_rank = locate(vector_pool)
    in_bm, bm_rank = locate(bm25_pool)
    in_graph, graph_rank = locate(graph_pool)
    in_fused, fused_rank = locate(fused_pool)
    in_final, final_rank = locate(final_hits)

    return FunnelStages(
        in_vector_pool=in_vec,
        in_bm25_pool=in_bm,
        in_graph_pool=in_graph,
        in_fused=in_fused,
        in_final=in_final,
        vector_pool_size=len(vector_pool),
        bm25_pool_size=len(bm25_pool),
        graph_pool_size=len(graph_pool),
        fused_size=len(fused_pool),
        final_size=len(final_hits),
        vector_rank=vec_rank,
        bm25_rank=bm_rank,
        graph_rank=graph_rank,
        fused_rank=fused_rank,
        final_rank=final_rank,
    )


# ---------- 答案质量评估 ----------
ANSWER_PROMPT_HEADER = """你是 Syncore，一个严谨的知识库问答助手。
请**只依据下面提供的资料**回答问题。资料中没有的内容，明确说明「资料中未提及」，不要用你自己的知识补充。

【资料】
{context}

【问题】
{question}"""


async def run_answer_eval(
    golden: GoldenSet,
    *,
    k: int,
    limit: int = 0,
    concurrency: int = 4,
) -> Tuple[List[AnswerEvaluation], List[str]]:
    """
    对最终链路检索到的上下文，跑一次「生成 → 判定」。

    注意与线上链路的差异：这里直接「检索 → 拼 Prompt → 调模型」，
    绕过了 LangGraph 编排与工具循环。这样做是刻意的 ——
    答案质量评估要**隔离检索质量**，把 Agent 编排的变化混进来会让指标无法归因。
    """
    from app.core.llm_factory import create_llm
    from app.rag import retriever as retriever_module

    judge = get_judge()
    if judge is None:
        return [], ["答案质量判定后端不可用，已跳过答案层评估（不伪造分数）"]

    items = list(golden.items)
    if limit > 0:
        items = items[:limit]

    # 生成与判定都打真实模型，串行会非常慢；用信号量限流而不是无上限并发，
    # 避免触发网关的并发限制后整批失败
    sem = asyncio.Semaphore(max(1, concurrency))
    notes: List[str] = []

    async def one(item: GoldenItem) -> AnswerEvaluation:
        async with sem:
            try:
                hits = await retriever_module.retrieve(item.query, top_k=k, mode="hybrid")
            except Exception as e:  # noqa: BLE001
                return AnswerEvaluation(question=item.query, errors={"_retrieve": str(e)})

            contexts = [str(h.get("text", "")) for h in hits]

            # 检索就没命中时，生成质量再好也不能说明链路有效，
            # 所以仍然评估（结果会自然偏低），但不在 report 里据此外推
            prompt = ANSWER_PROMPT_HEADER.format(
                context="\n\n---\n\n".join(contexts) or "（未检索到相关资料）",
                question=item.query,
            )
            try:
                llm = create_llm(streaming=False)
                resp = await llm.ainvoke(prompt)
                answer = getattr(resp, "content", "")
                if isinstance(answer, list):
                    answer = "".join(
                        b if isinstance(b, str) else b.get("text", "")
                        for b in answer
                        if isinstance(b, (str, dict))
                    )
            except Exception as e:  # noqa: BLE001
                return AnswerEvaluation(question=item.query, errors={"_generate": str(e)})

            return await evaluate_answer(item.query, answer, contexts, backend=judge)

    results = await asyncio.gather(*(one(i) for i in items))

    if not any(r.evaluated for r in results):
        notes.append("全部样本的答案判定均未成功，答案层指标不可用")
    return list(results), notes


def summarize_answers(evals: Sequence[AnswerEvaluation]) -> Dict[str, Any]:
    """
    汇总答案层指标。

    分母只用**成功判定**的样本数，而不是样本总数 ——
    把「判定失败」当成「得 0 分」会让指标随网关稳定性波动，
    而这类波动与系统质量毫无关系。
    """
    if not evals:
        return {"total": 0, "evaluated": 0, "metrics": {}}

    metrics: Dict[str, Dict[str, Any]] = {}
    for name in ("faithfulness", "answer_relevancy"):
        values = [
            e.scores.get(name) for e in evals if e.scores.get(name) is not None
        ]
        if not values:
            metrics[name] = {"evaluated": 0, "avg": None}
            continue
        metrics[name] = {
            "evaluated": len(values),
            "avg": round(sum(values) / len(values), 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "below_threshold": sum(1 for v in values if v < 0.7),
        }

    return {
        "total": len(evals),
        "evaluated": sum(1 for e in evals if e.evaluated),
        "metrics": metrics,
        "failures": [
            {"question": e.question, "errors": e.errors}
            for e in evals
            if not e.evaluated
        ][:10],
    }


# ---------- 主流程 ----------
def _snapshot_settings() -> Dict[str, Any]:
    return {
        "MILVUS_COLLECTION": settings.MILVUS_COLLECTION,
        "ES_INDEX": settings.ES_INDEX,
        "RETRIEVAL_MODE": settings.RETRIEVAL_MODE,
        "RERANK_ENABLED": settings.RERANK_ENABLED,
        "HYBRID_VECTOR_TOP_K": settings.HYBRID_VECTOR_TOP_K,
        "HYBRID_BM25_TOP_K": settings.HYBRID_BM25_TOP_K,
        "RERANK_TOP_N": settings.RERANK_TOP_N,
        # 图路开关也在 run_stage 里被临时改过。它自己会在 finally 还原，
        # 这里再兜一层：万一 `run_stage` 的还原路径被异常跳过，
        # 评估结束后整个进程会带着「图关着」的状态继续跑 —— 零症状但全链路降级。
        "GRAPH_ENABLED": settings.GRAPH_ENABLED,
    }


def _restore_settings(snapshot: Dict[str, Any]) -> None:
    for key, value in snapshot.items():
        setattr(settings, key, value)


def environment_info() -> Dict[str, Any]:
    """
    报告自描述所需的环境快照 —— 换个模型重跑后必须能看出结论不是同一前提下得出的。

    ⚠️ 这里必须先 `get_reranker()` 再取 `reranker_status()`：
    精排后端是**惰性单例**，不主动构造的话 `active` 永远是 `not_loaded`，
    报告里就会写着「精排未加载」而实际跑的是 API 精排 —— 一份错误的元数据
    会让所有指标都失去可比性。

    同一条理由，Embedding 也要报**当前后端实际在用的**那一个：本地模型与外部模型
    产出的是**两个不可比的向量空间**，报告里写着 `qwen3.7-text-embedding` 而实际
    跑的是本地 bge，等于把「换了模型」这件事从元数据里抹掉了 —— 以后再回头看
    这份报告，会拿它跟旧报告直接比。所以这里连 `backend` 一起写进同一个字段。
    """
    from app.core.embedding_factory import embedding_info
    from app.rag.reranker import get_reranker, reranker_status

    try:
        get_reranker()
    except Exception:  # noqa: BLE001
        pass

    emb = embedding_info()
    # 维度解析失败（配置与模型不符）时也照样写进报告：
    # 「这份报告是在一个配置坏掉的环境里跑出来的」本身就是必须被记录的事实。
    emb_label = f"{emb['model']} ({emb['dimension']}d, {emb['backend']})"
    if emb.get("degraded_reason"):
        emb_label += f" ⚠️ {emb['degraded_reason']}"

    return {
        "llm": settings.LLM_MODEL,
        "embedding": emb_label,
        "milvus": settings.milvus_uri,
        "es_enabled": settings.ES_ENABLED,
        "retrieval_mode": settings.RETRIEVAL_MODE,
        "vector_top_k": settings.HYBRID_VECTOR_TOP_K,
        "bm25_top_k": settings.HYBRID_BM25_TOP_K,
        "rrf_k": settings.HYBRID_RRF_K,
        "rerank": reranker_status(),
        "judge": settings.EVAL_JUDGE_BACKEND,
        # 图这里只放**静态**配置：`graph_store.status()` 是 async（要真连一次 Neo4j），
        # 而这个函数是同步的、也被别处以同步方式调用。
        # 活体状态（连通性 + 探针图里实际有多少关系）由 `run_eval` 在跑完后异步补进来，
        # 因为「⑤⑥ 两个图阶段为 0」到底是环境还是算法，只能靠那份活体状态回答。
        "graph": {
            "enabled": settings.GRAPH_ENABLED,
            "uri": settings.GRAPH_URI,
            "database": settings.GRAPH_DATABASE,
            "max_hops": settings.GRAPH_MAX_HOPS,
            "top_k": settings.GRAPH_TOP_K,
            "weight": settings.GRAPH_WEIGHT,
            "min_multihop": settings.GRAPH_MIN_MULTIHOP,
        },
    }


async def enrich_graph_environment(report: EvalReport) -> None:
    """
    把图路的**活体**状态补进报告环境快照（失败只记原因，不抛）。

    必须在 `teardown_probe()` **之前**调用 —— 一旦探针空间被清掉，
    `stats` 里的关系数就永远是 0，而"图是空的"正是这份信息要回答的问题。
    """
    try:
        from app.graph.store import graph_store

        live = await graph_store.status()
    except Exception as e:  # noqa: BLE001
        live = {"available": False, "degraded_reason": f"{type(e).__name__}: {e}"}

    graph = report.environment.setdefault("graph", {})
    graph.update(live)


async def run_eval(golden: GoldenSet, config: Optional[EvalConfig] = None) -> EvalReport:
    """跑一次完整评估。异常一律收敛进 report.error，不向上抛。"""
    config = config or EvalConfig()
    report = EvalReport(
        golden_name=golden.name,
        scope=golden.scope,
        item_count=len(golden.items),
        kind_counts=golden.counts_by_kind(),
        k=config.k,
        environment=environment_info(),
    )

    snapshot = _snapshot_settings()
    started = time.perf_counter()
    probe_ready = False

    try:
        # ---------- 1. 受控语料进探针索引 ----------
        if golden.scope == "controlled":
            settings.MILVUS_COLLECTION = PROBE_COLLECTION
            settings.ES_INDEX = PROBE_ES_INDEX
            if config.lane_top_k is not None:
                settings.HYBRID_VECTOR_TOP_K = config.lane_top_k
                settings.HYBRID_BM25_TOP_K = config.lane_top_k
            if config.rerank_top_n is not None:
                settings.RERANK_TOP_N = config.rerank_top_n

            written = await setup_probe(golden)
            probe_ready = True
            report.notes.append(f"受控语料 {written} 块已写入探针索引 {PROBE_COLLECTION}")
            if written <= (config.lane_top_k or settings.HYBRID_VECTOR_TOP_K):
                report.notes.append(
                    "⚠ 语料块数未超过单路 top_k：两路召回集可能完全重合，"
                    "会掩盖混合检索与单路的差异"
                )

            # 图探针必须**在 MILVUS_COLLECTION 已切到探针空间之后**写，
            # 图空间是跟着集合名走的（见 setup_probe_graph 的说明）。
            if any(spec.graph for spec in config.stages):
                relations = await setup_probe_graph(golden)
                if relations:
                    report.notes.append(
                        f"声明式三元组 {relations} 条已写入探针图谱（空间 {PROBE_COLLECTION}）"
                    )
                elif golden.triples:
                    report.notes.append(
                        "⚠ 多跳类目已有查询但探针图谱一条关系都没写进去："
                        "⑤⑥ 阶段的图路必然全空，此时应查 Neo4j 连通性而不是检索算法"
                    )

                if golden.counts_by_kind().get("multi_hop") and settings.GRAPH_MAX_HOPS < 2:
                    report.notes.append(
                        f"⚠ GRAPH_MAX_HOPS={settings.GRAPH_MAX_HOPS} < 2："
                        "多跳类目的答案块挂在关系链的**末段边**上，需要两跳才够得到。"
                        "当前配置下图路只能拿到桥接块，⑤⑥ 的多跳指标会以 0 呈现 —— "
                        "那是跳数不足，不是图没建好"
                    )
        else:
            if config.lane_top_k is not None:
                settings.HYBRID_VECTOR_TOP_K = config.lane_top_k
                settings.HYBRID_BM25_TOP_K = config.lane_top_k

        # ---------- 2. 逐阶段跑批 ----------
        all_funnels: List[FunnelStages] = []
        for index, spec in enumerate(config.stages):
            is_last = index == len(config.stages) - 1
            collect = is_last and not config.skip_attribution
            logger.info("评估阶段 [%s/%s] %s", index + 1, len(config.stages), spec.label)
            stage, funnels = await run_stage(
                golden, spec, k=config.k, collect_funnel=collect,
            )
            report.stages.append(stage)
            if collect:
                all_funnels = funnels

        # ---------- 3. 归因 ----------
        if all_funnels:
            attr_k = config.effective_attribution_k
            report.attribution_k = attr_k
            attributions = []
            for item, funnel in zip(golden.items, all_funnels):
                # 用漏斗自己的 final_rank 判定，而不是 outcome.rank：
                # outcome 是按指标口径 k 算的，归因口径可能更严（如 Top-1）
                hit = bool(funnel.in_final and (funnel.final_rank or 0) <= attr_k)
                attributions.append(classify_retrieval(item, funnel, k=attr_k, hit=hit))
            report.attributions = attributions
            report.attribution_summary = summarize_attributions(attributions)
            report.dominant_stage = dominant_stage(attributions)
            if attr_k != config.k:
                report.notes.append(
                    f"归因口径为 Top-{attr_k}（比指标口径 Top-{config.k} 更严）："
                    "目的在于暴露「召回了但排序不理想」的样本 —— "
                    "在 Recall@3 已满分的情况下，这是唯一还有优化空间的地方"
                )

        # ---------- 4. 答案质量（可选） ----------
        if config.run_answer_eval:
            evals, notes = await run_answer_eval(
                golden, k=config.k, limit=config.answer_sample_limit,
            )
            report.answer_evals = evals
            report.answer_summary = summarize_answers(evals)
            report.notes.extend(notes)

            # 检索已命中但答案不达标的，重新归到生成层
            if report.attributions:
                by_id = {a.item_id: a for a in report.attributions}
                for item, ev in zip(golden.items, evals):
                    attr = by_id.get(item.id)
                    if attr is None:
                        continue
                    improved = classify_generation(
                        attr,
                        judge_scores={k: v for k, v in ev.scores.items()},
                        k=config.k,
                    )
                    by_id[item.id] = improved
                report.attributions = list(by_id.values())
                report.attribution_summary = summarize_attributions(report.attributions)
                report.dominant_stage = dominant_stage(report.attributions)

        # ---------- 5. 图路活体状态 ----------
        # 必须在这里（finally 里的 teardown_probe 之前）：探针图谱一清，
        # 关系数就永远是 0，而“图里到底有没有数据”正是这份快照要回答的问题。
        await enrich_graph_environment(report)

    except Exception as e:  # noqa: BLE001
        logger.error("评估执行失败: %s", e, exc_info=True)
        report.error = f"{type(e).__name__}: {e}"
    finally:
        report.duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if probe_ready and not config.keep_probe:
            await teardown_probe()
            report.notes.append("探针索引已清理")
        _restore_settings(snapshot)

    return report


__all__ = [
    "BASELINE_STAGES",
    "EvalConfig",
    "EvalReport",
    "PROBE_COLLECTION",
    "PROBE_ES_INDEX",
    "StageSpec",
    "default_stages",
    "enrich_graph_environment",
    "environment_info",
    "run_answer_eval",
    "run_eval",
    "run_stage",
    "setup_probe",
    "setup_probe_graph",
    "summarize_answers",
    "teardown_probe",
]
