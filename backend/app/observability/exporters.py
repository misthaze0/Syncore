"""
旁路导出器 —— 把 Trace 转发到外部分析平台

★ 当前实现：LangFuse（走官方 ingestion REST API，不依赖官方 SDK）

为什么不用 `langfuse` SDK：
SDK 会把 `opentelemetry-sdk` 全家桶拖进来，且 v2 → v3 → v4 的 API 与传输协议
（JSON ingestion → OTLP protobuf）变更过多次。本项目只需要**单向写入**这一个动作，
用 httpx 直接 POST 官方 ingestion 接口即可 —— 与 Phase 3「不引入 IK 插件、
改用入库侧预分词」是同一条取舍：能用十几行可控代码解决的，不引入重量级依赖。

协议（`POST {host}/api/public/ingestion`，Basic Auth，返回 207）::

    {"batch": [
      {"id": "...", "type": "trace-create", "timestamp": "...", "body": {...}},
      {"id": "...", "type": "span-create",  "timestamp": "...", "body": {...}}
    ]}

★ 三个必须守住的工程点

1. **绝不阻塞请求链路**。`submit()` 只入队，网络发送交给后台 worker。
   LangFuse 抖动不该让用户多等 300ms。
2. **悬空 ID 兜底**。ingestion 是批量的，若 batch 内部 ID 引用不成立
   （child 的 parentObservationId 指向未被创建的 span），LangFuse 会**静默丢弃**该 span
   —— 响应里报 success，界面上却少一条。所以导出前先做一次引用完整性校验。
3. **失败必须可见**。207 响应里带 `errors` 数组，HTTP 200 也可能有部分失败。
   只判 `status_code` 会把「全部被丢弃」当成成功，这是最难发现的一类问题。

⚠️ 兼容性说明：LangFuse 已把 ingestion API 标记为 deprecated、推荐迁移到
   OTLP 端点（`/api/public/otel/v1/traces`）。自建实例若开启了 `v4 events_only` 模式，
   `trace-create` 会被过滤掉。因此这里**每条 span 都带完整的 `traceId` 与
   冗余的 trace 级元数据** —— 即使 trace-create 被过滤，span 仍能自行组成一条 trace。
"""

from __future__ import annotations

import abc
import asyncio
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.observability.trace import Trace

logger = logging.getLogger(__name__)

#: 单次 ingestion 请求最多携带的 event 数（LangFuse 限制请求体 4.5MB，
#: 这里按条数保守切分，避免一条长回答把整个 batch 撑爆）
MAX_EVENTS_PER_REQUEST = 100


def _iso(ms: Optional[float]) -> Optional[str]:
    """epoch ms → LangFuse 需要的 ISO 8601（UTC，Z 结尾）"""
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------- 接口 ----------
class TraceExporter(abc.ABC):
    """旁路导出器接口。所有实现都必须容忍失败：导出失败不影响本地 trace。"""

    name: str = "base"
    enabled: bool = False
    disabled_reason: Optional[str] = None

    @abc.abstractmethod
    def submit(self, trace: Trace) -> None:
        """提交一条 trace（必须是非阻塞的）"""
        raise NotImplementedError

    async def aclose(self) -> None:
        return None

    def status(self) -> Dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, "reason": self.disabled_reason}


# ---------- LangFuse ----------
def build_ingestion_batch(trace: Trace) -> List[Dict[str, Any]]:
    """
    把一条 Trace 转成 LangFuse ingestion 的 event 批次。

    抽成独立函数是为了可测：不必起 HTTP 服务就能断言「ID 引用完整、时间格式正确」。
    """
    batch: List[Dict[str, Any]] = []

    # ---------- 1. trace 本身 ----------
    trace_meta: Dict[str, Any] = {
        "thread_id": trace.thread_id,
        "status": trace.status,
        "intent": trace.intent,
        "duration_ms": trace.duration_ms,
        "token_usage": trace.token_usage,
    }
    if trace.error:
        trace_meta["error"] = trace.error

    batch.append({
        "id": f"trace-{trace.trace_id}",
        "type": "trace-create",
        "timestamp": _iso(trace.start_ms),
        "body": {
            "id": trace.trace_id,
            "name": "xjms-chat",
            "sessionId": trace.thread_id or None,
            "input": trace.user_input,
            "metadata": trace_meta,
            "tags": ["xjms", "phase4"],
        },
    })

    # ---------- 2. 各 observation ----------
    span_ids = {s.span_id for s in trace.spans}
    for span in trace.spans:
        # ★ 悬空父引用会让 LangFuse 静默丢弃这条 span（响应仍报 success）。
        # 这里主动断开，至少保证 span 本身能落进去，不会连人带树一起消失。
        parent = span.parent_id if span.parent_id in span_ids else None
        if span.parent_id and parent is None:
            logger.debug("span %s 的父节点 %s 不在本 trace 内，已断开引用",
                         span.span_id, span.parent_id)

        body: Dict[str, Any] = {
            "id": span.span_id,
            "traceId": trace.trace_id,
            "parentObservationId": parent,
            "name": span.name,
            "startTime": _iso(span.start_ms),
            "endTime": _iso(span.end_ms),
            "input": span.input,
            "output": span.output,
            "metadata": {
                "kind": span.kind,
                "duration_ms": span.duration_ms,
                **({"error": span.error} if span.error else {}),
                **span.metadata,
            },
            "level": "ERROR" if span.status == "error" else "DEFAULT",
        }
        batch.append({
            "id": f"span-{span.span_id}",
            "type": "span-create",
            "timestamp": _iso(span.start_ms),
            "body": body,
        })

    return batch


class LangfuseExporter(TraceExporter):
    """
    把 trace 异步推送到 LangFuse。

    未配置密钥 / 地址不可解析时**自行禁用并给出原因**，
    交给启动日志与 /api/health 如实上报 —— 不静默失效。
    """

    name = "langfuse"

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        host: str,
        *,
        timeout: float = 10.0,
        max_queue: int = 200,
    ) -> None:
        self.host = (host or "").rstrip("/")
        self._auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._timeout = timeout
        self._max_queue = max_queue

        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None

        self.sent = 0
        self.failed = 0
        self.dropped = 0

        if not self.host or not public_key or not secret_key:
            self.enabled = False
            self.disabled_reason = "缺少 LANGFUSE_HOST / PUBLIC_KEY / SECRET_KEY"
        else:
            self.enabled = True

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """在应用 lifespan 启动阶段调用：建连接 + 起后台 worker"""
        if not self.enabled or self._worker is not None:
            return
        self._queue = asyncio.Queue(maxsize=self._max_queue)
        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._worker = asyncio.create_task(self._drain(), name="langfuse-exporter")
        logger.info("✓ LangFuse 导出已启用: %s", self.host)

    def submit(self, trace: Trace) -> None:
        """非阻塞入队。队列满时丢弃并计数 —— 宁可丢导出，不可拖慢请求。"""
        if not self.enabled or self._queue is None:
            return
        try:
            self._queue.put_nowait(trace)
        except asyncio.QueueFull:
            self.dropped += 1
            # 降频告警：只在首次丢弃时说，避免刷屏
            if self.dropped == 1:
                logger.warning("LangFuse 导出队列已满（%s），后续 trace 将被丢弃", self._max_queue)

    async def _drain(self) -> None:
        assert self._queue is not None
        while True:
            try:
                trace = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._send(trace)
                self.sent += 1
            except Exception as e:  # noqa: BLE001
                self.failed += 1
                if self.failed == 1 or self.failed % 50 == 0:
                    logger.warning("LangFuse 导出失败（已累计 %s 次）: %s", self.failed, e)
            finally:
                self._queue.task_done()

    async def _send(self, trace: Trace) -> None:
        if self._client is None:
            return
        batch = build_ingestion_batch(trace)

        for start in range(0, len(batch), MAX_EVENTS_PER_REQUEST):
            chunk = batch[start : start + MAX_EVENTS_PER_REQUEST]
            resp = await self._client.post(
                f"{self.host}/api/public/ingestion",
                headers={
                    "Authorization": f"Basic {self._auth}",
                    "Content-Type": "application/json",
                },
                json={"batch": chunk},
            )
            if resp.status_code not in (200, 207):
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            # ★ 207 只代表「请求被处理」，不代表每条 event 都成功。
            # 只判状态码会把「全部被拒」当成成功 —— 这类问题只能靠看 errors 数组发现。
            try:
                payload = resp.json()
            except ValueError:
                continue
            errors = payload.get("errors") or []
            if errors:
                # 只打前 3 条，告警本身不该比被观测的请求还长
                logger.warning("LangFuse 拒绝了 %s 条 event: %s",
                               len(errors), str(errors[:3])[:400])

    async def aclose(self) -> None:
        """把队列里剩下的发完再退出，避免最后几条 trace 丢在队列里"""
        if self._worker is not None:
            try:
                if self._queue is not None:
                    await asyncio.wait_for(self._queue.join(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._worker = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "host": self.host or None,
            "reason": self.disabled_reason,
            "sent": self.sent,
            "failed": self.failed,
            "dropped": self.dropped,
            "queue_size": self._queue.qsize() if self._queue is not None else 0,
        }


__all__ = ["TraceExporter", "LangfuseExporter", "build_ingestion_batch", "MAX_EVENTS_PER_REQUEST"]
