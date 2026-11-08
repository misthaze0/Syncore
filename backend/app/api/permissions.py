"""
用户权限控制面：读取/保存默认模式、管理工具例外规则和暂停改动。

完全访问必须携带明确确认。模式和规则改变后对后续调用生效；会话请求只能收紧模式。
这组 API 不注册为模型工具。
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.permissions import modes
from app.permissions.policy import mode_store_default
from app.permissions.rules import (
    BEHAVIOR_HINTS,
    BEHAVIOR_LABELS,
    BEHAVIORS,
    PermissionRule,
    rule_store,
)

router = APIRouter(prefix="/permissions", tags=["permissions"])


class EnabledPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ModePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["read_only", "manual", "auto", "full"]
    acknowledge_full_access: bool = False


class RulePayload(BaseModel):
    """
    新建 / 修改一条规则。

    全部字段可选：`PUT` 只改传上来的那些，未提及的保持原样 ——
    前端提交整张表单时不会把没填的字段清空。`extra="forbid"` 保证键名打错
    是 422，而不是「看起来保存了但什么都没变」。
    """

    model_config = ConfigDict(extra="forbid")

    behavior: Optional[str] = Field(None, description=f"{' / '.join(BEHAVIORS)}")
    tool: Optional[str] = Field(None, description="工具名，支持 fnmatch 通配（mcp_notion_*）")
    arg: Optional[str] = Field(None, description="参数名；留空 = 自动挑主要参数")
    pattern: Optional[str] = Field(None, description="参数值通配；留空 = 整个工具")
    note: Optional[str] = Field(None, description="给人看的一句话，会作为拒绝/放行理由回显")
    enabled: Optional[bool] = None


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _mode_block() -> Dict[str, Any]:
    """当前档位的完整对外表示（界面不需要再拼文案）"""
    current = mode_store_default()
    payload = modes.mode_store.load()
    return {
        "default_mode": current,
        "label": modes.label(current),
        "hint": modes.hint(current),
        "changed_at": payload.get("changed_at", ""),
        "catalog": modes.mode_catalog(),
        # 界面用它把「本轮临时收紧」的下拉框裁到只剩合法选项
        "tighten_options": [
            modes.MODE_ORDER[i] for i in range(modes.MODE_ORDER.index(current) + 1)
        ],
        "file_boundary": {
            # `full` 之外都受沙箱 + 授权目录限制；把实际范围一起回显，
            # 用户才不用猜「自动执行到底能改哪些文件」
            "exempt": modes.escapes_file_boundary(current),
            "sandbox": str(settings.sandbox_path.resolve()),
            "allowed_dirs": [str(p.resolve()) for p in settings.agent_allowed_dirs_list],
        },
    }


# ---------- 读 ----------

@router.get("/mode")
async def get_mode() -> Dict[str, Any]:
    """当前默认档 + 四档说明 + 实际文件边界"""
    return _mode_block()


@router.put("/mode")
async def set_mode(payload: ModePayload) -> Dict[str, Any]:
    if payload.mode == modes.FULL and not payload.acknowledge_full_access:
        raise _bad_request("启用完全访问前，请确认已了解目录限制解除和高风险操作自动执行的影响。")
    modes.mode_store.save(payload.mode)
    # 模式变化后不保留旧的临时信任。
    from app.approval import grant_registry
    grant_registry.reset()
    return _mode_block()


@router.post("/pause")
async def pause_operations() -> Dict[str, Any]:
    """收紧后再释放等待者，尚未执行的调用会在注册中心重新校验。"""
    from app.agents.approval_broker import approval_broker
    from app.approval import grant_registry
    modes.mode_store.save(modes.READ_ONLY)
    grant_registry.reset()
    approval_broker.cancel_all()
    return _mode_block()


@router.get("/rules")
async def list_rules() -> Dict[str, Any]:
    """全部规则（新建的在前）+ 规则表统计 + 三个行为的界面文案"""
    rules = rule_store.load()
    return {
        "rules": [r.to_dict() for r in rules],
        "total": len(rules),
        "stats": rule_store.stats(),
        "behaviors": [
            {"value": b, "label": BEHAVIOR_LABELS[b], "hint": BEHAVIOR_HINTS[b]}
            for b in BEHAVIORS
        ],
        "pattern_hint": "支持 * 与 ?；留空匹配该工具的整个调用",
    }


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: str) -> Dict[str, Any]:
    rule = next((r for r in rule_store.load() if r.rule_id == rule_id), None)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return rule.to_dict()


# ---------- 写 ----------

@router.post("/rules")
async def create_rule(payload: RulePayload) -> Dict[str, Any]:
    """新增一条规则（立即生效，下一次调用就会被它影响）"""
    raw = payload.model_dump(exclude_none=True)
    rule = PermissionRule.from_dict(raw)
    try:
        saved, action = rule_store.upsert(rule)
    except ValueError as e:
        raise _bad_request(str(e)) from e
    return {"ok": True, "action": action, "rule": saved.to_dict(), "stats": rule_store.stats()}


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: str, payload: RulePayload) -> Dict[str, Any]:
    """修改一条规则（部分更新；没传的字段保持原样）"""
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise _bad_request("没有要修改的字段")
    existing = next((r for r in rule_store.load() if r.rule_id == rule_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    merged = PermissionRule.from_dict({**existing.to_storage(), **changes})
    merged.rule_id = rule_id
    try:
        saved, _ = rule_store.upsert(merged)
    except ValueError as e:
        raise _bad_request(str(e)) from e
    return {"ok": True, "rule": saved.to_dict(), "stats": rule_store.stats()}


@router.put("/rules/{rule_id}/enabled")
async def set_rule_enabled(rule_id: str, payload: EnabledPayload) -> Dict[str, Any]:
    """停用一条规则（不删，方便「最近想多看一眼」时先关掉别的）"""
    rule = rule_store.set_enabled(rule_id, payload.enabled)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return {"ok": True, "rule": rule.to_dict(), "stats": rule_store.stats()}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str) -> Dict[str, Any]:
    if not rule_store.delete(rule_id):
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return {"ok": True, "stats": rule_store.stats()}
