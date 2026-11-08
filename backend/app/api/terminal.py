"""
终端接口 —— 侧边栏「终端」页签的执行通道

★ 为什么用户手敲的命令不需要 HITL 审批

agent 调 `bash` 时必须弹窗审批，因为「模型决定执行什么」——
人看到的是一行命令字符串，模型可能夹带副作用。而终端页签里的每一条命令
都是**用户自己逐字输入**的：发起者就是审批者，「输入回车」这个动作本身
就是审批。跳过 HITL 不是放松安全，是同一条安全链上发起人不同的同一种检查。

★ 安全部照搬 shell 工具（一层都不减）

- 命令黑名单（`check_command`）：破坏性命令即便用户手敲也拒绝 ——
  终端里跑的命令和 agent 跑的命令伤害的是同一台机器；
- 沙箱 cwd：所有命令在工作目录内执行；
- 环境隔离（`_safe_env`）：业务密钥不进子进程 —— 前端页面是渲染模型产出的
  地方，键盘记录/注入的风险面比本地终端大，密钥不该出现在它的可达范围里；
- 超时 + 输出截断：由 `bash` 统一处理。

★ 为什么不是 WebSocket PTY

真实 PTY（持续会话、cd 持久化、交互式程序）需要常驻子进程 + 双向流，
是另一个量级的工程。桌面场景下「跑一条命令看输出」覆盖终端页签 90% 的诉求
（看目录、跑脚本、试环境），且与后端已有的执行基础设施完全同构。
`cd` 类会话状态不跨请求保留 —— 输出里已如实带回退出码与目录，够排查用。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.agents.tools.builtin.shell import DEFAULT_TIMEOUT, HARD_TIMEOUT_MAX, bash

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/terminal", tags=["terminal"])


class TerminalRunRequest(BaseModel):
    """一条终端命令。命令为空时直接 400 —— 前端不该发出空命令"""

    command: str = Field(..., min_length=1, max_length=2000, description="要执行的命令")
    timeout: int = Field(DEFAULT_TIMEOUT, ge=1, le=HARD_TIMEOUT_MAX, description="超时秒数")


@router.post("/run")
async def run_terminal_command(payload: TerminalRunRequest) -> dict[str, Any]:
    """在沙箱内执行一条命令并返回完整输出（含退出码）。不区分成功失败 —— 输出本身就是结果"""
    output = await bash(command=payload.command, timeout=payload.timeout)
    return {"ok": True, "command": payload.command, "output": output}


__all__ = ["router"]
