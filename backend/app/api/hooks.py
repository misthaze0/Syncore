"""
Hooks 控制面接口 —— 设置页「钩子」的数据源

列清单、开关、增删改、试跑、看执行留痕。

★ 「试跑」是真的把脚本跑一遍

`POST /hooks/{id}/test` 会起进程、走同一条 `safe_env` 与同一个 cwd，
只是结果只回给界面、不参与任何权限判定。总开关**关着时也允许试跑** ——
用户正是靠它决定要不要打开；关着时打开会立刻影响每一次工具调用，
没有试跑的话这个决定只能靠猜。

★ 试跑发的 JSON 与真跑一字不差

形状由 `app/hooks/executor.build_payload()` 独家定义，五条真实调用点
（两条 agent 循环 + 审批 + 主图首尾）与这里共用它。响应里把这份 payload
原样回显，用户对着它写脚本，不用猜键名。

★ 这里没有「让钩子放行」的入口

钩子的 `allow` 在执行层就被丢弃（`executor` 模块注释解释了为什么）。
接口如果只是"照单全收"地把它显示成「已放行」，用户会以为
自己那条脚本生效了 —— 所以 `ignored_allow` 必须原样带到界面上。

★ 路由顺序

`PUT /hooks/enabled` 声明在 `PUT /hooks/{hook_id}` **之前**：
FastAPI 按注册顺序匹配，反了的话 `enabled` 会被当成一个 hook id，
症状是「总开关点了没反应」。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.hooks import EVENT_LABELS, HOOK_EVENTS, HookSpec, build_payload, hook_store, run_once
from app.hooks import logs as hook_logs
from app.permissions.policy import mode_store_default

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hooks", tags=["hooks"])

#: 试跑用的假身份。写死成一眼假的值：脚本若拿 `session.thread_id` 当真身份去查库，
#: 在试跑里应该什么都查不到，而不是误命中某条真实会话。
TRIAL_SESSION: Dict[str, str] = {
    "thread_id": "trial",
    "task_id": "trial",
    "run_id": "trial",
    "employee_id": "",
    "agent": "trial",
}


class HookPayload(BaseModel):
    """新建 / 修改一条钩子（`PUT` 只改传上来的字段）"""

    model_config = ConfigDict(extra="forbid")

    event: Optional[str] = Field(None, description=f"{' / '.join(HOOK_EVENTS)}")
    command: Optional[str] = Field(None, description="完整的命令行，静态配置，不做变量插值")
    tool: Optional[str] = Field(None, description="工具名通配（bash / mcp_notion_*）；空=全部")
    timeout: Optional[int] = Field(None, description="秒，0=用全局默认，最长 300")
    enabled: Optional[bool] = None
    note: Optional[str] = Field(None, description="这条是干什么的，只给界面看")


class SwitchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class HookTestPayload(BaseModel):
    """试跑的输入：按事件挑用得到的那几个字段"""

    model_config = ConfigDict(extra="forbid")

    tool_name: Optional[str] = Field(None, description="工具类事件用；留空=示例工具")
    args: Dict[str, Any] = Field(default_factory=dict, description="即将传给工具的参数")
    prompt: str = Field("", description="UserPromptSubmit 的示例消息")
    answer: str = Field("", description="Stop 的示例回答")
    message: str = Field("", description="Notification 的提示文案")


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _catalog() -> list[Dict[str, Any]]:
    """五个事件的界面文案（哪一列会真表态也在这里说清，免得用户指望 Stop 能拦人）"""
    return [
        {
            "value": event,
            "label": EVENT_LABELS.get(event, event),
            "decides": event == "PreToolUse",
        }
        for event in HOOK_EVENTS
    ]


def _overview() -> Dict[str, Any]:
    hooks = hook_store.load()["hooks"]
    return {
        "hooks": [h.to_dict() for h in hooks],
        "total": len(hooks),
        "enabled": sum(1 for h in hooks if h.enabled),
        "global_enabled": hook_store.global_enabled,
        "stats": hook_store.stats(),
        "events": _catalog(),
        "paths": {
            "store": str(settings.hooks_store_path),
            "run_log": str(settings.hooks_run_log_path),
        },
        # 脚本的运行环境 —— 用户写命令之前必须知道的三件事
        "runtime": {
            "cwd": str(settings.sandbox_path),
            "env": "只带白名单变量（LLM_API_KEY / REDIS_URL 这类不会传进来）",
            "default_timeout_seconds": settings.HOOKS_TIMEOUT_SECONDS,
            "max_timeout_seconds": 300,
            "payload": "参数走 stdin 的 JSON，命令串里不做任何变量替换",
        },
    }


def _get_hook(hook_id: str) -> HookSpec:
    hook = next((h for h in hook_store.load()["hooks"] if h.hook_id == hook_id), None)
    if hook is None:
        raise HTTPException(status_code=404, detail=f"钩子不存在: {hook_id}")
    return hook


def _sample_payload(hook: HookSpec, body: HookTestPayload) -> Dict[str, Any]:
    """把界面填的示例值补成一份**能跑**的 payload（缺省的用一眼是示例的值）"""
    event = hook.event
    tool_name = (body.tool_name or "read_file").strip()
    args = dict(body.args) or {"path": "notes/示例.txt"}
    if event == "PostToolUse":
        return build_payload(
            event,
            tool_name=tool_name,
            args=args,
            session=TRIAL_SESSION,
            permission_mode=mode_store_default(),
            tool_response={"success": True, "result": "这是试跑的示例输出，不是真实执行结果。"},
        )
    if event == "UserPromptSubmit":
        return build_payload(
            event, prompt=body.prompt or "帮我看一下今天的日程", session=TRIAL_SESSION
        )
    if event == "Notification":
        return build_payload(
            event,
            message=body.message or f"助手想执行「{tool_name}」，正在等你批准。",
            reason="试跑示例理由",
            tool_name=tool_name,
            session=TRIAL_SESSION,
        )
    if event == "Stop":
        return build_payload(
            event, session=TRIAL_SESSION, answer=body.answer or "这是这一轮回答的示例。"
        )
    return build_payload(
        event,
        tool_name=tool_name,
        args=args,
        session=TRIAL_SESSION,
        permission_mode=mode_store_default(),
    )


# ---------- 读 ----------

@router.get("")
async def list_hooks() -> Dict[str, Any]:
    """全部钩子 + 事件目录 + 统计 + 运行环境（设置页一次拿全）"""
    return _overview()


@router.get("/runs")
async def run_log(limit: int = 50) -> Dict[str, Any]:
    """
    执行留痕（新的在前）。

    这是「她今天反应好慢」和「这个操作莫名其妙被拦了」唯一能核对的地方：
    退出码、耗时、超时标记、判定的表态、给用户看的那句话都在里面。
    `?limit=` 上限 500（内存尾部就这么多，再多要读文件）。
    """
    clean = max(1, min(limit, 500))
    return {"runs": hook_logs.tail(clean), "limit": clean, "log_path": str(settings.hooks_run_log_path)}


# ---------- 总开关 ----------

@router.put("/enabled")
async def set_global_enabled(payload: SwitchPayload) -> Dict[str, Any]:
    """
    打开 / 关掉整个 Hooks 功能。

    ⚠️ 打开 = 她每次调工具都会跑你写的脚本，**不弹窗、不审批**。
    所以出厂是关的，而且关掉时一个进程都不起（不是"界面提示一下"）。
    """
    applied = hook_store.set_global(payload.enabled)
    logger.info("Hooks 总开关由设置页切换为 %s", "开" if applied else "关")
    return {"ok": True, "global_enabled": applied, **_overview()}


# ---------- 增删改 ----------

@router.post("")
async def create_hook(payload: HookPayload) -> Dict[str, Any]:
    """新增一条钩子（总开关还关着也能建，建好先试跑）"""
    hook = HookSpec.from_dict(payload.model_dump(exclude_none=True))
    try:
        saved, action = hook_store.upsert(hook)
    except ValueError as e:
        raise _bad_request(str(e)) from e
    return {"ok": True, "action": action, "hook": saved.to_dict(), "stats": hook_store.stats()}


@router.put("/{hook_id}")
async def update_hook(hook_id: str, payload: HookPayload) -> Dict[str, Any]:
    """修改一条钩子（部分更新；命令改了不会追溯已经跑过的那次）"""
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise _bad_request("没有要修改的字段")
    existing = _get_hook(hook_id)
    merged = HookSpec.from_dict({**existing.to_storage(), **changes})
    merged.hook_id = hook_id
    try:
        saved, _ = hook_store.upsert(merged)
    except ValueError as e:
        raise _bad_request(str(e)) from e
    return {"ok": True, "hook": saved.to_dict(), "stats": hook_store.stats()}


@router.put("/{hook_id}/enabled")
async def set_hook_enabled(hook_id: str, payload: SwitchPayload) -> Dict[str, Any]:
    """停用单条钩子（总开关之外的第二道闸，用来「先停一下看看是不是它」）"""
    hook = hook_store.set_enabled(hook_id, payload.enabled)
    if hook is None:
        raise HTTPException(status_code=404, detail=f"钩子不存在: {hook_id}")
    return {"ok": True, "hook": hook.to_dict(), "stats": hook_store.stats()}


@router.delete("/{hook_id}")
async def delete_hook(hook_id: str) -> Dict[str, Any]:
    if not hook_store.delete(hook_id):
        raise HTTPException(status_code=404, detail=f"钩子不存在: {hook_id}")
    return {"ok": True, "stats": hook_store.stats()}


# ---------- 试跑 ----------

@router.post("/{hook_id}/test")
async def test_hook(hook_id: str, payload: HookTestPayload) -> Dict[str, Any]:
    """
    真跑一次这条命令，返回退出码 / stdout / stderr / 收敛后的表态。

    不参与权限判定（判定只在真对话里发生），但**确实执行**：
    脚本会读写文件、会发请求。所以界面上这句话要写明白 —— 「试跑」不是模拟。
    """
    hook = _get_hook(hook_id)
    sample = _sample_payload(hook, payload)
    result = await run_once(hook, sample)
    return {
        "ok": True,
        "hook": hook.to_dict(),
        "payload": sample,
        "ran_while_disabled": not hook_store.global_enabled,
        **result,
    }
