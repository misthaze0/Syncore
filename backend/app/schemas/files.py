"""
文件上传相关数据模型
"""

from pydantic import BaseModel, Field


class FileUploadResponse(BaseModel):
    """文件上传响应"""
    filename: str
    file_size: int  # bytes
    chunks_indexed: int  # 入库的 chunk 数量
    success: bool
    message: str = ""
    warnings: list[str] = Field(default_factory=list)
    unchanged: bool = False
    keyword_indexed: bool | None = None


class AttachmentResponse(BaseModel):
    """
    对话附件的元数据（`POST /api/files/attach`）。

    **不含正文** —— 正文留在服务端等本轮 chat 请求来取，
    回传它只会白白消耗带宽（一个 24k 字符的附件 ≈ 70KB JSON）。
    """

    attachment_id: str
    filename: str
    file_size: int  # bytes
    chars: int  # 解析出的文本字符数
    truncated: bool = False  # 超过单附件上限被截断
    original_available: bool = True  # 原件仍在（可下载），过期后为 false
    success: bool
    message: str = ""
