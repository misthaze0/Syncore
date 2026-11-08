"""
文件接口

两条路径，语义必须分开：
- `POST /files/upload` —— **永久入库**：解析 → 分块 → 向量化 → Milvus（设置 → 知识库）
- `POST /files/attach` —— **本轮临时上下文**：只解析文本，随消息注入提示词，
  不入任何向量库，24h 过期（聊天输入框的回形针 / 拖拽 / 粘贴）
"""

import logging
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.attachments import AttachmentError, load_original, save_attachment
from app.config import settings
from app.rag import incremental
from app.rag.parser import SUPPORTED_SUFFIXES
from app.schemas.files import AttachmentResponse, FileUploadResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["files"])


@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(file: UploadFile = File(...)):
    """
    上传文件 → 解析 → 分块 → 向量化 → 存入 Milvus + Elasticsearch → 登记清单

    支持: PDF, Word(.docx), PowerPoint(.pptx), Markdown(.md), Excel(.xlsx), TXT, CSV, 代码文件
    """
    # 客户端可控的只有 basename：`Path("上传") / "../../x.exe"` 会跳出上传目录，
    # 而这里拼出来的路径是紧接着 write_bytes 的，等于任意路径写。
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if (not filename or filename in {".", ".."} or filename.endswith((".", " "))
            or re.search(r'[\x00-\x1f<>:"|?*]', filename)
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", filename, re.I)
            or len(filename.encode("utf-8")) > 240):
        return FileUploadResponse(
            filename="",
            file_size=0,
            chunks_indexed=0,
            success=False,
            message="文件名无效，请更换文件名后重试",
        )
    if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(status_code=400, detail="不支持这个文件格式；请使用 PDF、DOCX、PPTX、XLSX 或文本文件")

    # Bound each read, and keep the existing original until indexing succeeds.
    upload_dir = settings.upload_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    file_size = 0
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=upload_dir, prefix=".knowledge-", suffix=Path(filename).suffix, delete=False) as staged:
            staged_path = Path(staged.name)
            while True:
                block = await file.read(min(1024 * 1024, max_bytes - file_size + 1))
                if not block:
                    break
                file_size += len(block)
                if file_size > max_bytes:
                    raise ValueError(f"文件大小超过限制 ({settings.MAX_FILE_SIZE_MB} MB)")
                staged.write(block)
        result = await incremental.ingest_upload(staged_path, filename)
    except Exception as e:
        logger.error(f"文件入库失败: {e}", exc_info=True)
        return FileUploadResponse(
            filename=filename,
            file_size=file_size,
            chunks_indexed=0,
            success=False,
            message=f"入库失败: {str(e)}",
        )
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        await file.close()

    return FileUploadResponse(
        filename=filename,
        file_size=file_size,
        chunks_indexed=result["chunks"],
        success=True,
        message=("内容未变化，已复用索引" if result["unchanged"] else f"成功入库 {result['chunks']} 个文本块")
                + (f"；{'；'.join(result['warnings'])}" if result["warnings"] else ""),
        warnings=result["warnings"],
        unchanged=result["unchanged"],
        keyword_indexed=result["keyword_indexed"],
    )


@router.post("/attach", response_model=AttachmentResponse)
async def attach_file(file: UploadFile = File(...)):
    """
    上传一个**对话附件**：解析出文本 → 存临时 JSON（24h 过期）→ 返回 id。

    它与 `/upload` 的区别是语义而不是实现细节：
    附件回答「拿这份文件问一句话」，入库回答「让助手永远知道这份文档」。
    附件**不入 Milvus/ES** —— 一次性的临时文件不该永久污染知识库，
    而且跳过分块与向量化后，「拖进来就能问」才等得起。

    不支持的类型 / 超限返回 400（带人话说明），解析失败同样 400 ——
    前端把它变成附件 chip 上的一条错误说明，而不是全局报错。
    """
    content = await file.read()
    try:
        meta = await save_attachment(file.filename or "", content)
    except AttachmentError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error("附件解析失败: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail="这个文件没能解析出文本内容") from e

    return AttachmentResponse(
        attachment_id=meta["attachment_id"],
        filename=meta["filename"],
        file_size=meta["size"],
        chars=meta["chars"],
        truncated=meta["truncated"],
        original_available=bool(meta.get("original_available", True)),
        success=True,
        message="已就绪" if meta["chars"] else "解析出的文本为空",
    )


@router.get("/attach/{attachment_id}")
async def download_attachment(attachment_id: str) -> FileResponse:
    """
    下载附件的**原件**（不是解析出的文本）。

    附件仍按 24h 过期；过期后与「不存在」统一 404 —— 不暴露 id 是否存在过。
    """
    found = load_original(attachment_id)
    if found is None:
        raise HTTPException(status_code=404, detail="附件已过期或不存在")
    path, meta = found
    return FileResponse(
        path=str(path),
        filename=str(meta.get("filename") or path.name),
        media_type=str(meta.get("mime") or "application/octet-stream"),
    )
