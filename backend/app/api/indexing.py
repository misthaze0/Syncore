"""Knowledge catalog, stored chunks, retrieval testing and incremental ingestion."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.core.doc_id import chunk_id
from app.core.milvus_client import milvus_manager
from app.rag import incremental
from app.rag.es_client import es_manager
from app.rag.retriever import capture_retrieval, retrieve
from app.rag.watcher import watcher_status

router = APIRouter(prefix="/index", tags=["indexing"])


class SourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=2000)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    mode: Literal["hybrid", "vector", "bm25", "hybrid_graph"] = "hybrid"
    rerank: bool | None = None
    source: str | None = Field(default=None, max_length=2000)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请输入要测试的问题")
        return value


def _known(source: str) -> incremental.FileEntry:
    entry = incremental.load_manifest().get("files", {}).get(source)
    if entry is None:
        raise HTTPException(status_code=404, detail="知识库里没有这个来源")
    return entry


@router.get("/status")
async def index_status() -> dict[str, Any]:
    info = await asyncio.to_thread(incremental.status)
    info["watcher"] = watcher_status()
    info["watch_dirs_exist"] = [
        {"dir": str(p), "exists": p.is_dir()} for p in settings.index_watch_dirs_list
    ]

    async def vector_count():
        try:
            info["milvus_chunks"] = await asyncio.wait_for(
                asyncio.to_thread(milvus_manager.count, strict=True), timeout=5,
            )
            info["vector_space"] = await asyncio.wait_for(
                asyncio.to_thread(milvus_manager.vector_space_state), timeout=5,
            )
        except Exception as exc:
            info["milvus_chunks"] = None
            info["milvus_error"] = str(exc) or "向量存储查询超时"

    async def keyword_count():
        info["es_docs"] = None
        if es_manager.enabled:
            try:
                info["es_docs"] = await asyncio.wait_for(es_manager.count(strict=True), timeout=5)
            except Exception as exc:
                info["es_error"] = str(exc) or "关键词存储查询超时"

    await asyncio.gather(vector_count(), keyword_count())
    issues = []
    if info.get("vector_space", {}).get("state") == "mismatch":
        issues.append("向量模型与现有索引不一致，请在检索模型设置中重建向量库")
    for key, label in (("milvus_chunks", "向量索引"), ("es_docs", "关键词索引")):
        if key == "es_docs" and not es_manager.enabled:
            continue
        if info[key] is None:
            issues.append(f"{label}暂时不可用")
        elif info[key] != info["tracked_chunks"]:
            issues.append(f"{label}块数与文档清单不一致")
    info["health"] = {"state": "warning" if issues else "healthy", "issues": issues}
    info["retrieval"] = {"mode": settings.RETRIEVAL_MODE, "rerank_enabled": settings.RERANK_ENABLED,
                         "graph_enabled": settings.GRAPH_ENABLED}
    return info


@router.get("/documents")
async def index_documents(
    q: str = Query("", max_length=200),
    state: Literal["all", "indexed", "warning", "error", "missing", "empty"] = "all",
    sort: Literal["recent", "name", "chunks"] = "recent",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    docs = await asyncio.to_thread(incremental.documents)
    summary = {"documents": len(docs), "chunks": sum(d["chunks"] for d in docs),
               "needs_attention": sum(bool(d["error"] or d["warnings"] or not d["on_disk"] or not d["chunks"]) for d in docs)}
    needle = q.strip().casefold()
    filtered = [d for d in docs if (not needle or needle in d["source"].casefold())
                and (state == "all" or (state == "missing" and not d["on_disk"]) or d["status"] == state)]
    if sort == "name":
        filtered.sort(key=lambda d: d["source"].casefold())
    elif sort == "chunks":
        filtered.sort(key=lambda d: (-d["chunks"], d["source"]))
    return {"documents": filtered[offset:offset + limit], "total": len(filtered),
            "limit": limit, "offset": offset, "summary": summary}


@router.get("/chunks")
async def document_chunks(
    source: str = Query(..., min_length=1, max_length=2000),
    offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    entry = _known(source)
    indices = list(range(offset, min(offset + limit, entry.chunks)))
    if not indices:
        return {"source": source, "chunks": [], "total": entry.chunks, "offset": offset, "warnings": []}
    rows: dict[int, dict[str, Any]] = {}
    warnings = []
    available = False
    try:
        hits = await asyncio.wait_for(asyncio.to_thread(
            milvus_manager.get_by_ids, [chunk_id(source, i) for i in indices], strict=True,
        ), timeout=6)
        available = True
        rows.update({int(hit["chunk_index"]): hit for hit in hits if hit.get("source") == source})
    except Exception:
        warnings.append("向量存储暂时不可用，已尝试从关键词索引读取")
    missing = [i for i in indices if i not in rows]
    if missing and es_manager.enabled:
        try:
            hits = await asyncio.wait_for(es_manager.mget_by_keys([(source, i) for i in missing], strict=True), timeout=6)
            available = True
            rows.update({int(hit["chunk_index"]): hit for hit in hits if hit.get("source") == source})
        except Exception:
            warnings.append("关键词存储暂时不可用")
    if not available:
        raise HTTPException(status_code=503, detail="无法读取已入库内容，请检查索引服务后重试")
    missing = [i for i in indices if i not in rows]
    if missing:
        warnings.append(f"本页有 {len(missing)} 个文本块缺失，可重新索引修复")
    return {"source": source, "chunks": [
        {"chunk_index": i, "text": rows[i].get("text", ""), "page": rows[i].get("page", 0)}
        for i in indices if i in rows
    ], "total": entry.chunks, "offset": offset, "warnings": warnings}


@router.post("/search")
async def test_retrieval(payload: SearchRequest) -> dict[str, Any]:
    if payload.source:
        _known(payload.source)
    started = time.perf_counter()
    scope = (lambda source: source == payload.source) if payload.source else None
    try:
        with capture_retrieval() as diagnostic:
            hits = await asyncio.wait_for(retrieve(
                payload.query, top_k=payload.top_k, mode=payload.mode,
                scope=scope, rerank=payload.rerank, source_filter=payload.source,
            ), timeout=35)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="检索超时，请检查模型服务后重试") from exc
    lanes = []
    for name in ("vector", "bm25", "graph"):
        enabled = ((name == "vector" and payload.mode != "bm25")
                   or (name == "bm25" and payload.mode != "vector" and settings.ES_ENABLED)
                   or (name == "graph" and payload.mode in {"hybrid", "hybrid_graph"} and settings.GRAPH_ENABLED))
        error = diagnostic["failures"].get(name)
        lanes.append({"name": name, "status": "disabled" if not enabled else "error" if error else "ok",
                      "hits": diagnostic["lane_counts"].get(name, 0), "error": error})
    warnings = [f"{lane['name']} 召回不可用：{lane['error']}" for lane in lanes if lane["status"] == "error"]
    if payload.mode == "bm25" and not settings.ES_ENABLED:
        warnings.append("关键词检索未启用，请在检索设置中启用或切换为混合检索")
    if diagnostic.get("rerank_requested") and hits and not diagnostic["reranked"]:
        warnings.append("本次未获得重排分数，已保留召回顺序")
    keys = ("source", "text", "page", "chunk_index", "retrieval", "recalled_by",
            "score", "rrf_score", "rerank_score", "vector_rank", "bm25_rank", "graph_rank")
    results = []
    for hit in hits:
        result = {key: hit[key] for key in keys if key in hit}
        for key in ("score", "rrf_score", "rerank_score"):
            if key in result and (result[key] is None or not math.isfinite(float(result[key]))):
                result[key] = None
        results.append(result)
    return {"query": payload.query, "mode": payload.mode, "results": results,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "lanes": lanes, "reranked": diagnostic["reranked"], "warnings": warnings}


@router.post("/documents/reindex")
async def document_reindex(payload: SourceRequest) -> dict[str, Any]:
    try:
        return await incremental.reindex_document(payload.source)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="知识库里没有这个来源") from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"重新索引失败：{exc}") from exc


@router.delete("/documents/{source:path}")
async def index_document_delete(source: str) -> dict[str, Any]:
    res = await incremental.remove_document(source)
    if not res.get("ok"):
        raise HTTPException(status_code=404 if res.get("not_found") else 502, detail=res.get("reason", "删除失败"))
    return res


@router.post("/scan")
async def index_scan(dry_run: bool = Query(False)) -> dict[str, Any]:
    try:
        if dry_run:
            plan = await asyncio.to_thread(incremental.scan)
            return {"ok": True, "dry_run": True, **incremental.plan_to_json(plan)}
        report = await incremental.scan_and_apply()
        if report.get("busy"):
            raise HTTPException(status_code=409, detail=report["reason"])
        return report
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"增量扫描失败：{exc}") from exc
