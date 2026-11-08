"""
文件解析器 - 支持 PDF / Word / PowerPoint / Markdown / Excel / TXT
将上传文件转为纯文本
"""

import asyncio
from pathlib import Path

from app.office.reader import OFFICE_SUFFIXES, extract_text

# 可入库文件的准入白名单（**单一来源**）。
#
# ⚠️ 它与 `parse_file()` 的分支不是同一件事：
#   - 这份清单是「准入门槛」——增量索引扫描目录时用它过滤，
#     免得把一个 .zip / .png 也丢进解析器
#   - `parse_file()` 对未知后缀有「当文本读」的兜底，是「尽力解析」
# 两者由同一处维护，避免「扫描放行了但解析不了」或反过来的错配。
SUPPORTED_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".csv", ".log",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".py", ".js", ".ts", ".json", ".yaml", ".yml",
})


async def parse_file(file_path: str, filename: str = "") -> str:
    """
    解析文件，返回纯文本

    Args:
        file_path: 文件路径
        filename: 原始文件名（用于判断类型）

    Returns:
        解析后的文本内容
    """
    path = Path(file_path)
    # 上传的文件在落盘时可能被改名（例如存成无扩展名的临时名），
    # 所以优先用 filename 的扩展名判断类型，取不到再退回真实路径的扩展名。
    suffix = Path(filename).suffix.lower() or path.suffix.lower()

    if suffix in OFFICE_SUFFIXES:
        # ZIP/XML/PDF 的同步解析不占用 API 事件循环。附件、知识库与办公工具共用结构。
        return await asyncio.to_thread(extract_text, path, suffix)

    return await asyncio.to_thread(_parse_text, path)


def _parse_pdf(path: Path) -> str:
    return extract_text(path, ".pdf")


def _parse_docx(path: Path) -> str:
    return extract_text(path, ".docx")


def _parse_text(path: Path) -> str:
    """读取纯文本文件"""
    # 尝试不同编码
    for encoding in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _parse_excel(path: Path) -> str:
    return extract_text(path, ".xlsx")
