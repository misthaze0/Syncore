"""
HITL 审批增强 —— 留痕 + 批量处理（Phase 6）

Phase 2 完成了「危险操作可控」的闭环：interrupt → 弹窗 → 决策 → 恢复执行。
但它停在**演示级**：审批的全部痕迹只活在内存里，一次重启就归零，
也没有任何批量处理能力，一批危险操作必须逐个人工点确认。

本模块补上把它做到**可用级**的两块：

| 文件 | 解决什么 | 对外入口 |
| :--- | :--- | :--- |
| `records.py` | 「谁在什么时候批了什么」可查询、可审计、重启不丢 | `approval_store` |
| `grants.py`  | 「这一批同类操作一次放行」，且带次数与时效上限 | `grant_registry` |

★ 与 `agents/approval_broker.py` 的分工

    approval_broker   运行时协调（谁在等、唤醒谁）—— 生命周期等于一次请求
    records           留痕（发生了什么）—— 生命周期跨进程重启
    grants            授权（哪些可以不问）—— 生命周期等于一个会话

三者不要互相替代：把留痕塞进 broker 会让它变成半个数据库，
把 broker 的状态塞进 records 则会让每次等待都多一次磁盘 IO。
"""

from __future__ import annotations

from app.approval.grants import ApprovalGrant, GrantRegistry, grant_registry
from app.approval.records import (
    BY_GRANT,
    BY_HOOK,
    BY_LABELS,
    BY_MODE,
    BY_RULE,
    BY_SYSTEM,
    BY_TIMEOUT,
    BY_USER,
    DECISION_APPROVED,
    DECISION_LABELS,
    DECISION_PENDING,
    DECISION_REJECTED,
    DECISION_TIMEOUT,
    ApprovalRecord,
    ApprovalStore,
    approval_store,
)

__all__ = [
    "BY_GRANT",
    "BY_HOOK",
    "BY_LABELS",
    "BY_MODE",
    "BY_RULE",
    "BY_SYSTEM",
    "BY_TIMEOUT",
    "BY_USER",
    "DECISION_APPROVED",
    "DECISION_LABELS",
    "DECISION_PENDING",
    "DECISION_REJECTED",
    "DECISION_TIMEOUT",
    "ApprovalGrant",
    "ApprovalRecord",
    "ApprovalStore",
    "GrantRegistry",
    "approval_store",
    "grant_registry",
]