"""
快捷指令接口 —— 输入框 `/` 菜单、命令面板与设置页共用的数据源。

★ 一个写入口

增删改只发生在「设置 → 快捷指令」；输入框与命令面板只读同一份清单。
单一事实来源不是巧合：两处读者如果各自缓存一份，设置页改完那一刻
另一处还是旧的，用户会以为保存没生效。

★ 错误码沿用旧模板接口的约定

内置不可改删 → 403；找不到 → 404；内容不合法 → 400。
前端按这三个分支给不同提示，不把它折成一句「操作失败」。
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.quick_commands import MAX_BODY, MAX_CATEGORY, MAX_TITLE, get_quick_command_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/quick-commands", tags=["quick-commands"])


class QuickCommandWrite(BaseModel):
    """新建 / 修改一条用户指令（内置指令的修改会被 403 拒绝）"""

    title: str = Field(..., min_length=1, max_length=MAX_TITLE)
    body: str = Field(..., min_length=1, max_length=MAX_BODY)
    category: str = Field(default="通用", max_length=MAX_CATEGORY)


@router.get("")
async def list_quick_commands(category: str = ""):
    """全部指令（内置在前）。`?category=` 过滤 —— 菜单按分组渲染时用。"""
    store = get_quick_command_store()
    commands = store.list(category=category or None)
    return {
        "commands": commands,
        "categories": store.categories(),
        "total": len(commands),
    }


@router.post("")
async def create_quick_command(payload: QuickCommandWrite):
    """新建一条用户指令"""
    try:
        command = get_quick_command_store().create(payload.title, payload.body, payload.category)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "command": command}


@router.put("/{command_id}")
async def update_quick_command(command_id: str, payload: QuickCommandWrite):
    """修改一条用户指令（内置指令返回 403）"""
    try:
        command = get_quick_command_store().update(
            command_id,
            {"title": payload.title, "body": payload.body, "category": payload.category},
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "command": command}


@router.delete("/{command_id}")
async def delete_quick_command(command_id: str):
    """删除一条用户指令（内置指令返回 403）"""
    try:
        return get_quick_command_store().delete(command_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
