"""
对话附件 —— 「拖一个文件进来问一句话」的临时上下文

★ 与知识库上传（`/api/files/upload`）是两条路径，语义必须分开

用户拿一份文档问一句话时，第一反应是拖进输入框。这件事**不该**等于
「永久入库」：一是一次性的临时文件会把知识库污染成一堆垃圾，
二是入库要走 分块 → 向量化 → Milvus，用户等不起也不想等。

所以附件走的路是：解析出文本 → 存成 `{id}.json` → 随本轮消息注入提示词，
**不进 Milvus / ES**，24 小时后顺带清理。用户想要永久入库，入口在
「设置 → 知识库」——两个入口回答两种意图，互不替代。

★ 附件文本进 HumanMessage 而不是单独的状态字段

拼进本轮的 HumanMessage（`state.initial_state::compose_message`），
模型自然看到它，历史窗口内追问「刚才那份文件里 X 是多少」也能答上；
而 `user_input` 保持干净 —— RAG 检索不该拿附件全文当 query。
代价是附件文本会随 messages 进 Checkpointer（有单附件截断上限兜底），
换来的是「追问附件内容」不需要任何额外机制。
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

#: 附件 id 的合法形态（uuid4().hex）——它会成为文件名，必须挡住路径穿越
_AID_RE = re.compile(r"^[0-9a-f]{32}$")


class AttachmentError(ValueError):
    """附件不合法（类型 / 大小 / 解析失败）——API 层转成 400"""


def _meta_path(aid: str) -> Path:
    return settings.attachments_path / f"{aid}.json"


def _originals_dir() -> Path:
    p = settings.attachments_path / "originals"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _original_path(aid: str, suffix: str) -> Path:
    return _originals_dir() / f"{aid}{suffix}"


def purge_expired() -> int:
    """
    删除超过 TTL 的附件。

    刻意不做后台定时任务：附件是低频小文件，挂在「每次有人上传」这一
    必然发生的路径上顺带清理即可 —— 没人上传就没有过期文件需要关心。
    单个文件删除失败不中断（下一个 TTL 周期还会再试）。
    """
    ttl_seconds = settings.ATTACHMENT_TTL_HOURS * 3600
    now = time.time()
    removed = 0
    try:
        entries = list(settings.attachments_path.glob("*.json"))
    except OSError:
        return 0
    for path in entries:
        try:
            if now - path.stat().st_mtime > ttl_seconds:
                path.unlink()
                # 原件与文本同时过期：只删 JSON 会让原件变成永远没人回收的孤儿
                for blob in _originals_dir().glob(f"{path.stem}.*"):
                    try:
                        blob.unlink()
                    except OSError:
                        pass
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("已清理 %d 个过期附件", removed)
    return removed


async def save_attachment(filename: str, content: bytes) -> Dict[str, Any]:
    """
    解析并保存一个附件，返回元数据（**不含正文** —— 正文不回传，省带宽）。

    流程：校验类型与大小 → 以正确后缀落盘 → 复用 RAG 的解析器 →
    截断到单附件上限 → 写 `{id}.json` → 删掉临时解析文件。
    """
    purge_expired()

    safe_name = Path(filename or "").name.strip()
    if not safe_name:
        raise AttachmentError("文件名不能为空")

    # 类型白名单与知识库入库同源（app.rag.parser.SUPPORTED_SUFFIXES）——
    # 「能解析的类型」两边必须一致，否则会出现「附件能传但全是乱码」。
    from app.rag.parser import SUPPORTED_SUFFIXES, parse_file

    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise AttachmentError(
            "暂不支持这种文件类型（支持：md / txt / csv / log / pdf / docx / xlsx / pptx / 常见代码文件）"
        )

    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise AttachmentError(f"文件大小超过限制（{settings.MAX_FILE_SIZE_MB}MB）")

    aid = uuid.uuid4().hex
    # ★ 原件保留：解析失败才删，成功就留到 TTL 到期
    #   文本被截断到 24k 字符后，复杂表格/图片/版式已经不可还原；
    #   没有原件，「改一下这份 Word」就只剩重新生成一份近似文件。
    original = _original_path(aid, suffix)
    original.write_bytes(content)

    try:
        text = await parse_file(str(original), safe_name)
    except Exception:
        try:
            original.unlink()
        except OSError:
            pass
        raise

    text = (text or "").strip()
    max_chars = settings.ATTACHMENT_MAX_CHARS
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    meta = {
        "attachment_id": aid,
        "filename": safe_name,
        "size": len(content),
        "chars": len(text),
        "truncated": truncated,
        "created_at": time.time(),
        "suffix": suffix,
        "mime": mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
        "sha256": hashlib.sha256(content).hexdigest(),
        "original_available": True,
        "text": text,
    }
    _meta_path(aid).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("已保存附件 %s (%s, %d 字符%s)", safe_name, aid[:8], len(text),
                "，已截断" if truncated else "")
    return {k: v for k, v in meta.items() if k != "text"}


def load_attachments(ids: Optional[List[str]]) -> List[Dict[str, Any]]:
    """
    按 id 顺序加载附件（含正文），发给模型前用。

    容错策略：**缺的跳过并告警，不让整轮对话失败** ——
    附件是 24 小时临时物，过期/被清之后用户可能拿着旧界面重发消息；
    这时正确表现是「那份附件没了」并继续回答，而不是 500。
    前端会通过轮次记录里缺失的元数据自行说明（重放只见文件名）。
    """
    if not ids:
        return []
    loaded: List[Dict[str, Any]] = []
    for raw_id in ids:
        aid = str(raw_id or "").strip().lower()
        if not _AID_RE.match(aid):
            logger.warning("忽略非法附件 id: %r", raw_id)
            continue
        path = _meta_path(aid)
        if not path.is_file():
            logger.warning("附件不存在（可能已过期）: %s", aid[:8])
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("附件读取失败 %s: %s", aid[:8], e)
            continue
        original = load_original(aid)
        loaded.append(
            {
                "attachment_id": aid,
                "filename": str(meta.get("filename", "")),
                "text": str(meta.get("text", "")),
                "truncated": bool(meta.get("truncated", False)),
                # 只由已校验的附件 id/suffix 推导，不能信任元数据里任意路径。
                "original_path": str(original[0].resolve()) if original else "",
            }
        )
    return loaded


def load_original(aid: str) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """
    取一个附件的**原件**（下载用）。

    元数据里的 suffix 是从文件名派生的，理论上可被人手工改 JSON 篡改；
    这里只接受「一个点 + 字母数字」的形态，其余一律当作没有原件，
    免得 aid 之外又多出一条拼路径的输入。
    """
    clean = str(aid or "").strip().lower()
    if not _AID_RE.match(clean):
        return None
    path = _meta_path(clean)
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    suffix = str(meta.get("suffix") or "")
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        return None
    original = _original_path(clean, suffix)
    if not original.is_file():
        return None
    return original, meta


def compose_message(user_input: str, attachments: Optional[List[Dict[str, Any]]]) -> str:
    """
    把附件正文拼进用户消息。

    用 `---` 分隔而不是 markdown 引用块：这是给**模型**看的结构标记，
    越朴素越不会被当成正文的一部分复述给用户。
    """
    if not attachments:
        return user_input
    blocks: List[str] = []
    for a in attachments:
        header = f"[附件：{a.get('filename') or '未命名文件'}]"
        if a.get("truncated"):
            header += "（内容过长，已截断）"
        text = (a.get("text") or "").strip()
        body = text if text else "（这个文件没有解析出文本内容）"
        original_path = a.get("original_path")
        location = (
            "\n原件路径（只读；读取完整结构用 read_document，修改时用 edit_document 另存）："
            + json.dumps(str(original_path), ensure_ascii=False)
            if original_path else ""
        )
        blocks.append(f"{header}{location}\n{body}")
    return f"{user_input}\n\n---\n" + "\n\n".join(blocks) + "\n---"


def attachment_metas(attachments: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    """轮次留痕用的精简元数据（**绝不落盘正文** —— 会话文件不该膨胀几十 KB）"""
    if not attachments:
        return []
    return [
        {"id": str(a.get("attachment_id", "")), "filename": str(a.get("filename", ""))}
        for a in attachments
        if a.get("attachment_id")
    ]
