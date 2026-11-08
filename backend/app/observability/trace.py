"""
链路追踪数据模型 —— 回答「这一次问答到底发生了什么」

★ 为什么自己实现，而不是直接用现成 SDK

Phase 4 的原始规划是「接 LangFuse」。真正落地时拆成两件事看：

1. LangFuse 有两条写入路径 ——
   官方 SDK（引入 opentelemetry 全家桶，且 v2/v3/v4 的 API 多次变更）
   与 ingestion REST API（纯 JSON、协议稳定、零依赖）。
2. 本项目对可观测的真实需求只有两条：**落盘可查** + **评估能取到结构化数据**。
   这两条本地 JSONL 就能满足，SaaS 是加分项而不是前提条件。

所以这里与 Phase 3「拒绝 IK 插件、改为入库侧预分词」是同一条思路：
**把零依赖的本地实现做扎实，把外部服务降为可插拔的旁路导出**。
LangFuse 导出在 `exporters.py`，走官方 ingestion API，不引入 SDK 依赖。

★ 数据模型

    Trace（一次请求）
      └── Span[]（**扁平列表** + parent_id 表达树形）
            ├── intent     意图识别
            ├── retrieve   检索（含向量 / BM25 / RRF / 精排各段条数与耗时）
            ├── llm        模型生成（含 token 用量与首字延迟）
            ├── tool       工具调用（含审批结果）
            └── approval   人工审批等待

为什么是「扁平列表 + parent_id」而不是嵌套对象：
JSONL 每行是一条完整 Trace，而评估脚本要算的是「检索阶段 p95 耗时」这类**按名字聚合**的
统计。扁平结构下直接过滤即可，嵌套结构则要先递归打平 —— 而层级信息用 parent_id 一样没丢。

★ 空对象模式

调用方（retriever / runner 等）不应该到处写 `if recorder is not None`。
`active_recorder()` 在未启用追踪时返回 `NULL_RECORDER`，所有方法都是无副作用的空实现，
于是埋点代码只有一行且没有分支。
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 单个字符串字段的最大长度。追踪数据是「诊断用」的，
#: 存全量 Prompt / 回答会让 JSONL 迅速膨胀到几百 MB，反而没人看。
MAX_STR_LEN = 2000

#: 单个 list / dict 字段保留的元素个数上限（同上）
MAX_COLLECTION_LEN = 50


def now_ms() -> float:
    """当前时间戳（毫秒，float）。统一用 epoch ms，便于跨机器比较与算延迟。"""
    return time.time() * 1000.0


def _jsonable(value: Any, _depth: int = 0) -> Any:
    """
    把任意对象转成「一定可以 json.dumps」的结构。

    追踪数据来自运行时的各种对象（LangChain 消息、pydantic 模型、异常…），
    直接塞进 JSONL 会在落盘那一刻抛 TypeError，而落盘失败发生在请求结束之后 ——
    报错日志和用户问题对不上号，极难排查。所以入口处就做一次彻底净化。
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_STR_LEN else value[:MAX_STR_LEN] + "…[已截断]"
    if _depth >= 4:
        return str(value)[:MAX_STR_LEN]
    if isinstance(value, dict):
        items = list(value.items())[:MAX_COLLECTION_LEN]
        return {str(k): _jsonable(v, _depth=_depth + 1) for k, v in items}
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:MAX_COLLECTION_LEN]
        return [_jsonable(v, _depth=_depth + 1) for v in items]
    return str(value)[:MAX_STR_LEN]


# ---------- Span ----------
@dataclass
class Span:
    """
    链路中的单个环节。

    `kind` 取值对齐 LangFuse 的 observation 类型，导出时可直接映射：
    span / generation / tool / retriever / event / chain / agent
    """

    name: str
    kind: str = "span"
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: Optional[str] = None
    start_ms: float = field(default_factory=now_ms)
    end_ms: Optional[float] = None
    duration_ms: Optional[float] = None
    input: Any = None
    output: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: str = "ok"          # ok | error
    error: Optional[str] = None

    def close(
        self,
        *,
        output: Any = None,
        status: str = "ok",
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
        **metadata: Any,
    ) -> "Span":
        """
        结束这个 span。重复调用只取第一次的结果（幂等，便于写在 finally 里）。

        `duration_ms` 显式传入时以其为准，而不是用「当前时间 - 开始时间」：
        对于工具调用这类**外部已知真实耗时**的环节（SSE 的 tool_result 事件带
        `duration_ms`），外部计时包含了审批等待，会显著偏大。
        让外部值覆盖内部计时，追踪数据才与实际执行时间一致。
        """
        if self.end_ms is not None:
            return self
        if duration_ms is not None:
            try:
                self.duration_ms = round(float(duration_ms), 2)
                self.end_ms = self.start_ms + self.duration_ms
            except (TypeError, ValueError):
                self.end_ms = now_ms()
                self.duration_ms = round(self.end_ms - self.start_ms, 2)
        else:
            self.end_ms = now_ms()
            self.duration_ms = round(self.end_ms - self.start_ms, 2)
        if output is not None:
            self.output = _jsonable(output)
        if metadata:
            self.metadata.update({k: _jsonable(v) for k, v in metadata.items()})
        self.status = status
        self.error = error
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start_ms": round(self.start_ms, 2),
            "end_ms": round(self.end_ms, 2) if self.end_ms is not None else None,
            "duration_ms": self.duration_ms,
            "input": _jsonable(self.input),
            "output": _jsonable(self.output),
            "metadata": _jsonable(self.metadata),
            "status": self.status,
            "error": self.error,
        }


# ---------- Trace ----------
@dataclass
class Trace:
    """一次完整请求的链路"""

    user_input: str
    thread_id: str = ""
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    start_ms: float = field(default_factory=now_ms)
    end_ms: Optional[float] = None
    duration_ms: Optional[float] = None
    status: str = "running"     # running | ok | error
    error: Optional[str] = None
    spans: List[Span] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ---------- 派生字段（列表页直接展示，免去展开全部 span） ----------
    def span_named(self, name: str) -> Optional[Span]:
        """按名字取第一个 span（同名时取最早的）"""
        for span in self.spans:
            if span.name == name:
                return span
        return None

    def spans_named(self, name: str) -> List[Span]:
        return [s for s in self.spans if s.name == name]

    @property
    def intent(self) -> Optional[str]:
        span = self.span_named("intent")
        return (span.metadata.get("intent") if span else None) or None

    @property
    def token_usage(self) -> Dict[str, int]:
        """
        汇总本次请求的 token 用量。

        以 `llm` span 的 usage 为准（模型返回的真实 usage）。
        部分网关不返回 usage 时，runner 会回填一个按字符数估算的近似值并标记 estimated。
        """
        total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for span in self.spans_named("llm"):
            usage = span.metadata.get("usage") or {}
            for key in total:
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    total[key] += int(value)
        return total

    def summary(self) -> Dict[str, Any]:
        """列表页用的精简视图（不含 span 明细）"""
        return {
            "trace_id": self.trace_id,
            "thread_id": self.thread_id,
            "user_input": _jsonable(self.user_input),
            "start_ms": round(self.start_ms, 2),
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error": self.error,
            "intent": self.intent,
            "span_count": len(self.spans),
            "tool_calls": sum(s.kind == "tool" or s.name == "tool" for s in self.spans),
            "retrieval_ms": _duration_of(self.span_named("retrieve")),
            "llm_ms": _duration_of(self.span_named("llm")),
            "token_usage": self.token_usage,
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary()
        data.update({
            "end_ms": round(self.end_ms, 2) if self.end_ms is not None else None,
            "metadata": _jsonable(self.metadata),
            "spans": [s.to_dict() for s in self.spans],
        })
        return data


def _duration_of(span: Optional[Span]) -> Optional[float]:
    return span.duration_ms if span is not None else None


# ---------- 采集器 ----------
class TraceRecorder:
    """
    采集一次请求的 span。

    生命周期：`get_agent_event_stream` 在请求开始时创建，结束时 `finish()`。
    完成时通过 `on_finish` 回调交给存储层（落盘 + 导出），
    这样采集器本身不需要知道存储实现 —— 测试里可以直接传一个 lambda。
    """

    def __init__(self, trace: Trace, on_finish: Any = None) -> None:
        self.trace = trace
        self._on_finish = on_finish
        self._finished = False
        #: 当前打开的 span（用于自动嵌套 parent_id）
        self._open: List[Span] = []

    # ---------- 基本属性 ----------
    @property
    def trace_id(self) -> str:
        return self.trace.trace_id

    @property
    def finished(self) -> bool:
        return self._finished

    def _current_parent(self) -> Optional[str]:
        return self._open[-1].span_id if self._open else None

    # ---------- span 生命周期 ----------
    def start_span(
        self,
        name: str,
        *,
        kind: str = "span",
        parent_id: Optional[str] = None,
        input: Any = None,
        **metadata: Any,
    ) -> Span:
        span = Span(
            name=name,
            kind=kind,
            parent_id=parent_id if parent_id is not None else self._current_parent(),
            input=_jsonable(input),
            metadata={k: _jsonable(v) for k, v in metadata.items()},
        )
        self.trace.spans.append(span)
        self._open.append(span)
        return span

    def end_span(
        self,
        span: Span,
        *,
        output: Any = None,
        status: str = "ok",
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
        **metadata: Any,
    ) -> Span:
        span.close(
            output=output, status=status, error=error,
            duration_ms=duration_ms, **metadata,
        )
        # 可能被乱序关闭（异常路径），所以按 identity 摘除而不是 pop()
        self._open = [s for s in self._open if s is not span]
        return span

    def record(
        self,
        name: str,
        *,
        kind: str = "span",
        duration_ms: Optional[float] = None,
        input: Any = None,
        output: Any = None,
        status: str = "ok",
        error: Optional[str] = None,
        **metadata: Any,
    ) -> Span:
        """记录一个「已经结束」的 span（只有一个结果、无过程可测的场景）"""
        span = Span(
            name=name,
            kind=kind,
            parent_id=self._current_parent(),
            start_ms=now_ms() - float(duration_ms or 0.0),
            input=_jsonable(input),
            metadata={k: _jsonable(v) for k, v in metadata.items()},
        )
        self.trace.spans.append(span)
        # ★ 把 duration_ms 显式传给 close，而不是让它自己用「当前时间 - start_ms」重算 ——
        # 那会引入几毫秒的抖动，让「调用方说 40ms、存下来是 40.01ms」这种对不上的现象
        # 出现在追踪数据里，排查时白白多一层怀疑。
        span.close(output=output, status=status, error=error, duration_ms=duration_ms)
        return span

    def add_metadata(self, **metadata: Any) -> None:
        for key, value in metadata.items():
            self.trace.metadata[key] = _jsonable(value)

    # ---------- 结束 ----------
    def finish(self, *, status: str = "ok", error: Optional[str] = None) -> Trace:
        """
        结束采集并交给存储。

        幂等：重复调用不会重复落盘（事件流的异常路径与正常路径都会调它）。
        """
        if self._finished:
            return self.trace

        # 未关闭的 span 统一收尾，避免出现「duration 为 None」的悬空记录
        for span in list(self._open):
            self.end_span(span)

        self._finished = True
        self.trace.end_ms = now_ms()
        self.trace.duration_ms = round(self.trace.end_ms - self.trace.start_ms, 2)
        self.trace.status = status
        self.trace.error = error

        if callable(self._on_finish):
            try:
                self._on_finish(self.trace)
            except Exception as e:  # noqa: BLE001
                # 观测层永远不能把主流程打挂 —— 这是它与业务代码的边界
                logger.warning("Trace 落盘/导出失败（已忽略）: %s", e)

        return self.trace


# ---------- 空对象 —— 未启用追踪时调用方无需分支 ----------
class NullRecorder:
    """未启用追踪时的替身：所有方法都是空操作，`active_recorder()` 永不返回 None"""

    trace_id = ""
    trace = None
    finished = True

    def start_span(self, name: str, **kwargs: Any) -> Span:  # noqa: ARG002
        return Span(name=name)

    def end_span(self, span: Span, **kwargs: Any) -> Span:  # noqa: ARG002
        return span

    def record(self, name: str, **kwargs: Any) -> Span:  # noqa: ARG002
        return Span(name=name)

    def add_metadata(self, **metadata: Any) -> None:  # noqa: ARG002
        return None

    def finish(self, **kwargs: Any) -> None:  # noqa: ARG002
        return None


NULL_RECORDER = NullRecorder()


# ---------- 上下文传播 ----------
# 为什么用 contextvar：埋点分散在 retriever / runner 等深层调用里，
# 一路显式传 recorder 会污染所有函数签名。contextvar 的隔离粒度是「当前任务」，
# 恰好匹配「一次请求一个 trace」的语义 ——
# 并发请求之间不会串台（asyncio.create_task 会复制当前 context）。
_current: contextvars.ContextVar[Optional[TraceRecorder]] = contextvars.ContextVar(
    "xjms_current_trace", default=None
)


def set_current_recorder(recorder: Optional[TraceRecorder]) -> contextvars.Token:
    return _current.set(recorder)


def reset_current_recorder(token: contextvars.Token) -> None:
    try:
        _current.reset(token)
    except ValueError:
        # 跨 context 复位（例如在 create_task 之外 reset）会抛这个，忽略即可
        pass


def get_current_recorder() -> Optional[TraceRecorder]:
    return _current.get()


def active_recorder() -> Any:
    """
    取当前追踪器；未启用时返回空对象。

    埋点侧统一写成 `active_recorder().record(...)`，不必判断 None。
    """
    return _current.get() or NULL_RECORDER


def new_trace(user_input: str, thread_id: str = "") -> Trace:
    return Trace(user_input=user_input, thread_id=thread_id)


__all__ = [
    "Span",
    "Trace",
    "TraceRecorder",
    "NullRecorder",
    "NULL_RECORDER",
    "active_recorder",
    "get_current_recorder",
    "new_trace",
    "now_ms",
    "reset_current_recorder",
    "set_current_recorder",
    "MAX_STR_LEN",
]
