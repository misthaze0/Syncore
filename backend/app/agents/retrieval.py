"""
检索预取 —— Supervisor `direct` 模式下的并发优化（不变量 50）。

★ 为什么它还在

Router 时代 `main_agent` 在识别意图的**同时**把检索也发起来
（两者只依赖同一个 `user_input`，串行等于把两段耗时相加）。
Supervisor 时代这个优化只对 `mode="direct"` 成立 ——
`delegate` 模式下 Supervisor 还没决定派哪个子任务，
每个子任务有**自己的** query（`instruction`），预检时根本不知道。

所以：direct 路径照旧并发，delegate 路径不预取（子任务自己会检索）。
这是不变量 50 的第二个实例，两个都还在。

★ 降级行为与检索层完全一致

这里**不重复任何兜底逻辑**，它只是「提前调用」`app.rag.retriever.retrieve`：
某一路挂掉就少一路召回，全挂则返回空列表。
异常一律吞掉 —— 预取失败的正确表现是「这一步再正常走一次」，
而不是整轮报错。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


async def prefetch_sources(user_input: str, graph_enabled: bool) -> list[dict[str, Any]]:
    """
    预取检索结果（与意图识别并发跑）。

    `graph_enabled` 就是本轮的图路开关（用户在界面上关掉它时是 False）。
    ⚠️ 它要透传下去：预取时图路由已经是关的，
    漏传会让「用户关掉的图路又被偷偷走了一遍」——
    而这个症状在检索结果里看不出来（多召回一些，答案反而更好）。

    ★ 这里**故意**不传 `scope`（ADR 0022 的知识边界）

    预取只存在于 Supervisor 的 `direct` 路径 = 主图 = 用户和主助手对话，
    而主图的 `initial_state()` 从来不带 `employee_id`
    （数字员工的会话与子任务都走子图，见 `workspace/employee_stream.py` 与
    `tasks/executor.py`）。所以「主图这一轮没有员工 ⇒ 没有项目 ⇒ 不设边界」
    是结构成立的，不是漏了。

    ⚠️ 如果哪天把员工对话接回主图，这里必须先按 `employee_id` 求 scope 再检索 ——
    边界过滤一旦后移到 runner，预取回来的那批越界片段已经进了上下文。
    """
    if not user_input.strip():
        return []
    try:
        from app.rag.retriever import retrieve

        return await retrieve(user_input, top_k=settings.RERANK_TOP_N, graph=graph_enabled)
    except asyncio.CancelledError:
        # ⚠️ 取消必须往上抛，不能被下面的 except 吞成空结果 ——
        # 吞掉的话 supervisor 会等一个永远不来的结果
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("检索预取失败（改由本轮正常检索）: %s", e)
        return []


__all__ = ["prefetch_sources"]
