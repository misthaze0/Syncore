"""
SSE 事件发射器

Graph 节点无法直接 yield SSE 事件，因此通过 `config["configurable"]["emit"]`
把事件推给驱动层（`get_agent_event_stream`）的事件队列。

注意: `configurable` 不会被写入 Checkpointer，因此可以安全携带函数对象。
"""

from __future__ import annotations

from typing import Any, Callable


def get_emitter(config: Any) -> Callable[[dict], None]:
    """从 RunnableConfig 中取出 emit 回调；缺失时返回空实现（保证节点可脱机测试）"""
    if isinstance(config, dict):
        emit = (config.get("configurable") or {}).get("emit")
        if callable(emit):
            return emit
    return lambda event: None  # noqa: ARG005
