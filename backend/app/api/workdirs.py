"""
工作区接口 —— 界面上的「选择工作区」背后那一整套。

★ 这里暴露的是一条**授权**通道，不是一个文件浏览器

`POST /workdirs` 会把一个目录写进 Agent 的可达范围（见 `app/workdir/store.py`）。
所以它必须满足两条：
1. 只有用户主动点提交才会发生（没有「顺手同步一下目录」这种隐式写入）；
2. 只登记目录本身，不返回任何文件内容 —— 浏览接口只给子目录名，
   否则这个路由就变成一个绕过文件工具的任意读入口。

★ 绑定按会话（thread）而不是按请求

聊天请求里**没有**工作区字段：本轮在哪个目录干活，只可能来自这张绑定表。
这样一段被注入的文本、一个写错的客户端参数，都没办法把某一句话挪到别的目录里去。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.conversations import conversation_store
from app.conversations.store import is_valid_thread_id
from app.workdir import store as workdir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workdirs", tags=["workdirs"])

#: 非法与不存在统一 404（与会话/工作空间接口同一口径：不暴露「哪些 id 存在」）
_NOT_FOUND = "工作区不存在"


class WorkdirCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="要授权的目录绝对路径")
    name: Optional[str] = Field(None, description="界面显示名，留空取目录名")
    icon: Optional[str] = Field(None, description=f"图标名：{' / '.join(workdir.ICON_NAMES)}")
    color: Optional[str] = Field(None, description="#rrggbb")


class WorkdirUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    path: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None


class BindingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workdir_id: str = Field(
        "",
        description="要绑到的工作区 id；空串 = 不指定（回到沙箱）；"
        f"`{workdir.AUTO_BINDING}` = 由系统分配一个对话产物文件夹",
    )


def _bad_request(e: ValueError) -> HTTPException:
    """登记表抛的 ValueError 原样回给用户 —— 那些话本来就是写给人看的"""
    return HTTPException(status_code=400, detail=str(e))


def _binding_view(thread_id: str) -> dict[str, Any]:
    """
    绑定的对外形状：`mode` 说清是哪一种，`workdir` 给出可渲染的条目。

    ★ 为什么要有 `mode`
    「没绑」「绑了某个目录」「交给系统自动分配」是三种不同的状态，
    只回一个 `workdir_id` 的话，前端得靠「是不是空串 / 是不是那个魔法值」去猜。

    ★ 物化之后 `mode` 仍是 `auto`
    自动目录在第一次用到时会被改写成真实 id（见 `workdir._materialize_auto`），
    但「这段对话的文件夹是系统建的」这件事没有变 —— 界面据此继续显示成
    自动分配的产物（并带上文件夹名），而不是混进用户手选的那一类。
    """
    wid = workdir.get_binding(thread_id)
    if not wid:
        return {"workdir_id": "", "mode": "none", "workdir": None}
    if wid == workdir.AUTO_BINDING:
        return {"workdir_id": wid, "mode": "auto", "workdir": None}
    entry = workdir.get_workdir(wid)
    mode = "auto" if entry and entry.get("origin") == workdir.ORIGIN_AUTO else "manual"
    return {"workdir_id": wid, "mode": mode, "workdir": entry}


# ---------- 清单 ----------

@router.get("")
async def list_workdirs(origin: str = Query("manual", pattern="^(manual|auto|all)$")) -> dict[str, Any]:
    """
    工作区清单（按最近使用排序），每条带 `exists` 与可选图标名。

    默认只列**手动登记**的目录：「选择工作区」那个清单里混进几十个
    `2026102901` 这种自动目录，用户就找不到自己真正点选的项目了。
    自动目录走 `GET /workdirs/auto`（「设置 → 数据」）。
    """
    items = workdir.list_workdirs("" if origin == "all" else origin)
    return {"total": len(items), "workdirs": items, "icons": list(workdir.ICON_NAMES)}


@router.get("/auto")
async def list_auto_workdirs() -> dict[str, Any]:
    """
    对话自动分配的产物文件夹 —— 「设置 → 数据」页的数据源。

    比清单多三样东西：所属对话的**标题**（`thread_id` 对用户没有意义）、
    文件数、占用字节。标题查不到（会话被删了）就留空，界面显示会话 id 前缀。
    """
    items = workdir.auto_workdirs()
    for item in items:
        item["title"] = _thread_title(str(item.get("thread_id") or ""))
    root = workdir.auto_root()
    return {
        "total": len(items),
        "root": str(root) if root else "",
        "bytes": sum(int(item.get("bytes") or 0) for item in items),
        "files": sum(int(item.get("files") or 0) for item in items),
        "workdirs": items,
    }


def _thread_title(thread_id: str) -> str:
    """会话标题；会话不存在/读失败时返回空串（这一页不能因为一条记录而整块失败）"""
    if not is_valid_thread_id(thread_id):
        return ""
    try:
        found = conversation_store.get_conversation(thread_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("读取会话标题失败（已忽略）: %s %s", thread_id, e)
        return ""
    if not found:
        return ""
    summary = found[0] if isinstance(found, tuple) else found
    return str(getattr(summary, "title", "") or "")


@router.post("")
async def create_workdir(payload: WorkdirCreate) -> dict[str, Any]:
    """登记一个工作区（= 授权 Agent 读写这个目录）。"""
    try:
        entry = workdir.create_workdir(
            name=payload.name or "",
            path=payload.path,
            icon=payload.icon or "",
            color=payload.color or "",
        )
    except ValueError as e:
        raise _bad_request(e) from e
    return {"ok": True, "workdir": entry}


@router.patch("/{wd_id}")
async def update_workdir(wd_id: str, payload: WorkdirUpdate) -> dict[str, Any]:
    if not workdir.get_workdir(wd_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    try:
        item = workdir.update_workdir(wd_id, **payload.model_dump(exclude_none=True))
    except ValueError as e:
        raise _bad_request(e) from e
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return {"ok": True, "workdir": item}


@router.delete("/{wd_id}")
async def delete_workdir(
    wd_id: str,
    purge: bool = Query(
        False,
        description="同时删掉磁盘上的文件夹。只对自动分配的工作区生效 —— 手动工作区是你的目录，永远不删",
    ),
) -> dict[str, Any]:
    """
    撤销授权。默认只删登记，磁盘上的目录与文件不动。

    `purge=true` 是「设置 → 数据」页那个删除按钮：自动目录由系统创建、
    只装这段对话的产出，留着只会越积越多。删完之后该会话重新绑回「自动」，
    下次说话会拿到一个新文件夹 —— 否则那个会话会静默退回沙箱。
    """
    entry = workdir.get_workdir(wd_id)
    purged = workdir.delete_workdir(wd_id, purge=purge)
    thread_id = str(entry.get("thread_id") or "") if entry else ""
    if purged and entry and entry.get("origin") == workdir.ORIGIN_AUTO and thread_id:
        try:
            workdir.set_binding(thread_id, workdir.AUTO_BINDING)
        except ValueError as e:  # noqa: BLE001
            logger.debug("删除自动工作区后重新绑定失败（已忽略）: %s %s", thread_id, e)
    return {"ok": True, "deleted": purged, "purged": bool(purge and purged)}


# ---------- 目录浏览（只列子目录，不读内容） ----------

@router.get("/roots")
async def list_roots() -> dict[str, Any]:
    """目录选择器的快捷入口：主目录 / 桌面 / 文档 / 下载 / 各盘根。"""
    roots = workdir.quick_roots()
    return {"total": len(roots), "roots": roots}


@router.get("/browse")
async def browse_dir(
    path: str = Query(..., description="要列出的目录（绝对路径）"),
    query: str = Query("", description="按名字过滤子目录"),
) -> dict[str, Any]:
    try:
        return workdir.browse(path, query)
    except ValueError as e:
        raise _bad_request(e) from e


# ---------- 会话绑定 ----------

@router.get("/binding/{thread_id}")
async def get_thread_binding(thread_id: str) -> dict[str, Any]:
    """
    这个会话当前在哪个工作区里干活。

    `mode` 有三种：`none`（不指定，落沙箱）/ `manual`（绑了用户点选的目录）/
    `auto`（由系统分配产物文件夹，目录在第一次用到时才建）。
    非法与未绑定都是 `none` —— 这不是错误，「不指定工作区」是一个正常状态。
    """
    if not is_valid_thread_id(thread_id):
        return {"workdir_id": "", "mode": "none", "workdir": None}
    return _binding_view(thread_id)


@router.put("/binding/{thread_id}")
async def set_thread_binding(thread_id: str, payload: BindingPayload) -> dict[str, Any]:
    """把会话绑到某个工作区（空串 = 解绑回沙箱；`auto` = 交给系统分配）。"""
    try:
        workdir.set_binding(thread_id, payload.workdir_id)
    except ValueError as e:
        raise _bad_request(e) from e
    return {"ok": True, **_binding_view(thread_id)}
