"""
可观测层 —— 链路追踪 + 旁路导出

对外只暴露三件事：

    start_observability()   应用启动时调用（建存储、启导出器）
    begin_trace(...)        请求开始时调用（返回记录器）
    observability_status()  能力状态（供启动日志与 /api/health）

埋点侧（retriever / runner 等深层代码）用 `active_recorder()` 取记录器，
未启用追踪时它返回空对象，因此埋点处不需要写 `if`。

★ 分层

    trace.py      数据模型 + 采集器（纯内存，无 IO、无依赖）
    store.py      本地落盘（JSONL + 环形缓冲）
    exporters.py  外部平台旁路（LangFuse）

`TracePipeline` 把 store 与 exporters 串起来，作为采集器的 `on_finish` 回调。
分成三个文件而不是一个大模块，是为了让「采集逻辑」可以完全不碰 IO 地单测。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.observability.exporters import TraceExporter
from app.observability.store import TraceStore
from app.observability.trace import (
    NULL_RECORDER,
    NullRecorder,
    Span,
    Trace,
    TraceRecorder,
    active_recorder,
    get_current_recorder,
    new_trace,
    now_ms,
    reset_current_recorder,
    set_current_recorder,
)

logger = logging.getLogger(__name__)


# ---------- 管道：一条 trace 的多个去处 ----------
class TracePipeline:
    """
    Trace 落点编排：**本地存储优先，外部导出次之**。

    顺序不能反。本地 JSONL 是所有评估数据的来源，必须成功；
    外部导出是加分项，失败只记账。若先导外部，一次网络抖动就会让
    「本地有数据、评估却跑不了」这种事发生。
    """

    def __init__(self, store: TraceStore, exporters: Optional[List[TraceExporter]] = None) -> None:
        self.store = store
        self.exporters: List[TraceExporter] = list(exporters or [])

    def handle(self, trace: Trace) -> None:
        """采集完成回调（同步）。任何一层失败都不影响其余层。"""
        try:
            self.store.save(trace)
        except Exception as e:  # noqa: BLE001
            logger.warning("Trace 本地落盘异常: %s", e)

        for exporter in self.exporters:
            try:
                exporter.submit(trace)
            except Exception as e:  # noqa: BLE001
                logger.warning("Trace 导出到 %s 异常: %s", exporter.name, e)

    def status(self) -> Dict[str, Any]:
        return {
            "store": self.store.stats(),
            "exporters": [ex.status() for ex in self.exporters],
        }


# ---------- 全局单例 ----------
_store: Optional[TraceStore] = None
_pipeline: Optional[TracePipeline] = None


def init_observability() -> TracePipeline:
    """按配置构建存储与导出器（可在 lifespan 之前重复调用，幂等）"""
    global _store, _pipeline

    if _pipeline is not None:
        return _pipeline

    from app.config import settings

    enabled = bool(getattr(settings, "OBSERVABILITY_ENABLED", True))
    _store = TraceStore(
        directory=settings.trace_path,
        max_memory=getattr(settings, "TRACE_MAX_MEMORY", 500),
        enabled=enabled,
    )

    exporters: List[TraceExporter] = []
    if getattr(settings, "LANGFUSE_ENABLED", False):
        from app.observability.exporters import LangfuseExporter

        exporters.append(LangfuseExporter(
            public_key=getattr(settings, "LANGFUSE_PUBLIC_KEY", ""),
            secret_key=getattr(settings, "LANGFUSE_SECRET_KEY", ""),
            host=getattr(settings, "LANGFUSE_HOST", ""),
            timeout=getattr(settings, "LANGFUSE_TIMEOUT_SECONDS", 10.0),
        ))
    else:
        logger.info("→ LangFuse 导出未启用（LANGFUSE_ENABLED=false），仅本地落盘")

    _pipeline = TracePipeline(_store, exporters)
    return _pipeline


async def start_observability() -> Dict[str, Any]:
    """应用 startup 阶段：回填历史 trace + 启动导出 worker"""
    from app.config import settings

    pipeline = init_observability()

    # 先清理过期分片再回填：不读马上要删的文件；保留期收紧后重启立即生效
    retention = int(getattr(settings, "TRACE_RETENTION_DAYS", 30))
    if retention > 0 and pipeline.store.enabled:
        try:
            pipeline.store.cleanup_old_files(retention)
        except Exception as e:  # noqa: BLE001
            logger.warning("启动清理过期 trace 失败（不影响追踪）: %s", e)

    load_n = int(getattr(settings, "TRACE_LOAD_ON_START", 100))
    loaded = pipeline.store.load_recent(load_n) if load_n > 0 else 0

    for exporter in pipeline.exporters:
        start = getattr(exporter, "start", None)
        if callable(start):
            try:
                await start()
            except Exception as e:  # noqa: BLE001
                exporter.enabled = False
                exporter.disabled_reason = f"启动失败: {e}"
                logger.warning("导出器 %s 启动失败: %s", exporter.name, e)

    if pipeline.store.enabled:
        logger.info("✓ 链路追踪已启用 | 目录=%s | 回填历史=%s 条",
                    pipeline.store.directory, loaded)
    return observability_status()


async def shutdown_observability() -> None:
    """应用 shutdown 阶段：把导出队列里剩下的发完"""
    for exporter in get_exporters():
        try:
            await exporter.aclose()
        except Exception:  # noqa: BLE001
            pass


def get_store() -> TraceStore:
    return init_observability().store


def get_exporters() -> List[TraceExporter]:
    return init_observability().exporters


def get_pipeline() -> TracePipeline:
    return init_observability()


def observability_status() -> Dict[str, Any]:
    pipeline = init_observability()
    store_stats = pipeline.store.stats()
    return {
        "enabled": store_stats["enabled"],
        "path": store_stats["directory"],
        "in_memory": store_stats["in_memory"],
        "written": store_stats["written"],
        "exporters": [ex.status() for ex in pipeline.exporters],
    }


# ---------- 请求入口 ----------
def begin_trace(user_input: str, thread_id: str = "") -> TraceRecorder:
    """
    开始采集一次请求，并把记录器挂到当前上下文。

    返回的记录器**由调用方负责 `finish()`**（放在 finally 里），
    否则这条 trace 会永远停在 running 状态、也不会落盘。
    """
    recorder = TraceRecorder(new_trace(user_input, thread_id), on_finish=get_pipeline().handle)
    set_current_recorder(recorder)
    return recorder


__all__ = [
    "Trace",
    "Span",
    "TraceRecorder",
    "NullRecorder",
    "NULL_RECORDER",
    "TracePipeline",
    "TraceStore",
    "active_recorder",
    "begin_trace",
    "get_current_recorder",
    "get_exporters",
    "get_pipeline",
    "get_store",
    "init_observability",
    "new_trace",
    "now_ms",
    "observability_status",
    "reset_current_recorder",
    "set_current_recorder",
    "shutdown_observability",
    "start_observability",
]
