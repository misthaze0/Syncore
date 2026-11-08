"""
健康检查接口
"""

import logging

from fastapi import APIRouter

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# 服务版本的**唯一来源**：main.py 的 FastAPI(version=) 与根端点都引用它，
# pyproject.toml 的 version 需人工与之对齐（见 CHANGELOG「版本号与服务版本」）。
SERVICE_VERSION = "0.7.0"


@router.get("/info")
async def service_info():
    """
    服务身份：版本号 + 当前模型。

    ★ 为什么不复用 `/health`

    `/health` 会**逐个探活依赖**（Milvus / ES / Redis / Neo4j），任一不可达都要等满
    连接超时 —— 实测在依赖全不可用时需要 15 秒。而界面（侧栏的「你在用什么」）
    要的是几十毫秒就能答完的问题，不该被 4 个依赖的超时拖着。
    两者分工：`/health` 给运维看「谁能用」，`/info` 给界面看「我是什么」。
    这里**不做任何探测**，只读配置 —— 所以它永远快，也永远不会因为依赖挂了而失败。
    """
    from app.core.embedding_factory import embedding_info

    emb = embedding_info()
    return {
        "version": SERVICE_VERSION,
        "model": settings.LLM_MODEL,
        # 保留原字段名（界面与文档已经在读它），但值改成**当前后端实际在用的模型**：
        # 本地后端下报 `EMBEDDING_MODEL` 会显示一个根本没被调用过的模型名。
        "embedding_model": emb["model"],
        # 新增字段回答「这个向量是本地算的，还是调接口算的」——
        # 换后端会连带改变维度与是否需要重建 Milvus，值得一眼可见。
        "embedding_backend": emb["backend"],
        "embedding_dimension": emb["dimension"],
    }


@router.get("/health")
async def health_check():
    """服务健康检查（同时报告各依赖的可用状态）"""
    from app.agents.graph import get_graph
    from app.agents.tools.registry import tool_registry
    from app.core.embedding_factory import embedding_info
    from app.rag.reranker import reranker_status
    from app.rag.retriever import VALID_MODES

    dependencies: dict[str, str] = {}

    # Milvus
    #
    # ★ 为什么要带 `reason` 与 `credentials_configured`
    #
    # 这是四个依赖里唯一原本**把原因吞掉**的一个（只报 up/down）。而 Milvus 恰有
    # 两种长得一样的失败：进程没起、以及服务端开了认证而客户端没带（或带错）凭据。
    # 两者在 `dependencies.milvus == "down"` 上完全无法区分，只能去翻后端启动日志。
    # 如实转发原因，是「降级必须可见」这条原则在依赖层的最后一处空白
    # （前端展示与否是另一件事，接口不该先替它把信息丢掉）。
    milvus_info: dict = {"collection": settings.MILVUS_COLLECTION}
    try:
        from app.core.milvus_client import milvus_manager

        milvus_manager.client.list_collections()
        milvus_info["status"] = "up"
    except Exception as e:  # noqa: BLE001
        milvus_info["status"] = "down"
        milvus_info["reason"] = f"{type(e).__name__}: {e}"
    milvus_info["credentials_configured"] = bool(settings.MILVUS_USER)
    dependencies["milvus"] = milvus_info["status"]

    # Elasticsearch（Phase 3 关键词召回）
    es_info: dict = {"status": "disabled"}
    if settings.ES_ENABLED:
        try:
            from app.rag.es_client import es_manager

            es_info = await es_manager.health()
            dependencies["elasticsearch"] = es_info.get("status", "unknown")
        except Exception as e:  # noqa: BLE001
            dependencies["elasticsearch"] = "down"
            es_info = {"status": "down", "reason": str(e)}
    else:
        dependencies["elasticsearch"] = "disabled"

    # Redis
    try:
        from app.core.redis_client import get_redis

        dependencies["redis"] = "up" if await get_redis() else "down"
    except Exception:  # noqa: BLE001
        dependencies["redis"] = "down"

    # Neo4j（Phase 4b 图路召回）
    graph_info = await _graph_status()
    dependencies["neo4j"] = (
        "up" if graph_info.get("available")
        else ("disabled" if not graph_info.get("enabled") else "down")
    )

    mode = (settings.RETRIEVAL_MODE or "hybrid").strip().lower()
    if mode not in VALID_MODES:
        mode = "hybrid"

    return {
        "status": "ok",
        "service": "xjms-backend",
        "version": SERVICE_VERSION,
        "mode": "multi_agent" if get_graph() is not None else "rag_only",
        "tools_registered": tool_registry.stats()["total"],
        "dependencies": dependencies,
        "retrieval": {
            "mode": mode,
            "vector_top_k": settings.HYBRID_VECTOR_TOP_K,
            "bm25_top_k": settings.HYBRID_BM25_TOP_K,
            "rrf_k": settings.HYBRID_RRF_K,
            "bm25_index_docs": es_info.get("docs", 0),
            "rerank": reranker_status(),
            # 向量化后端（本地模型 or 外部接口）。放在 retrieval 里而不是别处，
            # 是因为它和精排一样是检索链路上的一个可替换环节 ——
            # 排查「召回变差了」时要能一眼看出这两处分别跑的是什么。
            "embedding": embedding_info(),
            "graph": graph_info,
            # 与 graph 同一套分工：`dependencies.milvus` 只给一个 up/down 的字，
            # 细节（连不上的原因、有没有配凭据、当前集合名）放这里。
            "milvus": milvus_info,
        },
        "mcp": _mcp_status(),
        "observability": _observability_status(),
        "evaluation": _evaluation_status(),
        "config": {
            "llm_model": settings.LLM_MODEL,
            "embedding_model": embedding_info()["model"],
            # ⚠️ 这是「LangGraph 多 Agent 模式」的开关，与图检索无关。
            # 图检索的开关在 retrieval.graph.enabled（GRAPH_ENABLED），
            # 两者名字接近但管的是完全不同的事，所以各自标明。
            "graph_enabled": settings.ENABLE_GRAPH,
        },
    }


async def _graph_status() -> dict:
    """
    图检索能力状态。

    与追踪模块同一条原则：图最危险的失效形态是「以为在跑、其实一路空」——
    检索不会报错，只是多跳证据永远进不来，而指标看起来"只是低了一点"。
    所以这里如实转发 `available` 与 `degraded_reason`，绝不吞掉原因。
    """
    try:
        from app.graph.store import graph_store

        return await graph_store.status()
    except Exception as e:  # noqa: BLE001
        return {
            "enabled": bool(getattr(settings, "GRAPH_ENABLED", False)),
            "available": False,
            "degraded_reason": f"{type(e).__name__}: {e}",
        }


def _observability_status() -> dict:
    """
    追踪能力状态。

    必须**如实**上报「未启用」与「降级原因」—— 追踪最危险的失效形态是
    「以为在采集、其实一条都没有」，等到需要排查时才发现没有数据。
    """
    try:
        from app.observability import observability_status

        return observability_status()
    except Exception as e:  # noqa: BLE001
        return {"enabled": False, "reason": f"{type(e).__name__}: {e}"}


def _evaluation_status() -> dict:
    """评估能力状态（Golden Set 是否就绪、答案判定后端是否可用）"""
    info: dict = {"golden_set": None, "judge": None}
    try:
        from app.eval import judge_status

        info["judge"] = judge_status()
    except Exception as e:  # noqa: BLE001
        info["judge"] = {"available": False, "reason": str(e)}

    try:
        info["golden_set"] = {
            "path": str(settings.golden_set_path),
            "exists": settings.golden_set_path.exists(),
        }
    except Exception as e:  # noqa: BLE001
        info["golden_set"] = {"exists": False, "reason": str(e)}
    return info


def _mcp_status() -> dict:
    """
    MCP 接入概况（细节在 `/api/mcp`，这里只给"接了几个、通不通"）。

    健康检查会被各种面板反复拉，把每个 server 的 command/args/工具 schema 全塞进来
    等于把配置清单重复传一遍；出问题时该看的是 `/api/mcp` 的失败原因与重试窗口。
    """
    try:
        from app.agents.tools.mcp_adapter import mcp_manager

        status = mcp_manager.status()
        return {
            "enabled": status["enabled"],
            "connected": status["connected"],
            "configured": len(status["servers"]),
            "tools": len(status["tools"]),
            "servers": [
                {
                    "name": s["name"],
                    "transport": s["transport"],
                    "connected": s["connected"],
                    "tools": len(s["tools"]),
                    "error": s["error"],
                }
                for s in status["servers"]
            ],
        }
    except Exception:  # noqa: BLE001
        return {"enabled": settings.MCP_ENABLED, "servers": [], "tools": 0}
