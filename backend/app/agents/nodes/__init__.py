"""
节点实现（Supervisor 架构，见 ADR 0019）。

★ 主图的节点不在这里

主图三个节点（`supervisor` / `dispatch` / `respond`）在 `app/agents/supervisor.py` ——
它们是**一个决策流程的三个阶段**，放一起才看得出「拆解 → 派发 → 回答」
是怎么串起来的。

这个包只放**子图**用的东西：
- `runner` — planner / tool_executor 的实现（被子图适配层复用，一行都没改）
- `subtask` — 子图节点的适配层（字段改名 + 事件过滤）
"""

from app.agents.nodes.runner import run_planner_turn, tool_executor_node
from app.agents.nodes.subtask import subtask_planner_node, subtask_tool_executor_node

__all__ = [
    # 复用的实现（子图适配层调用它们）
    "run_planner_turn",
    "tool_executor_node",
    # 子图节点
    "subtask_planner_node",
    "subtask_tool_executor_node",
]
