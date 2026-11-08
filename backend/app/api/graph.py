"""
知识图谱接口 —— 查看图状态 + 显式触发构建

★ 为什么构建必须是一个**显式**入口（而不是像增量索引那样能开监听）

构建图要对**每一块**文档打一次模型抽取三元组（见 `graph/extractor.py`），
成本与知识库块数成正比。把它挂到文件监听上意味着「改一个文件 → 静默花掉一轮 token」，
而且花钱的规模取决于目录里有多少文件 —— 这类开销必须是 opt-in。

与 `INDEX_WATCH_ENABLED` 是同一套取舍：**主动花钱的开关一律默认关**。
所以这里只给两个显式入口：本接口与 `scripts/build_graph.py`。

★ 为什么要有并发锁

一次构建包含「清空 space → 逐块抽取（几十秒~几分钟）→ 写入」。
两个构建交错执行时，后一个的清空会删掉前一个刚写进去的关系，
而两边都会报 `ok` —— 结果是图里只留下半份数据，且没有任何错误信息。

增量索引那边用 `_scan_lock` 处理同一类问题（见 `rag/incremental.py`），
这里用同一套做法：拿到锁才执行，拿不到就如实说"正在进行中"（HTTP 200，不是错误）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/graph", tags=["graph"])

#: 同一进程内串行化构建（理由见模块头注释）
_build_lock = asyncio.Lock()


@router.get("/status")
async def graph_status() -> Dict[str, Any]:
    """
    图能力状态：开关 / 连通性 / 降级原因 / 统计 / 当前命名空间。

    必须**如实**上报 `available` 与 `degraded_reason`：图最危险的失效形态是
    「以为在跑、其实一路空」—— 检索不报错，只是多跳证据永远进不来。
    （与 `_observability_status` 是同一条原则。）

    ⚠️ `space` 是当前向量集合名（图跟着集合名走）。评估跑批期间如果恰好来查，
    看到的会是探针空间 `_eval_probe` —— 这不是 bug，是刻意的隔离设计。
    """
    from app.graph.store import graph_store

    info = await graph_store.status()
    info["backend"] = "neo4j"
    # 与 /api/index/status 一样，把「图里有多少」与「库里有多少」放在一起，
    # 两个数字对不上通常意味着只跑了索引没跑构建（或反之）。
    try:
        from app.core.milvus_client import milvus_manager

        info["milvus_chunks"] = milvus_manager.count()
    except Exception as e:  # noqa: BLE001
        info["milvus_chunks"] = None
        info["milvus_error"] = str(e)
    return info


@router.post("/build")
async def graph_build(
    clear: bool = Query(
        True,
        description="构建前是否清空当前命名空间的图（默认清空 —— 见下方说明）",
    ),
    concurrency: Optional[int] = Query(
        None, ge=1, le=32, description="抽取并发度（默认沿用 GRAPH_BUILD_CONCURRENCY）",
    ),
) -> Dict[str, Any]:
    """
    从知识库目录构建图（解析 → 分块 → 逐块抽取三元组 → 写 Neo4j）。

    ★ `clear` 默认 true 的理由

    `MERGE` 只对「关系集合不变」幂等：文档改了、某条关系不再成立时，
    旧关系不会自动消失（它的主键含 source + chunk_index，新关系是新主键）。
    于是图里会留下**已经过期却仍被检索命中**的关系 ——
    与「换成短文件会留下孤儿块」是同一类问题（增量索引的两个真坑之一）。

    所以默认「先清空再重建」。代价是构建期间图是空的（多跳路暂时失效），
    但换来的是图内容与当前文档**严格一致**。要增量补建时显式传 `clear=false`。

    ★ 为什么不做成后台任务立即返回

    构建耗时与**文件数**成正比（几百块就是几分钟），而这条接口的典型调用者是
    `scripts/build_graph.py` 或运维手动触发，他们要的就是"跑完了没有、写进去几条"。
    立刻返回 202 会把"到底成没成"变成一个必须轮询才能回答的问题。
    真要处理大目录，用 CLI 脚本更合适（它能打印逐文件进度）。
    """
    from app.graph.builder import build_from_dirs
    from app.graph.store import graph_store

    if not graph_store.enabled:
        # 不是 500：这是配置状态而非故障，调用方需要能区分
        # 「图功能被关掉了」与「图服务挂了」
        return {
            "ok": False,
            "reason": "图功能未启用（GRAPH_ENABLED=false）",
            "space": graph_store.space,
        }

    if _build_lock.locked():
        return {
            "ok": False,
            "reason": "已有构建正在进行中，请稍后再试",
            "space": graph_store.space,
        }

    async with _build_lock:
        logger.info("开始构建图谱: space=%s clear=%s concurrency=%s",
                    graph_store.space, clear, concurrency)
        report = await build_from_dirs(clear=clear, concurrency=concurrency)
        logger.info("图谱构建完成: %s", report.to_dict())

    return {"ok": report.error is None, **report.to_dict()}


__all__ = ["router"]
