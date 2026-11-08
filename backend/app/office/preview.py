"""Bounded, read-only previews. Never execute formulas, macros or external links."""
from __future__ import annotations

import math
import re
import zipfile
from datetime import date, datetime, time
from itertools import islice
from pathlib import Path

MAX_OFFICE_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 80 * 1024 * 1024
MAX_ROWS = 500
MAX_COLUMNS = 60
MAX_SHEETS = 30
MAX_CELLS = 60000
MAX_TEXT_CHARS = 300000

INLINE_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".avif": "image/avif", ".ico": "image/x-icon", ".pdf": "application/pdf",
    ".mp4": "video/mp4", ".webm": "video/webm", ".ogv": "video/ogg",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4", ".flac": "audio/flac",
}


def _check_package(path: Path):
    if path.stat().st_size > MAX_OFFICE_BYTES:
        raise ValueError("文件超过 20 MB 预览上限，请下载查看完整内容")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > MAX_UNPACKED_BYTES:
            raise ValueError("文档解压后超过预览上限，请下载查看")


def _cell(value, number_format="General"):
    if value is None:
        return {"text": "", "value": None}
    if isinstance(value, (datetime, date, time)):
        return {"text": value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat(), "value": None}
    numeric = isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
    text = str(value)[:8000]
    if numeric:
        # Only common formats are approximated; the original is always available to download.
        fmt = number_format.split(";")[0]
        decimals = re.search(r"\.([0#]+)", fmt)
        precision = min(len(decimals[1]) if decimals else 0, 10)
        if "%" in fmt:
            text = f"{value * 100:.{precision}f}%"
        elif re.fullmatch(r"0{2,}", fmt):
            text = f"{int(value):0{len(fmt)}d}"
        elif re.fullmatch(r"[#0,]+(?:\.[#0]+)?", fmt):
            text = format(value, f",.{precision}f" if "," in fmt else f".{precision}f")
    return {"text": text, "value": value if numeric else None}


def workbook_preview(path: Path) -> dict:
    from openpyxl import load_workbook

    _check_package(path)
    book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        cached = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            sheets, remaining, chars_left = [], MAX_CELLS, MAX_TEXT_CHARS
            truncated = len(book.worksheets) > MAX_SHEETS
            for sheet in book.worksheets[:MAX_SHEETS]:
                columns = min(sheet.max_column or 1, MAX_COLUMNS)
                row_limit = min(sheet.max_row or 1, MAX_ROWS, remaining // columns)
                rows, formulas = [], 0
                values = cached[sheet.title].iter_rows(max_row=max(1, row_limit), max_col=columns)
                for raw, cache in zip(sheet.iter_rows(max_row=max(1, row_limit), max_col=columns), values):
                    if len(rows) >= row_limit or chars_left <= 0:
                        break
                    cells = []
                    for cell, cached_cell in zip(raw, cache):
                        formula = cell.data_type == "f"
                        value = cached_cell.value if formula else cell.value
                        item = _cell(value, cell.number_format)
                        if formula:
                            item["formula"] = str(cell.value)[:8000]
                            formulas += 1
                            if value is None:
                                item["text"] = "未计算"
                        if len(item["text"]) > chars_left:
                            item["text"] = item["text"][:max(0, chars_left)]
                        chars_left -= len(item["text"]) + len(item.get("formula", ""))
                        cells.append(item)
                    rows.append(cells)
                remaining -= len(rows) * columns
                limited = (sheet.max_row or 1) > len(rows) or (sheet.max_column or 1) > columns or chars_left <= 0
                sheets.append({"name": sheet.title, "rows": rows, "total_rows": sheet.max_row or 0,
                               "total_columns": sheet.max_column or 0, "truncated": limited, "formula_count": formulas})
                truncated |= limited
            return {"kind": "spreadsheet", "sheets": sheets, "truncated": truncated,
                    "notice": "只读预览；公式显示文件保存的缓存值，未缓存时标记未计算。图表视图根据所选数值列生成，原有嵌入图表和复杂格式请下载查看。"}
        finally:
            cached.close()
    finally:
        book.close()


def office_preview(path: Path) -> dict:
    _check_package(path)
    blocks, size, truncated = [], 0, False

    def append(block):
        nonlocal size, truncated
        length = len(str(block))
        if len(blocks) >= 2000 or size + length > MAX_TEXT_CHARS:
            truncated = True
            return False
        blocks.append(block)
        size += length
        return True

    def table_block(table):
        nonlocal truncated
        rows = [[cell.text[:8000] for cell in islice(row.cells, MAX_COLUMNS)] for row in islice(table.rows, MAX_ROWS)]
        truncated |= len(table.rows) > MAX_ROWS or len(table.columns) > MAX_COLUMNS
        return {"kind": "table", "rows": rows}

    if path.suffix.lower() == ".docx":
        from docx import Document
        from docx.table import Table

        document = Document(str(path))
        for item in document.iter_inner_content():
            if isinstance(item, Table):
                block = table_block(item)
            else:
                if not item.text:
                    continue
                block = {"kind": "heading" if item.style.name.startswith(("Heading", "Title")) else "paragraph", "text": item.text}
            if not append(block):
                break
    else:
        from pptx import Presentation

        document = Presentation(str(path))
        for index, slide in enumerate(document.slides):
            if not append({"kind": "heading", "text": f"第 {index + 1} 页"}):
                break
            for shape in slide.shapes:
                if shape.has_table:
                    block = table_block(shape.table)
                elif shape.has_text_frame and shape.text:
                    block = {"kind": "paragraph", "text": shape.text}
                else:
                    continue
                if not append(block):
                    break
            if truncated:
                break
    text = "\n".join(block.get("text", "") or "\n".join(" | ".join(row) for row in block["rows"]) for block in blocks)
    return {"kind": "document", "text": text, "blocks": blocks, "truncated": truncated,
            "notice": "内容预览 · 显示段落与表格。完整排版、图片和动画请下载查看。"}


def archive_preview(path: Path) -> dict:
    if path.stat().st_size > MAX_OFFICE_BYTES:
        raise ValueError("压缩包超过 20 MB 预览上限，请下载查看目录")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        return {"kind": "archive", "entries": [{"name": e.filename[:1000], "size": e.file_size}
                for e in entries[:200]], "total": len(entries), "truncated": len(entries) > 200}
