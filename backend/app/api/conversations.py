"""
会话历史接口 —— 列表 / 详情 / 改名 / 删除 / 清空 / 恢复上下文

★ 详情返回的是**轮次**，不是渲染好的消息

每个轮次带 `user_input` / `answer` / `events`（这一轮产生过的 SSE 事件快照）。
前端拿它做的是「把事件重放一遍」——用的是和实时流完全相同的那套 reducer。
好处是工具卡片、引用来源、审批留痕、记忆回执的渲染逻辑只有一份，
不会出现「历史会话看起来和当时不一样」这种只在恢复路径上冒出来的偏差。

★ 为什么 thread_id 要校验

它会直接进文件路径与 URL。`../../backend/.env` 这种输入若被放行，
一个删除请求就能删掉仓库里的任意文件。校验集中在
`conversations.is_valid_thread_id()`（只允许字母数字与 `-` / `_`），
这里对非法输入统一返回 404 —— **不区分「不存在」与「非法」**，
免得把「哪些路径存在」变成可探测的信息。
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app import artifacts as artifact_store
from app.config import settings
from app.conversations import conversation_store, is_valid_thread_id
from app.conversations.models import events_for_replay
from app.core.sandbox import resolve_in_sandbox

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

_NOT_FOUND = "没有找到该会话"
_FILE_NOT_FOUND = "没有找到该文件（可能已被删除或移动）"

#: 打包下载的硬上限 —— 单次把几百 MB 塞进内存会把服务打挂
MAX_ZIP_BYTES = 200 * 1024 * 1024

#: 画布预览单次读取的字节上限。超出部分截断并**如实标注** ——
#: 画布是给人看的，一个几 MB 的 HTML 全塞给前端既没有意义，也会拖垮渲染。
MAX_PREVIEW_BYTES = 512 * 1024


def _list_limit_cap() -> int:
    """
    列表接口单次返回条数的硬上限。

    这个上限刻意**在调用时**从配置读，而不是在模块导入时算成常量：
    本项目所有配置都走「改 `.env` → 重启即生效」的路径，导入期求值会让
    `CONVERSATION_LIST_LIMIT` 变成一条改了也不生效的假配置（它曾经就是这样 ——
    定义了却没有任何地方引用）。同理测试才能 monkeypatch 它。
    """
    return max(1, int(getattr(settings, "CONVERSATION_LIST_LIMIT", 100) or 100))


class RenameRequest(BaseModel):
    """改标题（只影响索引，不动轮次文件）"""

    title: str = Field(..., min_length=1, max_length=200, description="新的会话标题")


class ForkRequest(BaseModel):
    """从前 N 轮分支出一个新会话"""

    turns: int = Field(..., ge=1, le=1000, description="复制前多少轮（含第 N 轮）")


def _turn_payload(turn: Any) -> Dict[str, Any]:
    data = turn.to_dict()
    # 防御性再过滤一次：写入侧已经剔除过 token/done，但手工编辑过的
    # 历史文件可能带着它们 —— 重放时它们会让回答内容翻倍。
    data["events"] = events_for_replay(data.get("events") or [])
    data["status_label"] = turn.status_label
    return data


@router.get("")
async def list_conversations(
    limit: int = Query(50, ge=1, description="本页条数（超过 CONVERSATION_LIST_LIMIT 会被收敛到该上限）"),
    offset: int = Query(0, ge=0, description="跳过条数（按最后活跃时间倒序）"),
    scope: str = Query(
        "main",
        pattern="^(main|assistant)$",
        description="main=普通对话（默认），assistant=助理命名空间的对话",
    ),
) -> Dict[str, Any]:
    """
    会话列表（最近聊的在最前）+ 统计

    条数上限来自配置（`CONVERSATION_LIST_LIMIT`，默认 100）并在服务端收敛，
    而不是靠调用方自觉 —— 它的存在意义就是「防止一次把几百个会话推给前端」。

    ★ 过滤员工私聊 / 子任务线程

    员工私聊（`emp_`）与后台子任务（`subtask_`）的会话历史复用同一个
    `conversation_store`，但**不该**出现在用户主对话侧栏 —— 它们是别的
    生命周期里的东西，混进来会让「我的聊天」列表被内部线程刷屏。

    ★ 助理会话（`assistant_`）按 scope 二选一

    助理是独立命名空间：普通侧栏（scope=main）看不到助理会话，
    助理入口（scope=assistant）也看不到普通对话 —— 两边永不混流。
    """
    limit = min(limit, _list_limit_cap())
    from app.agents.subgraph import is_subtask_thread_id
    from app.conversations import is_assistant_thread_id
    from app.workspace import is_employee_thread_id

    # 先取全量（store 内部已被 CONVERSATION_MAX_ITEMS 兜底），过滤掉内部线程后再分页，
    # 保证 `total` 是「用户可见会话」的真实数量，而不是「含员工私聊」的虚高。
    stored = conversation_store.list_conversations(limit=1000, offset=0)
    items, _all = stored
    if scope == "assistant":
        visible = [s for s in items if is_assistant_thread_id(s.thread_id)]
    else:
        visible = [
            s for s in items
            if not is_subtask_thread_id(s.thread_id)
            and not is_employee_thread_id(s.thread_id)
            and not is_assistant_thread_id(s.thread_id)
        ]
    total = len(visible)
    page = visible[offset : offset + limit]
    return {
        "total": total,
        "count": len(page),
        "offset": offset,
        "limit": limit,
        "conversations": [s.to_dict() for s in page],
        "stats": conversation_store.stats(),
    }


@router.get("/search")
async def search_conversations(
    q: str = Query("", max_length=100, description="关键词（在用户输入与助手回答里匹配）"),
    limit: int = Query(30, ge=1, le=100, description="最多返回多少个会话"),
    scope: str = Query(
        "main",
        pattern="^(main|assistant)$",
        description="只搜某个命名空间：main=普通对话（默认），assistant=助理对话",
    ),
) -> Dict[str, Any]:
    """
    跨会话搜「对话正文」—— 命中会话 + 片段（三段式，供前端高亮）。

    ★ 路由顺序不是风格问题：它**必须**声明在 `/{thread_id}` 之前

    FastAPI 按声明顺序做匹配，而 `/search` 完全符合 `/{thread_id}` 的形态。
    若把它写在后面，这个请求会被「读一个 thread_id 叫 `search` 的会话」接走，
    返回一句「没有找到该会话」—— 排查时几乎没人会先想到是路由顺序。

    ★ 空关键词返回空结果，而不是全部会话

    「搜空串」在语义上不是「列出所有东西」。真那样做的话，前端一打开搜索框
    就会被几百个会话淹没，还得为它单独准备一套分页 —— 而用户要的列表
    本来就在 `/conversations` 上，不必再走一遍搜索。

    ★ 搜索范围与列表一致

    侧栏搜索是列表的放大镜：列表里看不到的命名空间（员工私聊、子任务、
    以及另一个 scope 的助理/普通对话）搜索结果里也不该冒出来。
    """
    needle = " ".join((q or "").split())
    if not needle:
        return {"success": True, "query": "", "total": 0, "results": []}
    from app.agents.subgraph import is_subtask_thread_id
    from app.conversations import is_assistant_thread_id
    from app.workspace import is_employee_thread_id

    results = conversation_store.search(needle, limit=200)
    if scope == "assistant":
        results = [r for r in results if is_assistant_thread_id(r.get("thread_id", ""))]
    else:
        results = [
            r for r in results
            if not is_subtask_thread_id(r.get("thread_id", ""))
            and not is_employee_thread_id(r.get("thread_id", ""))
            and not is_assistant_thread_id(r.get("thread_id", ""))
        ]
    results = results[:limit]
    return {"success": True, "query": needle, "total": len(results), "results": results}


@router.get("/{thread_id}")
async def get_conversation(thread_id: str) -> Dict[str, Any]:
    """一个会话的全部轮次（前端据此重放界面）"""
    if not is_valid_thread_id(thread_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    loaded = conversation_store.get_conversation(thread_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    summary, turns = loaded
    return {
        "success": True,
        "conversation": summary.to_dict(),
        "total": len(turns),
        "turns": [_turn_payload(t) for t in turns],
    }


# ---------- 本轮产出的文件（交付物） ----------
def _load_turns_or_404(thread_id: str) -> List[Any]:
    if not is_valid_thread_id(thread_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    loaded = conversation_store.get_conversation(thread_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return loaded[1]


def _file_entry(
    path: str,
    tool: str,
    turn: Any,
    revisions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """把「轮次里记的一条相对路径」补成界面能直接用的条目（现算磁盘状态）"""
    entry: Dict[str, Any] = {
        "path": path,
        "name": Path(path).name,
        "suffix": Path(path).suffix.lower().lstrip("."),
        "tool": tool,
        "turn_id": getattr(turn, "turn_id", ""),
        "created_at": getattr(turn, "created_at", ""),
        "exists": False,
        "size": 0,
        # 历史版本按「内容变过」生成，最新的在最后
        "revisions": revisions or [],
    }
    try:
        target = resolve_in_sandbox(path)
    except PermissionError:
        # 沙箱配置换过 / 历史里存了越权路径 —— 如实标记「不在」，不抛给前端
        return entry
    if target.is_file():
        entry["exists"] = True
        entry["size"] = target.stat().st_size
    return entry


@router.get("/{thread_id}/files")
async def list_conversation_files(thread_id: str) -> Dict[str, Any]:
    """
    这个会话**产出过**的文件（跨全部轮次）。

    ★ 为什么是「从轮次里取」而不是「扫工作目录」

    工作目录里可能有用户自己丢进去的文件、上一次演示留下的垃圾。
    用户问的是「你刚才给我做了什么」，答案必须**限定在这次会话真的写过的东西**上
    —— 判定依据是事件流里成功的 `write` / `generate_document` 调用
    （见 `conversations/files.py`）。

    同一路径在多轮里被写过只算一条（取最早那次），但 `exists` / `size` 是现算的：
    文件被删掉之后界面该显示「已不在了」，而不是继续给一个点不动的下载按钮。
    """
    turns = _load_turns_or_404(thread_id)
    revisions = artifact_store.revision_summary(thread_id)

    files: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for turn in turns:
        for item in getattr(turn, "files", []) or []:
            path = str(item.get("path") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            files.append(
                _file_entry(
                    path,
                    str(item.get("tool") or ""),
                    turn,
                    revisions.get(path),
                )
            )

    # 最新的排前面：用户最关心「刚才这一轮产出了什么」
    files.reverse()
    return {
        "success": True,
        "thread_id": thread_id,
        "total": len(files),
        "available": sum(1 for f in files if f["exists"]),
        "files": files,
    }


async def _produced_file_or_404(thread_id: str, path: str) -> Path:
    """
    把「这个会话**真的产出过**的相对路径」解析成沙箱内的真实文件，否则 404。

    ★ 为什么这段校验必须只有一处

    它同时承担两条安全职责，任何一条单独看都足以致命：
    1. **只认本会话产出过的路径** —— 否则 `thread_id` 会退化成一个「猜路径」的入口，
       凭一个会话就能读沙箱里的任意文件；
    2. 复用 `resolve_in_sandbox()` —— `path` 直接来自 URL，不校验就等于开了一个
       「读服务器任意文件」的接口。

    下载、预览、以后的任何读取接口都走这里。抄第二份，就一定会有一份先漂移
    （而不变量 12 与 47 都指着这条边界）。

    越权与不存在**统一 404** —— 不区分，免得把「哪些路径存在」变成可探测的信息。
    """
    files = await list_conversation_files(thread_id)
    if path not in {f["path"] for f in files["files"]}:
        raise HTTPException(status_code=404, detail=_FILE_NOT_FOUND)

    try:
        target = resolve_in_sandbox(path)
    except PermissionError:
        raise HTTPException(status_code=404, detail=_FILE_NOT_FOUND) from None

    if not target.is_file():
        raise HTTPException(status_code=404, detail=_FILE_NOT_FOUND)
    return target


@router.get("/{thread_id}/files/content")
async def read_conversation_file_content(
    thread_id: str,
    path: str = Query(..., description="会话产出文件的相对路径（来自 /files 的 path 字段）"),
) -> Dict[str, Any]:
    """
    读取一个产出文件的**文本内容**，供前端画布（Artifacts）直接渲染。

    ★ 为什么预览要单独一个接口，而不是复用下载接口

    `download` 返回 `FileResponse`：浏览器拿到它只会存成文件，前端**拿不到内容**，
    也就无从在画布里渲染。这个接口把内容作为文本返回，前端可以直接渲染。
    两者的安全边界完全一致（同一个 `_produced_file_or_404`）。

    ★ 为什么「是不是文本」由后端判定

    只有读过字节的一方才说得准。前端按扩展名猜会两头出错：
    把 `.docx` 当文本解码成乱码、又认不出没有扩展名的纯文本。
    所以这里如实返回 `is_text`，前端只负责「拿到文本之后怎么渲染」——
    呈现方式的映射（html → 画布 / md → Markdown / csv → 表格）在前端，
    那是纯粹的展示决策。

    ★ 截断的三个约定

    1. 超上限就截断，并置 `truncated=true` —— 界面必须说出来，
       否则用户会以为文件本身就这么短。
    2. 截断点可能切断一个多字节字符，这种解码失败**不判成二进制**
       （那是「读到的字节不是 UTF-8」，不是「文件不是文本」）——
       丢掉那半个字符继续，比把一个正常 HTML 报成「无法预览」好得多。
    3. `size` 始终是**文件真实大小**，不是读到的字节数。
    """
    target = await _produced_file_or_404(thread_id, path)

    size = target.stat().st_size
    from app.office.preview import INLINE_MEDIA, archive_preview, office_preview, workbook_preview

    suffix = target.suffix.lower()
    preview = None
    if suffix in INLINE_MEDIA:
        return {"success": True, "path": path, "name": target.name, "suffix": suffix[1:],
                "size": size, "is_text": False, "content": None, "truncated": False}
    parser = {".xlsx": workbook_preview, ".xlsm": workbook_preview,
              ".docx": office_preview, ".pptx": office_preview, ".zip": archive_preview}.get(suffix)
    if parser:
        try:
            preview = await run_in_threadpool(parser, target)
        except Exception as exc:
            logger.info("文件预览解析失败 %s: %s", target.name, exc)
            raise HTTPException(status_code=422, detail="无法预览该文件：文件可能已损坏、已加密或超过预览上限，请下载查看") from exc
        return {"success": True, "path": path, "name": target.name, "suffix": suffix[1:],
                "size": size, "is_text": False, "content": None,
                "truncated": preview.get("truncated", False), "preview": preview}
    with target.open("rb") as stream:
        raw = stream.read(MAX_PREVIEW_BYTES)
    truncated = size > MAX_PREVIEW_BYTES

    payload: Dict[str, Any] = {
        "success": True,
        "path": path,
        "name": target.name,
        "suffix": target.suffix.lower().lstrip("."),
        "size": size,
        "is_text": True,
        "content": None,
        "truncated": truncated,
    }

    if b"\x00" in raw and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        payload["is_text"] = False
        return payload

    try:
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        payload["content"] = raw.decode(encoding)
    except UnicodeDecodeError:
        if not truncated:
            payload["is_text"] = False
            return payload
        # Only a partial trailing code point may be discarded. Invalid bytes
        # earlier in the file mean binary content, even when size exceeds the cap.
        try:
            payload["content"] = raw.decode(encoding, errors="strict")
        except UnicodeDecodeError as exc:
            if exc.end != len(raw) or exc.reason not in {"unexpected end of data", "truncated data"}:
                payload["is_text"] = False
                return payload
            payload["content"] = raw[:exc.start].decode(encoding)

    return payload


class CanvasSaveRequest(BaseModel):
    path: str
    content: str = Field(max_length=MAX_PREVIEW_BYTES)
    original: str = Field(max_length=MAX_PREVIEW_BYTES)


@router.post("/{thread_id}/files/save")
async def save_canvas_file(thread_id: str, payload: CanvasSaveRequest) -> Dict[str, Any]:
    target = await _produced_file_or_404(thread_id, payload.path)
    current = await read_conversation_file_content(thread_id, payload.path)
    if not current["is_text"] or current["truncated"]:
        raise HTTPException(422, "仅支持完整文本文件编辑")
    if current["content"] != payload.original:
        raise HTTPException(409, "文件已在其他位置修改，请重新打开后再编辑")
    if len(payload.content.encode("utf-8")) > MAX_PREVIEW_BYTES:
        raise HTTPException(413, "编辑内容超过大小限制")
    import os
    import tempfile
    # Same-directory atomic replacement; never leave a partially written artifact.
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".canvas-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(payload.content)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"success": True}


@router.get("/{thread_id}/files/convert")
async def convert_canvas_file(thread_id: str, path: str, format: str) -> FileResponse:
    from app.office.converter import convert, OUTPUT_SUFFIXES
    from starlette.background import BackgroundTask
    import tempfile
    import shutil
    source = await _produced_file_or_404(thread_id, path)
    if "." + format not in OUTPUT_SUFFIXES:
        raise HTTPException(422, "不支持的目标格式")
    folder = Path(tempfile.mkdtemp(prefix="canvas-export-"))
    target = folder / (source.stem + "." + format)
    try:
        await run_in_threadpool(convert, source, target)
    except Exception as exc:
        shutil.rmtree(folder)
        raise HTTPException(422, str(exc)) from exc
    return FileResponse(target, filename=target.name, media_type="application/octet-stream",
                        background=BackgroundTask(shutil.rmtree, folder))


@router.get("/{thread_id}/files/inline")
async def inline_conversation_file(thread_id: str, path: str = Query(...)) -> FileResponse:
    """Same conversation boundary as downloads; only passive media may render inline."""
    from app.office.preview import INLINE_MEDIA

    target = await _produced_file_or_404(thread_id, path)
    mime = INLINE_MEDIA.get(target.suffix.lower())
    if not mime:
        raise HTTPException(status_code=415, detail="该格式不支持内嵌浏览")
    return FileResponse(str(target), media_type=mime, filename=target.name,
                        content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff",
                        "Content-Security-Policy": "default-src 'none'"})


@router.get("/{thread_id}/files/download")
async def download_conversation_file(
    thread_id: str,
    path: str = Query(..., description="会话产出文件的相对路径（来自 /files 的 path 字段）"),
) -> FileResponse:
    """下载单个产出文件（路径校验见 `_produced_file_or_404`）"""
    target = await _produced_file_or_404(thread_id, path)

    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream",
    )


@router.get("/{thread_id}/files/zip")
async def download_conversation_files_zip(thread_id: str) -> Response:
    """把这一轮/这个会话的产出打包成一个 zip（用户一次拿走全部成果）"""
    files = await list_conversation_files(thread_id)
    available = [f for f in files["files"] if f["exists"]]
    if not available:
        raise HTTPException(status_code=404, detail="这个会话还没有可下载的文件")

    buffer = io.BytesIO()
    total = 0
    written = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in available:
            try:
                target = resolve_in_sandbox(entry["path"])
            except PermissionError:
                continue
            if not target.is_file():
                continue
            if total + entry["size"] > MAX_ZIP_BYTES:
                logger.warning("打包超过上限，已截断: thread=%s", thread_id)
                break
            zf.write(target, arcname=entry["path"])
            total += entry["size"]
            written += 1

    if written == 0:
        raise HTTPException(status_code=404, detail="文件都已不存在，无法打包")

    filename = f"{thread_id[:8]}-files.zip"
    headers = {
        "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
        "X-File-Count": str(written),
    }
    return Response(content=buffer.getvalue(), media_type="application/zip", headers=headers)


class RevisionRestoreRequest(BaseModel):
    """恢复产物历史版本时的入参"""

    path: str = Field(..., min_length=1)
    revision_id: str = Field(..., min_length=1)


async def _ensure_produced_path(thread_id: str, path: str) -> None:
    """
    版本接口与下载接口共用同一条边界：
    **只认这个会话真的产出过的路径**，否则 thread_id 就退化成了猜路径入口。
    """
    files = await list_conversation_files(thread_id)
    if path not in {f["path"] for f in files["files"]}:
        raise HTTPException(status_code=404, detail=_FILE_NOT_FOUND)


@router.get("/{thread_id}/files/revisions/download")
async def download_conversation_file_revision(
    thread_id: str,
    path: str = Query(..., description="会话产出文件的相对路径"),
    revision_id: str = Query(..., description="来自 /files 的 revisions[].revision_id"),
) -> FileResponse:
    """下载某个历史版本（不改变磁盘上的当前文件）"""
    await _ensure_produced_path(thread_id, path)
    target = artifact_store.open_revision(thread_id, path, revision_id)
    if target is None:
        raise HTTPException(status_code=404, detail="没有找到这个历史版本")

    suffix = target.suffix or Path(path).suffix
    filename = f"{Path(path).stem}-{revision_id[:15]}{suffix}"
    return FileResponse(
        path=str(target),
        filename=filename,
        media_type="application/octet-stream",
    )


@router.post("/{thread_id}/files/revisions/restore")
async def restore_conversation_file_revision(
    thread_id: str,
    payload: RevisionRestoreRequest,
) -> Dict[str, Any]:
    """
    把历史版本写回沙箱里的当前文件。

    恢复也是一次内容变更：版本库会给它记一条 `restored_from`，
    历史链条不会因为「手工恢复」而断开。
    """
    await _ensure_produced_path(thread_id, payload.path)
    try:
        target = resolve_in_sandbox(payload.path)
    except PermissionError:
        raise HTTPException(status_code=404, detail=_FILE_NOT_FOUND) from None

    if not artifact_store.restore_revision(
        thread_id, payload.path, payload.revision_id, target
    ):
        raise HTTPException(status_code=404, detail="没有找到这个历史版本")

    return {
        "success": True,
        "path": payload.path,
        "revision_id": payload.revision_id,
    }


@router.patch("/{thread_id}")
async def rename_conversation(thread_id: str, payload: RenameRequest) -> Dict[str, Any]:

    """改会话标题"""
    if not is_valid_thread_id(thread_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    summary = conversation_store.rename(thread_id, payload.title)
    if summary is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return {"ok": True, "conversation": summary.to_dict()}


@router.post("/{thread_id}/fork")
async def fork_conversation(thread_id: str, payload: ForkRequest) -> Dict[str, Any]:
    """
    把该会话的前 N 轮复制成一个**新会话**（「从这里分支」）。

    使用场景：某轮回答跑偏了，用户想从更早的那条消息另起一条线重问 ——
    保留之前的上下文，但不覆盖原会话。

    新 thread_id 由服务端生成（uuid4）；模型上下文不用在这里播种 ——
    前端随后调 `POST /{thread_id}/resume`，`seed_thread_from_history`
    发现 Checkpointer 里没有新会话时自然会从复制的轮次文件播。
    """
    if not is_valid_thread_id(thread_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    summary = conversation_store.fork_conversation(thread_id, payload.turns)
    if summary is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return {"ok": True, "conversation": summary.to_dict()}


async def _drop_thread_contexts(thread_ids: Iterable[str]) -> int:
    """
    把一批 thread 的模型上下文从 Checkpointer 里删掉，返回成功条数。

    删了历史文件却不删上下文，模型下一轮仍会带着它回答 ——
    用户看到的是「删了却还记得」。失败只告警：删除历史本身已经是用户要的结果。

    ⚠️ 子图的上下文不是会话 id，而是 `subtask_<task_id>`；
    调用方要先用 `subtask_thread_id()` 换算再传进来。
    """
    ids = [tid for tid in dict.fromkeys(thread_ids) if tid]
    if not ids:
        return 0
    try:
        from app.agents.graph import get_graph

        graph = get_graph()
    except Exception as e:  # noqa: BLE001
        logger.warning("删除上下文时拿不到图（历史已删除）: %s", e)
        return 0
    checkpointer = getattr(graph, "checkpointer", None) if graph else None
    if checkpointer is None or not hasattr(checkpointer, "adelete_thread"):
        return 0
    dropped = 0
    for tid in ids:
        try:
            await checkpointer.adelete_thread(tid)
            # 摘要与落盘的工具输出都是从上下文派生的用户数据，
            # 上下文删了它们也一起删（否则「删了却还记得」换个形式出现）。
            try:
                from app.agents.context import (
                    purge_thread_summaries,
                    purge_thread_tool_results,
                )

                purge_thread_summaries(tid)
                purge_thread_tool_results(tid)
            except Exception as e:  # noqa: BLE001
                logger.warning("清理上下文派生数据失败: thread=%s err=%s", tid, e)
            dropped += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("删除会话上下文失败: thread=%s err=%s", tid, e)
    return dropped


async def _drop_task_contexts(task_ids: Iterable[str]) -> int:
    """删掉子任务在 Checkpointer 里的 `subtask_<task_id>` 状态"""
    from app.agents.subgraph import subtask_thread_id

    return await _drop_thread_contexts(subtask_thread_id(tid) for tid in task_ids)


def _revoke_grants(thread_id: str) -> int:
    """
    吊销这个会话留下的预授权。

    预授权是内存态（15 分钟 TTL、次数上限），过期自清；但会话都删了还让它
    继续替一个不存在的对话自动放行危险操作，没有任何正当理由。
    """
    try:
        from app.approval import grant_registry

        return grant_registry.revoke_thread(thread_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("吊销会话预授权失败: thread=%s err=%s", thread_id, e)
        return 0


async def _delete_thread_tasks(thread_id: str) -> int:
    """删掉会话的后台子任务，并同步清掉它们的子图上下文；返回删掉的数量"""
    from app.tasks.store import task_store

    task_ids = task_store.delete_by_thread(thread_id)
    await _drop_task_contexts(task_ids)
    # 子任务的上下文摘要按 `sub:<thread>:<task>` 存，跟着任务一起清
    try:
        from app.agents.context import purge_scope_summary

        for task_id in task_ids:
            purge_scope_summary(f"sub:{thread_id}:{task_id}")
    except Exception as e:  # noqa: BLE001
        logger.warning("清理子任务摘要失败（任务已删除）: %s", e)
    return len(task_ids)


@router.delete("/{thread_id}/turns/{turn_index}")
async def delete_conversation_turn(thread_id: str, turn_index: int) -> Dict[str, Any]:
    """
    删除该会话的第 `turn_index` 轮（0 基，一问一答算一轮）。

    ★ 轮次全删光时会话本身也被删掉 —— `conversation` 返回 null，
    前端据此切到新会话（不能留一个点进去空空的死壳）。

    ★ 模型上下文同步删掉这一轮

    只删轮次文件是半吊子：下一次提问模型仍然带着这一轮的上下文，
    用户会看到「删了却还记得」。这里用 `RemoveMessage` 按 id 摘掉
    「本轮起点 ~ 下一轮起点」之间的全部消息（含工具消息），其余不动；
    找不到锚点的旧轮次则按剩余文件重建上下文。
    """
    if not is_valid_thread_id(thread_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    deleted, summary, removed_turn = conversation_store.remove_turn(thread_id, turn_index)
    if not deleted:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    # ---------- 同步模型上下文 ----------
    # 与 `delete_conversation` 同一边界：历史已经删了，这件事本身就是用户要的结果；
    # 上下文同步失败只告警，不让整个删除请求变成 500。
    context: Dict[str, Any] = {"removed": 0, "reseeded": False, "reason": ""}
    try:
        from app.agents.graph import (
            discard_thread_context,
            get_graph,
            remove_turn_messages_from_context,
        )

        graph = get_graph()
        if summary is None:
            # 会话整个下线：上下文没有留下的理由
            context = await discard_thread_context(graph, thread_id)
        elif removed_turn is not None:
            context = await remove_turn_messages_from_context(
                graph,
                thread_id,
                turn_id=removed_turn.turn_id,
                user_input=removed_turn.user_input,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("删除轮次后同步模型上下文失败（轮次已删除）: %s", e)
        context["reason"] = f"同步上下文失败: {e}"

    # ---------- 最后一轮删掉 = 会话下线，收尾与 delete_conversation 一致 ----------
    # 否则这个会话的后台任务会变成「查不到来源」的悬空记录，
    # 预授权也会继续替一个已经不存在的对话放行危险操作。
    tasks_deleted: Optional[int] = None
    if summary is None:
        _revoke_grants(thread_id)
        try:
            tasks_deleted = await _delete_thread_tasks(thread_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除会话的后台任务失败（会话已删除）: %s", e)

    return {
        "ok": True,
        "conversation": summary.to_dict() if summary else None,
        "context": context,
        "tasks_deleted": tasks_deleted,
    }


@router.post("/{thread_id}/resume")
async def resume_conversation(thread_id: str) -> Dict[str, Any]:
    """
    确保这个会话的**模型上下文**可用（必要时从历史播种）。

    存在的意义与 `/memory/prompt-block` 一致：让一件本来只能靠猜的事变成**可验证**。
    正常情况（同一个 Redis 还在跑）它什么都不做并如实报告「上下文仍在线」；
    只有在 Checkpointer 真的丢了这份状态时才会播种 —— 而不是默默让模型失忆。

    打开历史会话时前端会顺手调一次，于是「这次恢复到底做没做」在界面上是可见的。
    """
    if not is_valid_thread_id(thread_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    from app.agents.graph import get_graph, seed_thread_from_history

    result = await seed_thread_from_history(get_graph(), thread_id)
    return {"success": True, "enabled": bool(settings.CONVERSATION_REHYDRATE), **result}


@router.delete("/{thread_id}")
async def delete_conversation(
    thread_id: str,
    drop_context: bool = Query(
        True,
        description="同时删掉 Checkpointer 里的该会话状态（默认删，否则模型还『记得』你删掉的对话）",
    ),
) -> Dict[str, Any]:
    """
    删除一个会话。

    ★ 默认连 **Checkpointer 里的上下文**一起删

    只删文件是半吊子：用户删掉的是「这段对话」，而模型那边仍然带着它的全部上下文，
    下一次在同一个 thread 上说话就会被上一段对话影响 —— 删了却没删干净。
    所以默认调 `adelete_thread()`。Redis 不支持或调用失败时只告警：
    历史已经删了，这件事本身就是用户要的结果。
    """
    if not is_valid_thread_id(thread_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    deleted = conversation_store.delete_conversation(thread_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    context_deleted: Optional[bool] = None
    if drop_context:
        context_deleted = bool(await _drop_thread_contexts([thread_id]))

    # 会话都没了，针对它的预授权不该继续有效
    _revoke_grants(thread_id)

    # ---------- 联动删掉该会话的后台子任务（见 ADR 0019）----------
    # 会话没了而任务还留着，用户就会看到一批「查不到来源」的任务
    # 在界面上挂着 —— 悬空引用比没有更让人困惑。
    # 子任务在 Checkpointer 里还有 `subtask_<task_id>` 状态，一并丢掉，
    # 否则 Redis 里会留下永远查不到、也永远没人删的上下文。
    # 失败只告警：会话已经删了（那是用户要的结果），任务残留只是不好看。
    tasks_deleted: Optional[int] = None
    try:
        tasks_deleted = await _delete_thread_tasks(thread_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("删除会话的后台任务失败（会话已删除）: %s", e)

    return {
        "ok": True,
        "deleted": thread_id,
        "context_deleted": context_deleted,
        "tasks_deleted": tasks_deleted,
    }


@router.delete("")
async def clear_conversations(
    scope: str = Query(
        "all",
        pattern="^(all|main|assistant)$",
        description="all=全部（默认，含内部线程），main=只清普通对话，assistant=只清助理对话",
    ),
) -> Dict[str, Any]:
    """
    清空会话历史（不可撤销；不会动长期记忆与审批留痕）。

    ★ 后台子任务也一起清（见 ADR 0019）
    理由与单个会话删除同款：会话全没了而任务还留着，
    界面上会出现一批「查不到来源」的任务。
    ⚠️ **正在跑的子任务不会被强杀**（`TaskStore.clear` 默认 `keep_active=True`）：
    删掉它的 jsonl 而它还在往里写，会产生一个「有事件、索引里没有」的孤儿文件，
    下次索引重建时它会凭空冒出来；而它已经产生的副作用（写了文件、发了邮件）
    也无处可查。正在跑的那些跑完落成终态后，会被下一次清理带走。

    ★ scope=main / assistant 是「按命名空间清」
    侧栏「清空全部」只该清用户眼前这个列表 —— 站在普通对话里清空，
    不该把助理命名空间的会话一起带走（反之亦然）。

    ★ 所有 scope 都删 Checkpointer 上下文
    文件是界面，上下文才是模型记忆；只删文件等于「删了却还记得」。
    助理是固定单例线程，文件可能早被清空而上下文仍有内容，
    因此 scope=all / assistant 会显式连 `assistant_main` 一起丢弃。
    子任务的 `subtask_<task_id>` 上下文也随任务删除一并清理。
    """
    from app.agents.subgraph import is_subtask_thread_id
    from app.conversations import is_assistant_thread_id
    from app.workspace import is_employee_thread_id

    if scope == "all":
        cleared_ids = conversation_store.clear()
    elif scope == "assistant":
        cleared_ids = conversation_store.clear(thread_filter=is_assistant_thread_id)
    else:
        def keep(tid: str) -> bool:
            return (
                not is_subtask_thread_id(tid)
                and not is_employee_thread_id(tid)
                and not is_assistant_thread_id(tid)
            )

        cleared_ids = conversation_store.clear(thread_filter=keep)

    # ---------- 模型上下文：按「应当忘掉的线程」删，而不是只按删掉的文件 ----------
    # 助理是固定单例线程（assistant_main）：轮次文件可能早已删光，
    # Checkpointer 里却还留着模型记忆 —— 只遍历 `cleared_ids` 会漏掉它，
    # 「清空助理」之后下一句它仍然记得。所以把单例线程显式补进来。
    # 同时，这修掉了 scope=all 此前根本不删上下文的问题（清空不等于失忆）。
    context_ids = list(cleared_ids)
    if scope in ("all", "assistant"):
        from app.conversations.store import ASSISTANT_MAIN_THREAD_ID

        context_ids.append(ASSISTANT_MAIN_THREAD_ID)
    context_ids = list(dict.fromkeys(context_ids))
    await _drop_thread_contexts(context_ids)

    for tid in context_ids:
        _revoke_grants(tid)

    # ---------- 后台子任务：连同它们的 `subtask_<task_id>` 上下文一起删 ----------
    tasks_cleared: Optional[int] = None
    try:
        from app.tasks.store import task_store

        if scope == "all":
            task_ids = task_store.clear()
        else:
            task_ids = []
            for tid in cleared_ids:
                task_ids.extend(task_store.delete_by_thread(tid))
        tasks_cleared = len(task_ids)
        await _drop_task_contexts(task_ids)
    except Exception as e:  # noqa: BLE001
        logger.warning("清空后台任务失败（会话已清空）: %s", e)

    return {"ok": True, "cleared": len(cleared_ids), "tasks_cleared": tasks_cleared}
