"""
Hooks —— 在 agent 的关键节点跑用户自己的脚本

| 文件 | 职责 | 入口 |
| :--- | :--- | :--- |
| `spec.py` | 钩子的定义与总开关（落盘 `data/hooks/hooks.json`） | `hook_store` |
| `executor.py` | 执行、退出码解释、结果收敛 | `fire` / `fire_tool_pre` / `run_once` |
| `logs.py` | 执行留痕（`data/hooks/runs.jsonl`） | `append_run` / `tail` |

★ 与权限层的关系

钩子是判定链上的**一个输入**，不是另一套判定。
`permissions/policy.decide()` 收 `hook_behavior` / `hook_reason` 两个参数，
由调用点（两条 agent 循环）在问完钩子之后传进去。
这样「为什么这一步被拦」永远能在一个函数里读出来，而不是散在两处。

★ 三条不会变的规则（细节见各模块注释）

1. 总开关出厂关闭，关着的时候**一个进程都不起**；
2. 钩子只能收紧（deny / ask），`allow` 一律忽略并如实告知；
3. 钩子坏了不阻塞主流程，只在留痕里留下退出码。
"""

from __future__ import annotations

from app.hooks.executor import HookOutcome, build_payload, fire, fire_tool_pre, run_once
from app.hooks.spec import (
    EVENT_LABELS,
    HOOK_EVENTS,
    NOTIFICATION,
    POST_TOOL,
    PRE_TOOL,
    PROMPT_SUBMIT,
    STOP,
    HookSpec,
    hook_store,
)

__all__ = [
    "EVENT_LABELS",
    "HOOK_EVENTS",
    "HookOutcome",
    "HookSpec",
    "NOTIFICATION",
    "POST_TOOL",
    "PRE_TOOL",
    "PROMPT_SUBMIT",
    "STOP",
    "build_payload",
    "fire",
    "fire_tool_pre",
    "hook_store",
    "run_once",
]
