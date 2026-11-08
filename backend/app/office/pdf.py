"""基础 PDF 读取/校验，统一使用 pypdf，不依赖渲染器。"""
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def open_pdf(path: Path):
    from pypdf import PdfReader

    # 自己持有流，解析失败也能关闭 Windows 文件句柄。
    with path.open("rb") as stream:
        document = PdfReader(stream, strict=True)
        if document.is_encrypted and not document.decrypt(""):
            raise ValueError("PDF 已加密，请先提供已解密的副本")
        yield document


def validate_pdf(path: Path):
    with open_pdf(path) as document:
        if not document.pages:
            raise ValueError("生成的 PDF 没有页面")
        for page in document.pages:
            # 触发页对象解析，而不仅验证文件头。
            _ = page.mediabox
