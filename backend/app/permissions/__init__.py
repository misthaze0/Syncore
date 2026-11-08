"""
权限层 —— 模式（一档）+ 规则（三条一组的例外）+ 判定（一个函数）

Phase 5 的落点。原先「要不要问用户」这件事只由工具上的 `danger_level` 决定，
用户能做的只有「批 / 不批」；现在多了两件事：

| 能力 | 谁在用 | 入口 |
| :--- | :--- | :--- |
| 权限模式（只读 / 手动 / 自动 / 完全访问）| 设置页 + 每条 agent 循环 | `modes.py` / `policy.effective_mode` |
| allow / ask / deny 规则 | 设置页 + 判定函数 | `rules.py` / `policy.decide` |

★ 三层的分工（与 `approval/`、`hooks/` 的关系）

    permissions  这一类操作**该不该**做         —— 纯判定，无副作用
    approval     这一次**问了没有、谁答的**     —— 留痕与会话级预授权
    hooks        用户自己的脚本怎么说          —— 只能收紧（见 policy 注释）

判定不写留痕、不留痕的不问；问了的必须留下是谁答的。这条线一糊，
审计里就会出现「不知道为什么执行了」的记录。

★ 目录边界不在这里

能碰哪些文件仍由 `core/sandbox.py` + `AGENT_ALLOWED_DIRS` 决定，
`full` 模式通过 `modes.escapes_file_boundary()` 解除它 ——
放行的判断留在沙箱层，因为**工具之外**的入口（下载接口）也走同一道门。
"""

from __future__ import annotations

from app.permissions.modes import (
    AUTO,
    FULL,
    MANUAL,
    MODE_ORDER,
    READ_ONLY,
    allows_auto_approve,
    allows_mutation,
    auto_approve_reason,
    escapes_file_boundary,
    hint,
    label,
    mode_catalog,
    mode_store,
    normalize as normalize_mode,
    tighten,
)
from app.permissions.policy import (
    PermissionDecision,
    decide,
    effective_mode,
    explain,
)
from app.permissions.rules import (
    ALLOW,
    ASK,
    BEHAVIORS,
    BEHAVIOR_HINTS,
    BEHAVIOR_LABELS,
    DENY,
    PermissionRule,
    rule_store,
)

__all__ = [
    "ALLOW",
    "ASK",
    "AUTO",
    "BEHAVIORS",
    "BEHAVIOR_HINTS",
    "BEHAVIOR_LABELS",
    "DENY",
    "FULL",
    "MANUAL",
    "MODE_ORDER",
    "PermissionDecision",
    "PermissionRule",
    "READ_ONLY",
    "allows_auto_approve",
    "allows_mutation",
    "auto_approve_reason",
    "decide",
    "effective_mode",
    "escapes_file_boundary",
    "explain",
    "hint",
    "label",
    "mode_catalog",
    "mode_store",
    "normalize_mode",
    "rule_store",
    "tighten",
]
