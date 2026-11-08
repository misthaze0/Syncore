"""
用量账本 —— 对外入口

分层与 `app/observability/` 一致：
`ledger.py` 是纯存储与聚合，本模块只做「按配置构建单例」+「给调用点的这一行 API」。

调用点只需要记一句话：

    from app.usage import record_usage
    record_usage(model=llm.model_name, usage=usage, agent="supervisor", thread_id=tid)

`record_usage` **永不抛异常** —— 它是旁路，账记不上是「这次没记上」，
而不是「因为记不了账，所以答不了问题」（与会话历史写入同一条纪律）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.usage.ledger import (
    PURPOSE_ANSWER,
    PURPOSE_SUBAGENT,
    WINDOW_LABELS,
    UsageLedger,
    window_kind_of,
)

logger = logging.getLogger(__name__)

_ledger: Optional[UsageLedger] = None


def init_usage_ledger() -> UsageLedger:
    """按配置构建账本单例（幂等，可在 lifespan 之前重复调用）"""
    global _ledger

    if _ledger is not None:
        return _ledger

    from app.config import settings

    _ledger = UsageLedger(
        directory=settings.usage_ledger_path,
        enabled=bool(getattr(settings, "USAGE_LEDGER_ENABLED", True)),
        max_rows_per_file=int(getattr(settings, "USAGE_LEDGER_MAX_ROWS_PER_FILE", 200000)),
    )
    logger.info(
        "→ 用量账本%s：%s（只增不删，无保留期）",
        "已启用" if _ledger.enabled else "未启用",
        _ledger.directory,
    )
    return _ledger


def get_ledger() -> UsageLedger:
    return init_usage_ledger()


def record_usage(
    *,
    model: str,
    usage: Optional[Dict[str, Any]] = None,
    agent: str = "",
    purpose: str = PURPOSE_ANSWER,
    thread_id: str = "",
    step: int = 0,
    duration_ms: float = 0.0,
    status: str = "ok",
    title: Optional[str] = None,
) -> None:
    """
    记一笔对话用量。

    `usage` 是 `_extract_usage` 的产物（可能为空 dict —— 网关没返回 usage）。
    空 dict 时**仍然落一笔全 0 并标 reported=false**：调用真实发生了，
    只是数字拿不到。跳过不记会让账本看起来「那次问答没花钱」。

    `thread_id` 留空时从当前 trace 里取 —— 埋点处（runner / supervisor）
    本来就能从 state 拿到，但事件流的异常路径上未必，兜一道更稳。
    """
    try:
        ledger = init_usage_ledger()
        if not ledger.enabled:
            return

        resolved_thread = thread_id
        trace_id = ""
        # 延迟导入：observability 不依赖 usage，反向依赖只在运行时才成环
        from app.observability.trace import get_current_recorder

        recorder = get_current_recorder()
        trace = getattr(recorder, "trace", None) if recorder is not None else None
        if trace is not None:
            resolved_thread = resolved_thread or getattr(trace, "thread_id", "") or ""
            trace_id = getattr(trace, "trace_id", "") or ""

        ledger.append({
            "model": model,
            "agent": agent,
            "purpose": purpose,
            "thread_id": resolved_thread,
            "title": ledger_title_of(resolved_thread) if title is None else title,
            "step": step,
            "duration_ms": duration_ms,
            "status": status,
            "reported": bool(usage),
            "trace_id": trace_id,
            **(usage or {}),
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("用量记账失败（已忽略）: %s", e)


def ledger_title_of(thread_id: str) -> str:
    """
    取对话窗口标题（只读会话索引，不读轮次文件）。

    首轮问答时这一轮还没落盘，索引里没有标题 —— 返回空串是正常的，
    读接口会用「当前索引标题优先、快照兜底」两级回退补上。
    """
    if not thread_id:
        return ""
    try:
        from app.conversations.store import conversation_store

        return conversation_store.title_of(thread_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("取会话标题失败（已忽略）: %s", e)
        return ""


__all__ = [
    "PURPOSE_ANSWER",
    "PURPOSE_SUBAGENT",
    "WINDOW_LABELS",
    "UsageLedger",
    "get_ledger",
    "init_usage_ledger",
    "ledger_title_of",
    "record_usage",
    "window_kind_of",
]
