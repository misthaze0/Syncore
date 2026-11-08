"""
长期记忆接口 —— 查看 / 手动增删 / 清空

★ 为什么状态接口必须自己解析惰性单例

架构不变量 23（`judge_status()` 谎报判定后端不可用）就是在这类地方踩出来的：
「读取一个惰性构造的对象」和「让它被构造」是两件事。这里 manager 通过
`get_manager()` 主动取，而不是去读某个模块级私有变量。

★ 为什么提供「清空」

记忆是这个项目里**唯一会跨会话持续影响输出**的状态。
用户必须能一键回到干净状态 —— 没有这个出口，一条错误记忆会永久污染回答，
而且用户没有任何办法自救。这属于功能的一部分，不是可选项。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.memory import CATEGORIES, SOURCES, get_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryCreateRequest(BaseModel):
    """手动新增 / 覆盖一条偏好"""

    value: str = Field(..., min_length=1, max_length=500, description="偏好内容")
    key: Optional[str] = Field(
        None,
        max_length=64,
        description="去重键。留空则按类别派生（自动抽取就是这么做的，同类别只留最新一条）",
    )
    category: Optional[str] = Field(
        None,
        description=f"类别，可选：{' / '.join(CATEGORIES.keys())}；留空则按内容推断",
    )
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    pinned: bool = False


class MemoryUpdateRequest(BaseModel):
    """编辑一条已有偏好（key 与来源不变，旧值自动进 superseded 历史）"""

    value: str = Field(..., min_length=1, max_length=500, description="修正后的偏好内容")
    category: Optional[str] = Field(None, description="留空保持原类别")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0, description="留空保持原置信度")
    pinned: Optional[bool] = None


@router.get("")
async def list_memory() -> Dict[str, Any]:
    """列出全部长期记忆 + 统计"""
    manager = get_manager()
    items = manager.list_items()
    return {
        "items": [manager.item_payload(i) for i in items],
        "stats": manager.stats(),
        "categories": CATEGORIES,
        "sources": SOURCES,
    }


@router.post("")
async def create_memory(payload: MemoryCreateRequest) -> Dict[str, Any]:
    """
    手动添加或覆盖一条偏好。

    与自动抽取的差别：这里由调用方指定 `key`，因此可以并存多条同类偏好。
    自动抽取按类别去重（避免互相矛盾的偏好同时进 Prompt），手动添加则不受此限制。
    """
    manager = get_manager()
    result = manager.add_manual(
        value=payload.value,
        key=payload.key or "",
        category=payload.category or "",
        confidence=payload.confidence,
        pinned=payload.pinned,
    )
    if result is None:
        raise HTTPException(status_code=400, detail="value 不能为空")
    return {
        "ok": True,
        "action": result.action,
        "item": manager.item_payload(result.item),
    }


@router.put("/{key}")
async def update_memory(key: str, payload: MemoryUpdateRequest) -> Dict[str, Any]:
    """
    编辑一条已有偏好。

    与「删了重建」的差别：key 不变（历史留痕不断）、来源不变
    （手动条目仍受 protected 保护）、旧值自动进 superseded 可追溯。
    """
    manager = get_manager()
    result = manager.update_item(
        key,
        value=payload.value,
        category=payload.category or "",
        confidence=payload.confidence,
        pinned=payload.pinned,
    )
    if result is None:
        existing = manager.store.get(key)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"没有找到记忆: {key}")
        raise HTTPException(status_code=400, detail="value 不能为空")
    return {
        "ok": True,
        "action": result.action,
        "item": manager.item_payload(result.item),
    }


@router.post("/{key}/restore")
async def restore_memory(key: str) -> Dict[str, Any]:
    """恢复一条已失效（被否定）的记忆 —— 用户的显式动作，不需要再被保护拦截"""
    manager = get_manager()
    ok = manager.reactivate_item(key)
    if not ok:
        item = manager.store.get(key)
        if item is None:
            raise HTTPException(status_code=404, detail=f"没有找到记忆: {key}")
        raise HTTPException(status_code=400, detail="该记忆当前是有效状态，无需恢复")
    item = manager.store.get(key)
    return {"ok": True, "restored": key, "item": manager.item_payload(item) if item else None}


@router.post("/{key}/reinforce")
async def reinforce_memory(key: str) -> Dict[str, Any]:
    """用户手动确认旧记忆；沉睡可以回弹，已被否定的记忆必须先明确恢复。"""
    manager = get_manager()
    item = manager.store.get(key)
    if item is None:
        raise HTTPException(status_code=404, detail=f"没有找到记忆: {key}")
    if not item.active:
        raise HTTPException(status_code=400, detail="这条记忆已失效，请先恢复")
    if not manager.stats()["enabled"]:
        raise HTTPException(status_code=400, detail="长期记忆功能当前已关闭")
    changed = manager.reinforce_item(key)
    item = manager.store.get(key)
    return {"ok": True, "reinforced": changed, "item": manager.item_payload(item) if item else None}


@router.delete("/owner")
async def clear_owner_memory() -> Dict[str, Any]:
    """
    清空「关于用户」的记忆 —— 只删关系/生活类事实，不动工作偏好。

    ★ 必须声明在 `/{key}` 之前：`owner` 完全符合 `{key}` 的形态，
    写在后面前端点「清空」会被当成「删除 key 叫 owner 的记忆」，
    返回 404 而用户以为按钮坏了。
    """
    manager = get_manager()
    n = manager.clear_owner()
    logger.info("关系记忆已清空：%s 条", n)
    return {"ok": True, "cleared": n}


@router.delete("/{key}")
async def delete_memory(key: str) -> Dict[str, Any]:
    """忘记一条（key 即记忆的 id）"""
    manager = get_manager()
    ok = manager.delete(key)
    if not ok:
        raise HTTPException(status_code=404, detail=f"没有找到记忆: {key}")
    return {"ok": True, "deleted": key}


@router.delete("")
async def clear_memory() -> Dict[str, Any]:
    """清空全部长期记忆（不可撤销）"""
    manager = get_manager()
    n = manager.clear()
    logger.info("长期记忆已清空：%s 条", n)
    return {"ok": True, "cleared": n}


@router.get("/prompt-block")
async def memory_prompt_block() -> Dict[str, Any]:
    """
    返回当前会注入 System Prompt 的那段文本。

    存在的意义是**可验证**：调一次就能确认「模型到底看到了什么」，
    不必去猜记忆有没有生效、有没有被截断。

    偏好块与关系记忆块分开返回：两者的注入位置不同（系统提示的固定段 vs
    人设的动态段），拼在一起展示会让用户以为它们总是相邻出现。
    """
    manager = get_manager()
    block = manager.build_prompt_block()
    owner_block = manager.build_owner_block()
    return {
        "block": block,
        "chars": len(block),
        "empty": not block,
        "owner_block": owner_block,
        "owner_chars": len(owner_block),
    }
