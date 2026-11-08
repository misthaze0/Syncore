"""
Redis 客户端 (Phase 2)

两个用途:
1. LangGraph Checkpointer —— 持久化会话状态，支撑多轮记忆与 HITL 中断恢复
2. 通用缓存连接（审批结果短期存放等）

容错策略: Redis 不可用时自动降级为内存 Checkpointer，
保证「没有基础设施也能把 Phase 2 完整跑通」。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_redis_client: Any = None


async def get_redis():
    """获取通用 Redis 异步连接（不可用时返回 None）"""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await client.ping()
        _redis_client = client
        logger.info("✓ Redis 已连接: %s", settings.REDIS_URL)
    except Exception as e:  # noqa: BLE001
        logger.warning("Redis 不可用（将使用内存降级方案）: %s", e)
        _redis_client = None
    return _redis_client


async def close_redis() -> None:
    """关闭 Redis 连接"""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:  # noqa: BLE001
            pass
    _redis_client = None


#: ★ 子图（后台子任务）复用**主图那一个** Checkpointer 实例
#:
#: 为什么不另建一个：另建意味着多一套连接管理与降级逻辑，
#: 而「主图与子图不共用命名空间」这个需求**靠 thread_id 前缀就能满足**
#: （`subtask_<task_id>`，见不变量 58）——
#: 前缀让「不相撞」成为结构上的保证，不需要两套存储。
#:
#: 它由 `create_checkpointer()` 写入、`close_redis()` 清空；
#: 读它的地方是后台子任务执行器（`app/tasks/executor.py`）。
_checkpoint_holder: Any = None


def get_checkpointer() -> Any:
    """
    取当前共享的 Checkpointer（未初始化时返回 None）。

    ⚠️ 拿不到时子图**不能**挂起等审批 ——
    没有 checkpointer 就没有 `interrupt()` 的恢复能力，
    审批会变成「永久等待」（用户点批准，任务不往下走）。
    所以调用方必须检查返回值并如实告知，而不是静默跑一个「审批不了」的任务。
    """
    return _checkpoint_holder


async def create_checkpointer() -> tuple[Any, Any]:
    """
    创建 LangGraph Checkpointer。

    Returns:
        (checkpointer, cleanup) —— cleanup 是需要在应用关闭时调用的协程/上下文管理器，
        内存模式下为 None。

    优先 Redis（需要 redis-stack，含 JSON + Search 模块），
    失败则降级 MemorySaver（进程内有效，足够本地开发与演示）。
    """
    global _checkpoint_holder

    saver_cls = None
    for module_path in (
        "langgraph.checkpoint.redis.aio",
        "langgraph.checkpoint.redis",
    ):
        try:
            module = __import__(module_path, fromlist=["AsyncRedisSaver"])
            saver_cls = getattr(module, "AsyncRedisSaver", None)
            if saver_cls is not None:
                break
        except Exception:  # noqa: BLE001
            continue

    if saver_cls is not None:
        try:
            cm = saver_cls.from_conn_string(settings.REDIS_URL)
            saver = await cm.__aenter__()          # 保持连接不被关闭
            setup = getattr(saver, "asetup", None)
            if setup is not None:
                await setup()
            logger.info("✓ 使用 Redis Checkpointer（会话状态持久化）")
            _checkpoint_holder = saver
            return saver, cm
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis Checkpointer 初始化失败，降级为内存模式: %s", e)

    from langgraph.checkpoint.memory import InMemorySaver

    logger.info("✓ 使用 InMemory Checkpointer（进程内会话状态）")
    saver = InMemorySaver()
    _checkpoint_holder = saver
    return saver, None
