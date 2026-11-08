"""
检索器 —— Phase 3 混合检索编排层

★ 检索链路（三路召回 + 融合 + 精排）

    用户 query
      ├── 向量召回（Milvus + Embedding）        → Top-20，抓「语义相近」
      ├── 关键词召回（Elasticsearch BM25）      → Top-20，抓「字面精确」
      └── 图召回（Neo4j 实体关系，多跳）        → 顺着关系链走到零字面重合的块
                    ↓
              RRF 融合（只看排名，不看分数量纲）
                    ↓
              Rerank 精排（Cross-Encoder 联合编码，重排 Top-5）
                    ↓
              多跳证据保底槽（把仅图路召回的块补进末尾，见 backfill_multihop）
                    ↓
              注入 Prompt

每一阶段都是**可选增益**而非必需环节：
- ES 挂了 → 退化为纯向量召回（等价 Phase 1 行为）
- Embedding 挂了 → 退化为纯 BM25
- Rerank 挂了 → 直接返回 RRF 顺序
- Neo4j 挂了 → 退化为两路融合，多跳证据拿不到（这部分能力确实会丢失）

因此单点故障永远不会让「问答」整个失效，只会让召回质量下降。
这也是「依赖降级」这条架构不变量在检索层的落地。

★ 为什么两路都要

向量召回解决「换了说法也能搜到」：
    问「后端重启会丢记忆吗」→ 命中「会话状态持久化在 Redis」的段落
    （字面完全不同，但语义一致）

BM25 解决「换了说法就搜不到」：
    问「项目编号 X-123」→ 向量模型会把编号编码成没有区分度的向量，
    而 BM25 能按字面精确命中
    （这类「ID / 错误码 / 配置项名 / 英文缩写」是纯语义检索的天然盲区）

两者失效场景互补，这正是混合检索的价值来源。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, List

from app.config import settings
from app.core.embedding_factory import get_embedding
from app.core.milvus_client import milvus_manager
from app.observability import get_current_recorder, now_ms
from app.rag.es_client import es_manager
from app.rag.fusion import hit_identity, rrf_fuse
from app.rag.reranker import rerank_hits

logger = logging.getLogger(__name__)

# Per-request diagnostics are shared by child coroutines, isolated between requests.
_diagnostics: ContextVar[Dict[str, Any] | None] = ContextVar("retrieval_diagnostics", default=None)


@contextmanager
def capture_retrieval():
    data: Dict[str, Any] = {"failures": {}, "lane_counts": {}, "reranked": False}
    token = _diagnostics.set(data)
    try:
        yield data
    finally:
        _diagnostics.reset(token)


def _lane_failure(lane: str, error: Any) -> None:
    data = _diagnostics.get()
    if data is not None:
        data["failures"][lane] = str(error)


async def _no_lane() -> List[Dict[str, Any]]:
    """「这一路本轮不走」的占位协程：让 `asyncio.gather` 的三个位置保持同构
    （否则要在调用处写 if/else 分支，容易在后面改动时漏掉一路）"""
    return []

#: 单路模式 + 组合模式。
#:
#: - `vector` / `bm25` / `hybrid` 是 Phase 3 就有的三种，前两个只用于对照实验
#: - `graph` 是 Phase 4b 新增的单路（只走图），用于证明“图能到达另外两路够不到的地方”
#: - `hybrid_graph` 是**显式开启图路的混合检索**。之所以不直接复用 `hybrid`，
#:   是因为评估脚本按 `(mode, rerank)` 索引阶段（见 `run_eval.assert_baseline`），
#:   若图路阶段也叫 hybrid，就会把 Phase 3 的基线阶段覆盖掉，
#:   「混合+图」与「混合」的数字再也分不开。独立模式名让两种口径并存互不干扰。
VALID_MODES = {"hybrid", "vector", "bm25", "graph", "hybrid_graph"}


# ---------- 追踪埋点 ----------
# 为什么埋在这里而不是靠上层事件流：
# SSE 的 `sources` 事件只带最终结果，**不带各阶段耗时与候选条数**。
# 而「BM25 路召回了 0 条」正是 ES 索引为空的直接证据，「精排花了 800ms」
# 正是需要优化的信号 —— 这些只有检索层自己知道。
# 埋点用 get_current_recorder() 取当前追踪器，未启用追踪时为 None，直接跳过，
# 不产生任何额外开销（连结果字典都不构造）。
def _brief_hits(hits: List[Dict[str, Any]], limit: int = 10) -> List[Dict[str, Any]]:
    """检索结果的精简视图（完整 chunk 正文对追踪没有价值，只会撑爆 JSONL）"""
    out: List[Dict[str, Any]] = []
    for hit in hits[:limit]:
        out.append({
            "source": hit.get("source"),
            "score": _round_or_none(hit.get("score"), 4),
            "retrieval": hit.get("retrieval"),
            "recalled_by": list(hit.get("recalled_by") or []),
            "rrf_score": _round_or_none(hit.get("rrf_score"), 6),
            "rerank_score": _round_or_none(hit.get("rerank_score"), 6),
        })
    return out


def _record_retrieval(
    query: str,
    mode: str,
    *,
    t0: float,
    lane_counts: Dict[str, int],
    fused_count: int | None,
    reranked: bool,
    results: List[Dict[str, Any]] | None = None,
    backfilled: int = 0,
    scope_filtered: int | None = None,
) -> None:
    """记录一次检索的耗时、各阶段条数与最终命中明细"""
    diagnostic = _diagnostics.get()
    if diagnostic is not None:
        diagnostic.update(lane_counts=lane_counts, fused_count=fused_count,
                          rerank_requested=reranked,
                          reranked=any(h.get("rerank_score") is not None for h in (results or [])),
                          scope_filtered=scope_filtered or 0)
    rec = get_current_recorder()
    if rec is None:
        return
    rec.record(
        "retrieve",
        kind="retriever",
        duration_ms=round(now_ms() - t0, 2),
        input=query,
        output=_brief_hits(results or []),
        mode=mode,
        vector_hits=lane_counts.get("vector", 0),
        bm25_hits=lane_counts.get("bm25", 0),
        graph_hits=lane_counts.get("graph", 0),
        # 多跳保底补入的条数：为 0 时说明图路这一轮没有提供独有证据。
        # 它是判断“图检索到底有没有在起作用”的唯一直接指标。
        graph_backfilled=backfilled,
        fused_hits=fused_count,
        returned=len(results or []),
        reranked=reranked,
        # ★ 被项目边界筛掉的条数（None = 本轮没有边界）。
        # 没有它，「库里明明有却检索不到」就完全没有线索 —— 后过滤是静默丢弃，
        # 唯一的证据只能来自这里（ADR 0022 后果一节）。
        scope_filtered_count=scope_filtered,
    )


# ---------- 单路召回 ----------
async def retrieve_vector(query: str, top_k: int, source: str | None = None) -> List[Dict[str, Any]]:
    """
    向量召回（语义路）。

    Embedding 或 Milvus 不可用时返回空列表 —— 由调用方决定如何降级，
    本函数不抛异常（检索层统一「宁可少一路，不可整条挂」）。
    """
    try:
        query_vec = await get_embedding(query)
    except Exception as e:  # noqa: BLE001
        _lane_failure("vector", e)
        logger.warning("查询向量化失败，跳过向量召回: %s", e)
        return []

    if not query_vec:
        _lane_failure("vector", "向量化服务返回了空向量")
        return []

    try:
        filters = {"source": source} if source is not None else {}
        hits = await milvus_manager.search(query_vec, top_k=top_k, **filters)
    except Exception as e:  # noqa: BLE001
        _lane_failure("vector", e)
        logger.warning("Milvus 检索失败，跳过向量召回: %s", e)
        return []

    for hit in hits:
        hit["retrieval"] = "vector"
    return hits


async def retrieve_bm25(query: str, top_k: int, source: str | None = None) -> List[Dict[str, Any]]:
    """关键词召回（BM25 路）。ES 不可用时返回空列表。"""
    if not settings.ES_ENABLED:
        return []
    try:
        filters = {"source": source} if source is not None else {}
        hits = await es_manager.search(query, top_k=top_k, **filters)
        if not hits and es_manager._unavailable_reason:
            _lane_failure("bm25", es_manager._unavailable_reason)
        return hits
    except Exception as e:  # noqa: BLE001
        _lane_failure("bm25", e)
        logger.warning("ES 检索失败，跳过关键词召回: %s", e)
        return []


async def retrieve_graph_lane(query: str, top_k: int) -> List[Dict[str, Any]]:
    """
    图召回（多跳路，Phase 4b）。Neo4j 不可用 / 图为空 / 未启用时返回空列表。

    单独包一层而不是直接调 `app.graph.retrieve_graph`，是为了让「图这一路」
    与另外两路在 superfície 上完全同构（同样的签名、同样的降级语义），
    也便于测试里打桩替换。

    ★ `GRAPH_ENABLED` 的判断放在哪一层，以及为什么这里要额外告警

    开关真正的判断在 `app.graph.retriever.retrieve_graph` 的第一行
    （`if not graph_store.enabled: return []`）——**必须**放在那里，
    因为它是连接层的守卫，`entity_index()` / `neighbors()` 这些入口都依赖它。

    代价是：一个显式请求图路的调用（`retrieve(mode="graph")`，
    或评估的图路阶段）在开关关闭时会拿到一个**空列表** ——
    而"空列表"同时也是"图没建好""Neo4j 连不上""查询里没有已知实体"的症状。
    四种原因共用一个输出且都不报错，是最难查的一类假失败。

    所以这里不重复判断开关，而是**把这种组合显式喊出来**：
    只有当「模式明确要图」而「全局开关是关的」时才告警 ——
    此时调用方的意图与配置矛盾，一定是其中之一错了，不该静默。
    （评估的图路阶段会自己在 `run_stage` 里把开关打开，所以正常情况下看不到这条告警。）
    """
    try:
        from app.graph.retriever import retrieve_graph
        from app.graph.store import graph_store

        if not graph_store.enabled:
            logger.warning(
                "请求了图路但 GRAPH_ENABLED=false，这一路将返回空结果："
                "若这是评估/验收的图路阶段，说明调用方忘了临时打开开关；"
                "若是有意关闭，请忽略本条"
            )
        hits = await retrieve_graph(query, top_k)
        if graph_store.enabled and graph_store.degraded_reason:
            _lane_failure("graph", graph_store.degraded_reason)
        return hits
    except Exception as e:  # noqa: BLE001
        _lane_failure("graph", e)
        logger.warning("图检索失败，跳过图召回: %s", e)
        return []


# ---------- 多跳证据保底槽 ----------
def backfill_multihop(
    results: List[Dict[str, Any]],
    graph_hits: List[Dict[str, Any]],
    *,
    top_k: int,
    slots: int,
) -> List[Dict[str, Any]]:
    """
    把「仅被图路召回、且跳数 ≥ 1」的候选补进最终结果。

    ★ 为什么必须有这个槽（这是 GraphRAG 在本项目里真正的落点）

    RRF 的分数是 `Σ w/(k+rank)`，也就是**按被几路命中投票**。
    一条只被图路召回的块只有 1 票，而任何被两路共识命中的块有 2 票 ——
    在 k=60 这种极平的加权下，1 票永远赢不了 2 票，**与它排在第几名无关**。

    这不是 bug，是 RRF 的设计（见 `decisions/0006` 与不变量 11）。
    代价是：多跳证据（查询与它零字面重合、只能顺着关系链到达的块）
    在纯 RRF 下**结构性地不可能**进入最终结果。图检索会退化成
    "建了、也在跑、但用户永远看不到它的贡献"。

    所以这里给它一个**保底名额**，而不是指望它靠投票挤进去：

    - 只补 `hops >= 1` 的候选（hop=0 的是"直接提到查询实体"的块，
      那类块本来就在向量/BM25 的射程内，占保底名额没有意义）
    - 只补其它路**完全没有**召回的块（避免与已有结果重复）
    - 追加在**末尾**，占用最低优先级的上下文槽位 —— 多跳证据是补充信息，
      不该顶掉共识度更高的直接命中
    - 条数由 `GRAPH_MIN_MULTIHOP` 控制，设为 0 即完全关闭（对照实验用）

    Args:
        results: 精排后的最终结果（长度 ≤ top_k）
        graph_hits: 图路召回的原始候选（已按图路得分排序）
        top_k: 最终保留的条数
        slots: 保底名额上限

    Returns:
        新列表（不改原列表），长度仍 ≤ top_k
    """
    if slots <= 0 or not graph_hits or not results:
        return results

    existing = {hit_identity(hit) for hit in results}
    picked: List[Dict[str, Any]] = []
    for hit in graph_hits:
        if len(picked) >= slots:
            break
        if int(hit.get("graph_hops", 0) or 0) < 1:
            continue
        if hit_identity(hit) in existing:
            continue
        if not str(hit.get("text", "")).strip():
            continue  # 回填正文失败的候选塞进去只会让精排拿到空串
        picked.append({**hit, "backfilled": True, "retrieval": "graph"})

    if not picked:
        return results

    keep = max(0, top_k - len(picked))
    logger.info(
        "多跳证据保底：补入 %s 条仅图路召回的候选（跳数=%s）",
        len(picked), [h.get("graph_hops") for h in picked],
    )
    return results[:keep] + picked


def _apply_scope(
    hits: List[Dict[str, Any]],
    scope: Callable[[str], bool] | None,
) -> tuple[List[Dict[str, Any]], int]:
    """
    按项目边界筛掉越界命中，返回 (留下的, 被筛掉的条数)。

    `scope is None` 表示本轮没有边界（主对话、或未配置映射的员工）—— 原样返回，
    不复制列表也不产生任何开销。

    ★ 判定键只能是 `source`（ADR 0022 硬约束）：图路命中不带 doc_id，
    `fusion.hit_identity` 给它的是 `src:` 开头的键。按 doc_id 过滤会让每一条
    越界图命中全部漏网，而且是「看起来在正常工作」那种漏法。
    """
    if scope is None or not hits:
        return hits, 0
    kept = [hit for hit in hits if scope(str(hit.get("source", "")))]
    return kept, len(hits) - len(kept)


# ---------- 混合检索 ----------
async def retrieve_hybrid(
    query: str,
    top_k: int,
    *,
    rerank: bool | None = None,
    graph: bool | None = None,
    scope: Callable[[str], bool] | None = None,
    source_filter: str | None = None,
) -> List[Dict[str, Any]]:
    """
    混合检索：多路召回 → RRF 融合 →（按项目边界过滤）→ 精排 → 多跳保底。

    Args:
        query: 用户查询
        top_k: 最终返回条数
        rerank: 是否精排；None 时取 settings.RERANK_ENABLED
        graph: 是否带上图路；None 时取 settings.GRAPH_ENABLED。
               ★ 之所以做成显式参数而不是只读配置：评估要能跑
               「同一条代码路径、只差图和没图」的对照实验，
               而评估脚本会在运行中切换 settings（见 run_eval.run_stage）。
        scope: 知识边界的判定函数（`source -> bool`），由调用方按数字员工所属项目
               构造（见 `workspace/knowledge_scope.makes_scope`）。None = 不设边界。
               ★ 过滤点必须落在「融合之后、精排之前」：三条通道这时都已经汇进
               `fused`，一处过滤覆盖三路；放在三处各写一遍就是三个必然漂移的判据。
               ★ 也必须落在多跳保底之前：`backfill_multihop` 读的是**原始** `graph_hits`，
               顺序反了会把已经过滤掉的越界命中重新注回结果尾部。
               过滤会减少召回，所以 scope 生效时三路各超采 3 倍再筛 ——
               否则「库里明明有，员工却说找不到」。

    Returns:
        结果列表，每条含:
        - 基础字段: text / source / page / chunk_index
        - `score`: 最终排序分（精排分 > RRF 分）
        - `retrieval`: "hybrid" | "vector" | "bm25" | "graph"（该条被哪些路命中）
        - 可观测字段: `recalled_by` / `rrf_score` / `rerank_score` /
          `vector_rank` / `bm25_rank` / `graph_rank` / `graph_hops` / `graph_path`
    """
    _hybrid_t0 = now_ms()
    use_graph = settings.GRAPH_ENABLED if graph is None else bool(graph)
    # 有边界时三路各超采 3 倍再筛：过滤是减法，不超采的话筛完就不够 top_k 条了，
    # 而「库里有却答找不到」会被用户当成检索坏了，比越界更常被上报。
    lane_scale = 3 if scope is not None else 1

    # ★ 三路**并发**召回，不是依次 await。
    #
    # 三路之间没有任何依赖：各自查各自的库、各自给出各自的排名，融合在最后一步。
    # 串行 await 等于把三段耗时**相加**（本机实测：向量 2.8s + BM25 9.3s + 图 0.01s
    # ≈ 12.1s，而三者并行只需要最慢那一路的时间），而用户等的是「和」不是「最大」。
    #
    # 单路超时是必需的兜底：依赖不可用时客户端会一直等到自己的连接超时
    # （ES 默认 10s），一路慢就整轮问答被拖住。超时按**空结果**处理 ——
    # 与「这一路挂了」是同一个语义（不变量 14：所有依赖都要能降级）。
    lane_budget = max(0.1, float(getattr(settings, "RETRIEVAL_LANE_TIMEOUT_SECONDS", 5.0)))

    def _mark_es_timeout() -> None:
        """
        这一路超时也记成「ES 不可用」。

        ★ 为什么不能只靠 `ensure_index` 自己记（踩过的坑）

        连接失败本来会由 `ESManager.ensure_index` 捕获并进入冷却。但单路预算是用
        `asyncio.wait_for` 实现的 —— 它到点会**取消**那个协程，于是
        `ensure_index` 的 `except` 根本来不及执行，冷却装不上。

        后果：ES 若在运行中挂掉（启动探针已经跑过的那种情况不覆盖它），
        每一轮问答都会重新烧满整个单路预算，永远等不到「第二次就不试了」。
        实测（独立进程、无启动探针）连续三次检索各 5.02s，冷却一次都没生效。

        所以：**超时本身就是一个「这路现在不通」的证据，必须由超时的这一侧记下来。**
        """
        try:
            es_manager.mark_unavailable(f"召回超时（>{lane_budget:.1f}s）")
        except Exception:  # noqa: BLE001
            pass

    def _mark_graph_timeout() -> None:
        """同上，图路：`_ensure_driver` 到点被取消时也要留下记录"""
        try:
            from app.graph.store import graph_store

            graph_store.mark_unavailable(f"召回超时（>{lane_budget:.1f}s）")
        except Exception:  # noqa: BLE001
            pass

    async def _guarded(
        label: str,
        coro: Any,
        on_timeout: Any = None,
    ) -> List[Dict[str, Any]]:
        """单路召回的统一外壳：超时/异常都退化为空列表，绝不把异常抛给融合层"""
        try:
            return await asyncio.wait_for(coro, timeout=lane_budget)
        except asyncio.TimeoutError:
            _lane_failure({"向量": "vector", "BM25": "bm25", "图路": "graph"}.get(label, label), "召回超时")
            logger.warning("%s 召回超时（>%.1fs），本轮按空结果处理", label, lane_budget)
            if on_timeout is not None:
                on_timeout()
            return []
        except Exception as e:  # noqa: BLE001
            _lane_failure({"向量": "vector", "BM25": "bm25", "图路": "graph"}.get(label, label), e)
            logger.warning("%s 召回失败，本轮按空结果处理: %s", label, e)
            return []

    # 向量这一路不挂超时回调：它的慢来自 embedding 接口（一次性网络调用，跳不掉），
    # 而 Milvus 连不上时是**立刻**失败的（连接被拒），不会烧满预算。
    vector_hits, bm25_hits, graph_hits = await asyncio.gather(
        _guarded("向量", retrieve_vector(query, max(top_k, settings.HYBRID_VECTOR_TOP_K) * lane_scale, source_filter)),
        _guarded("BM25", retrieve_bm25(query, max(top_k, settings.HYBRID_BM25_TOP_K) * lane_scale, source_filter), on_timeout=_mark_es_timeout),
        _guarded("图路", retrieve_graph_lane(query, settings.GRAPH_TOP_K * lane_scale), on_timeout=_mark_graph_timeout)
        if use_graph else _no_lane(),
    )

    # 各路的**原始**召回条数，先存下来：后面会按项目边界就地筛掉越界命中，
    # 计量里的 `*_hits` 说的是「这一路捞回多少」，不是「筛完还剩多少」。
    raw_lane_counts = {
        "vector": len(vector_hits), "bm25": len(bm25_hits), "graph": len(graph_hits),
    }

    if not vector_hits and not bm25_hits and not graph_hits:
        _record_retrieval(
            query, "hybrid", t0=_hybrid_t0,
            lane_counts=raw_lane_counts,
            fused_count=0, reranked=False, results=[],
        )
        return []

    fused = rrf_fuse(
        {"vector": vector_hits, "bm25": bm25_hits, "graph": graph_hits},
        k=settings.HYBRID_RRF_K,
        weights={
            "vector": settings.HYBRID_VECTOR_WEIGHT,
            "bm25": settings.HYBRID_BM25_WEIGHT,
            "graph": settings.GRAPH_WEIGHT,
        },
    )

    # ★ 项目边界：融合之后、精排之前（ADR 0022）。越界候选连精排成本一起省掉。
    fused, scope_filtered = _apply_scope(fused, scope)
    if scope is not None and graph_hits:
        # 必须连原始图路命中一起筛：多跳保底读的是 `graph_hits` 本身，
        # 只筛 fused 的话越界命中会在精排之后被保底槽重新注回结果尾部。
        graph_hits, _ = _apply_scope(graph_hits, scope)

    # 单路有结果时另一路为空不影响融合正确性：RRF 会自然退化为该路的排名。
    # 但只有一路时排名信息与原始分等价，精排仍有价值（Cross-Encoder 更准），
    # 所以这里不做「只有一路就跳过精排」的优化。
    should_rerank = settings.RERANK_ENABLED if rerank is None else rerank
    if should_rerank:
        results = await rerank_hits(query, fused, top_k=top_k)
    else:
        results = fused[:top_k]

    # ★ 多跳证据保底：必须在精排之后做。
    # 放在精排之前的话，精排会把这些“孤立”的候选按相关性重新排序，
    # 大概率又把它们排到末尾 —— 那就等于没补。
    backfilled = 0
    if use_graph and graph_hits:
        results = backfill_multihop(
            results, graph_hits,
            top_k=top_k, slots=settings.GRAPH_MIN_MULTIHOP,
        )
        backfilled = sum(1 for hit in results if hit.get("backfilled"))

    for hit in results:
        # `score` 统一为「最终用于排序的分」，让上层与前端不必关心是哪种得分
        if hit.get("rerank_score") is not None:
            hit["score"] = hit["rerank_score"]
        else:
            hit["score"] = hit.get("rrf_score", 0.0)

    _record_retrieval(
        query, "hybrid",
        t0=_hybrid_t0,
        lane_counts=raw_lane_counts,
        fused_count=len(fused) + scope_filtered,
        reranked=should_rerank,
        results=results,
        backfilled=backfilled,
        scope_filtered=scope_filtered if scope is not None else None,
    )

    logger.info(
        "混合检索完成: query=%r | 向量=%s 条 | BM25=%s 条 | 图=%s 条 | 融合=%s 条 | 边界筛除=%s 条 | 返回=%s 条 | 精排=%s | 多跳保底=%s",
        query[:30], raw_lane_counts["vector"], raw_lane_counts["bm25"], raw_lane_counts["graph"],
        len(fused) + scope_filtered, scope_filtered, len(results), should_rerank, backfilled,
    )
    return results


# ---------- 统一入口 ----------
async def _finalize_single_lane(
    query: str,
    hits: List[Dict[str, Any]],
    top_k: int,
    lane: str,
    rerank: bool | None = None,
) -> List[Dict[str, Any]]:
    """
    单路模式（vector / bm25）的收尾：可选精排 + 补齐可观测字段。

    单路模式下没有 RRF，所以 `rrf_score` 缺省，`score` 取精排分（有精排时）
    或该路原始分。字段结构与混合模式保持一致，前端不必区分模式做特判。
    """
    if not hits:
        return []

    results = hits
    if settings.RERANK_ENABLED if rerank is None else rerank:
        results = await rerank_hits(query, hits, top_k=top_k)
    else:
        results = hits[:top_k]

    for hit in results:
        hit.setdefault("recalled_by", [lane])
        hit.setdefault("retrieval", lane)
        if hit.get("rerank_score") is not None:
            hit["score"] = hit["rerank_score"]
        elif hit.get("rrf_score") is not None:
            hit["score"] = hit["rrf_score"]
        # 无精排时 score 保持该路原始分（向量余弦 / BM25 原始分）
    return results


async def retrieve(
    query: str,
    top_k: int = 5,
    *,
    mode: str | None = None,
    graph: bool | None = None,
    scope: Callable[[str], bool] | None = None,
    rerank: bool | None = None,
    source_filter: str | None = None,
) -> List[Dict[str, Any]]:
    """
    检索统一入口。

    Args:
        query: 用户查询
        top_k: 返回条数
        mode: 检索模式，缺省取 settings.RETRIEVAL_MODE
              - "hybrid": 向量 + BM25（+ 图，若 GRAPH_ENABLED）→ RRF → 精排
              - "hybrid_graph": 强制带上图路的三路融合（评估对照用）
              - "graph": 只走图（多跳），用于证明图能到达单跳检索够不到的地方
              - "vector": 只走向量（Phase 1 行为，用于对照实验）
              - "bm25":   只走关键词（用于验证精确匹配场景）
        graph: **按调用**覆盖图路开关（仅 hybrid 模式生效）。
              `None` 取 `settings.GRAPH_ENABLED`；`False` 时这一轮退化为两路融合。
              它的用途是会话级选项（`ChatRequest.graph`）——
              默认由配置决定，需要时能临时关掉，而不必改 `.env` 重启。
        scope: 知识边界判定函数 `source -> bool`（True = 可见）。
              `None` = 不设边界（主 Agent / 全局检索的默认）。
              由 `app.workspace.knowledge_scope.makes_scope(workspace_id)` 生成，
              数字员工发起的检索会带上了它。见下方 `_apply_scope` 的时序说明。

    Returns:
        [{"text", "source", "page", "chunk_index", "score", "retrieval", ...}, ...]
    """
    if not query.strip() or top_k <= 0:
        return []
    if source_filter is not None:
        original_scope = scope
        scope = lambda source: source == source_filter and (original_scope is None or original_scope(source))
    active_mode = (mode or settings.RETRIEVAL_MODE or "hybrid").strip().lower()
    if active_mode not in VALID_MODES:
        logger.warning("未知的检索模式 '%s'，回退为 hybrid", active_mode)
        active_mode = "hybrid"

    # 有边界时按路多召回一些：过滤是「后过滤」（融合后、精排前），
    # 用 top_k 原值去检会被边界筛掉一大截、剩下不够精排喂给模型的量。
    # 倍数取 3 —— 再高就要开始为「边界外资料占满召回」付 embedding 费了。
    lane_scale = 3 if scope is not None else 1

    if active_mode == "vector":
        t0 = now_ms()
        hits = await retrieve_vector(query, max(top_k, settings.HYBRID_VECTOR_TOP_K) * lane_scale, source_filter)
        hits, scope_filtered = _apply_scope(hits, scope)
        results = await _finalize_single_lane(query, hits, top_k, "vector", rerank)
        _record_retrieval(query, "vector", t0=t0, lane_counts={"vector": len(hits) + scope_filtered},
                          fused_count=None, reranked=settings.RERANK_ENABLED if rerank is None else rerank, results=results,
                          scope_filtered=scope_filtered if scope is not None else None)
        return results

    if active_mode == "bm25":
        t0 = now_ms()
        hits = await retrieve_bm25(query, max(top_k, settings.HYBRID_BM25_TOP_K) * lane_scale, source_filter)
        hits, scope_filtered = _apply_scope(hits, scope)
        results = await _finalize_single_lane(query, hits, top_k, "bm25", rerank)
        _record_retrieval(query, "bm25", t0=t0, lane_counts={"bm25": len(hits) + scope_filtered},
                          fused_count=None, reranked=settings.RERANK_ENABLED if rerank is None else rerank, results=results,
                          scope_filtered=scope_filtered if scope is not None else None)
        return results

    if active_mode == "graph":
        # 单走图路。开关仍由 `retrieve_graph` 把关（连接层守卫），
        # 但模式名本身就是「我要图」的显式意图，所以开关若为关，
        # `retrieve_graph_lane` 会打一条告警而不是静默返回空 ——
        # 否则“空结果”的四种成因（未启用 / 没建图 / 连不上 / 无匹配实体）
        # 会共用同一个症状。评估的图路阶段由 `run_stage` 自行打开开关。
        t0 = now_ms()
        hits = await retrieve_graph_lane(query, max(top_k, settings.GRAPH_TOP_K) * lane_scale)
        hits, scope_filtered = _apply_scope(hits, scope)
        results = await _finalize_single_lane(query, hits, top_k, "graph", rerank)
        _record_retrieval(query, "graph", t0=t0, lane_counts={"graph": len(hits) + scope_filtered},
                          fused_count=None, reranked=settings.RERANK_ENABLED if rerank is None else rerank, results=results,
                          scope_filtered=scope_filtered if scope is not None else None)
        return results

    if active_mode == "hybrid_graph":
        return await retrieve_hybrid(query, top_k, graph=True, scope=scope, rerank=rerank, source_filter=source_filter)

    return await retrieve_hybrid(query, top_k, graph=graph, scope=scope, rerank=rerank, source_filter=source_filter)


def serialize_sources(hits: List[Dict[str, Any]], preview_chars: int = 80) -> List[Dict[str, Any]]:
    """
    把检索结果转成 SSE `sources` 事件的内容。

    前后端唯一契约：字段一旦新增就要同步 `frontend/src/types/index.ts`。
    Phase 3 新增的字段用于在 UI 上展示「这条来源是靠什么召回的、精排给了多少分」，
    让混合检索的效果对用户可见 —— 否则链路再复杂，界面上也只是一个文件名。

    Args:
        hits: `retrieve()` 的返回值
        preview_chars: 每条附带的内容预览长度（0 表示不带）

    Returns:
        [{"source", "page", "score", "retrieval", "recalled_by",
          "rrf_score", "rerank_score", "vector_rank", "bm25_rank", "preview"}, ...]
    """
    out: List[Dict[str, Any]] = []
    for hit in hits:
        score = hit.get("score", 0.0)
        try:
            score = round(float(score), 4)
        except (TypeError, ValueError):
            score = 0.0

        item: Dict[str, Any] = {
            "source": hit.get("source", ""),
            "page": hit.get("page", 0),
            "score": score,
            # 召回来源：hybrid（多路命中）/ vector / bm25 / graph
            "retrieval": hit.get("retrieval", "vector"),
            "recalled_by": list(hit.get("recalled_by") or []),
            "rrf_score": _round_or_none(hit.get("rrf_score"), 6),
            "rerank_score": _round_or_none(hit.get("rerank_score"), 6),
            "vector_rank": hit.get("vector_rank"),
            "bm25_rank": hit.get("bm25_rank"),
            # ---------- Phase 4b：图路可解释字段 ----------
            # 「这条来源是顺着哪条关系、走几跳找过来的」必须能被展示。
            # 多跳检索如果不能解释自己怎么走到的，用户无法判断它是否可信 ——
            # 而不可信的召回会被当成噪声，功能等于没做。
            "graph_rank": hit.get("graph_rank"),
            "graph_hops": hit.get("graph_hops"),
            "graph_path": list(hit.get("graph_path") or []),
            "graph_relations": list(hit.get("graph_relations") or []),
            # 该条是「多跳证据保底」补进来的（非共识命中，优先级最低）
            "backfilled": bool(hit.get("backfilled")),
        }
        if preview_chars > 0:
            text = str(hit.get("text", "")).replace("\n", " ").strip()
            item["preview"] = text[:preview_chars] + ("…" if len(text) > preview_chars else "")
        out.append(item)

    return out


def _round_or_none(value: Any, digits: int) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


__all__ = [
    "retrieve",
    "retrieve_vector",
    "retrieve_bm25",
    "retrieve_graph_lane",
    "retrieve_hybrid",
    "serialize_sources",
    "VALID_MODES",
]
