"""保留可定位结构的 Office 文本，供附件、知识库和 Agent 共用。"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx", ".pdf"})


def word_text(paragraph) -> str:
    from docx.oxml.ns import qn
    if not paragraph._p.xpath("./w:ins | ./w:del"):
        return paragraph.text
    parts = []
    for node in paragraph._p:
        if node.tag in {qn("w:ins"), qn("w:del")}:
            label = "插入" if node.tag == qn("w:ins") else "删除"
            text = "".join(child.text or "" for child in node.iter() if child.tag in {qn("w:t"), qn("w:delText")})
            parts.append(f"【{label}：{text}】")
        else:
            parts.extend(child.text or "" for child in node.iter(qn("w:t")))
    return "".join(parts)


def word_paragraphs(doc) -> Iterator[tuple[str, object]]:
    """与编辑器共用稳定定位符；正文段落序号含空段落，从 1 开始。"""
    for i, paragraph in enumerate(doc.paragraphs, 1):
        yield f"p:{i}", paragraph
    for t, table in enumerate(doc.tables, 1):
        yield from _table_paragraphs(table, f"t:{t}")
    seen = set()
    for s, section in enumerate(doc.sections, 1):
        for kind in ("header", "footer", "first_page_header", "first_page_footer",
                     "even_page_header", "even_page_footer"):
            story = getattr(section, kind)
            if story.is_linked_to_previous or id(story._element) in seen:
                continue
            seen.add(id(story._element))
            for i, paragraph in enumerate(story.paragraphs, 1):
                yield f"section:{s}:{kind}:p:{i}", paragraph
            for t, table in enumerate(story.tables, 1):
                yield from _table_paragraphs(table, f"section:{s}:{kind}:t:{t}")


def _table_paragraphs(table, prefix):
    seen = set()
    for r, row in enumerate(table.rows, 1):
        for c, cell in enumerate(row.cells, 1):
            if cell._tc in seen:
                continue
            seen.add(cell._tc)
            base = f"{prefix}:r:{r}:c:{c}"
            for p, paragraph in enumerate(cell.paragraphs, 1):
                yield f"{base}:p:{p}", paragraph
            for t, nested in enumerate(cell.tables, 1):
                yield from _table_paragraphs(nested, f"{base}:t:{t}")


def slide_paragraphs(prs):
    def shapes(items, prefix):
        for i, shape in enumerate(items, 1):
            location = f"{prefix}:sh:{i}"
            if shape.has_text_frame:
                for p, paragraph in enumerate(shape.text_frame.paragraphs, 1):
                    yield f"{location}:p:{p}", paragraph
            if shape.has_table:
                for r, row in enumerate(shape.table.rows, 1):
                    for c, cell in enumerate(row.cells, 1):
                        if cell.is_spanned:
                            continue
                        for p, paragraph in enumerate(cell.text_frame.paragraphs, 1):
                            yield f"{location}:r:{r}:c:{c}:p:{p}", paragraph
            if shape.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
                yield from shapes(shape.shapes, location)
    for s, slide in enumerate(prs.slides, 1):
        yield from shapes(slide.shapes, f"s:{s}")


def native_lines(path: Path, suffix: str = "") -> Iterator[str]:
    suffix = suffix or path.suffix.lower()
    if suffix == ".docx":
        from docx import Document
        doc = Document(str(path))
        # 按正文 XML 顺序输出，避免表格被挪到全文末尾。
        paragraphs = {p._p: (key, p) for key, p in word_paragraphs(doc)}
        emitted = set()
        for element in doc.element.body.iter():
            if element in paragraphs:
                key, paragraph = paragraphs[element]
                emitted.add(key)
                yield f"[{key}] {word_text(paragraph)}"
        for key, paragraph in paragraphs.values():
            if key not in emitted:
                yield f"[{key}] {word_text(paragraph)}"
    elif suffix == ".xlsx":
        from openpyxl import load_workbook
        # 使用流以支持上传文件落盘时被改成没有扩展名的临时文件。
        with path.open("rb") as stream, path.open("rb") as cached_stream:
            wb = load_workbook(stream, read_only=True, data_only=False)
            cached = None
            try:
                cached = load_workbook(cached_stream, read_only=True, data_only=True)
                yield "[说明] 公式按原式读取；未执行公式重算，缓存可能过期，缺失缓存不能按零统计。"
                for ws in wb.worksheets:
                    yield f"[工作表: {ws.title}]"
                    for row, cached_row in zip(ws.iter_rows(), cached[ws.title].iter_rows()):
                        cells = []
                        for cell, cached_cell in zip(row, cached_row):
                            if cell.value is None:
                                continue
                            text = f"{cell.coordinate}={cell.value}"
                            if cell.data_type == "f":
                                text += f"（缓存={cached_cell.value}）" if cached_cell.value is not None else "（无缓存结果）"
                            cells.append(text)
                        if cells:
                            yield " | ".join(cells)
            finally:
                wb.close()
                if cached is not None:
                    cached.close()
    elif suffix == ".pptx":
        from pptx import Presentation
        prs = Presentation(str(path))
        for key, paragraph in slide_paragraphs(prs):
            yield f"[{key}] {paragraph.text}"
        for i, slide in enumerate(prs.slides, 1):
            for sh, shape in enumerate(slide.shapes, 1):
                if shape.has_chart:
                    chart = shape.chart
                    yield f"[s:{i}:sh:{sh}:图表] {chart.chart_type}"
                    for plot in chart.plots:
                        if hasattr(plot, "categories"):
                            yield "分类：" + " | ".join(str(category.label) for category in plot.categories)
                        for series in plot.series:
                            yield f"系列 {series.name}：" + " | ".join(str(value) for value in series.values)
            if slide.has_notes_slide:
                yield f"[s:{i}:备注] {slide.notes_slide.notes_text_frame.text}"
    elif suffix == ".pdf":
        from app.office.pdf import open_pdf
        with open_pdf(path) as doc:
            for i, page in enumerate(doc.pages, 1):
                # Filee 等引擎产出的 PDF 字体子集信息不全，layout 模式会静默
                # 提取为空 —— 先按 layout 提，空了退回默认模式，两者皆空才提示 OCR。
                text = page.extract_text(extraction_mode="layout") or page.extract_text() or ""
                text = text.strip()
                yield f"[第{i}页]\n{text}" if text else f"[第{i}页] 未提取到文字，可能需要 OCR。"
    else:
        raise ValueError(f"不支持的办公文档格式：{suffix}")


def extract_text(path: Path, suffix: str = "", engine: str = "native") -> str:
    suffix = suffix or path.suffix.lower()
    if engine == "native":
        return "\n".join(native_lines(path, suffix))
    if engine == "markitdown":
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise ValueError('请安装办公解析增强依赖：pip install -e ".[office-read]"') from exc
        # 不允许 URL / 插件 / 云端 document intelligence，上传原件可能没有原扩展名。
        with path.open("rb") as stream:
            return MarkItDown(enable_plugins=False).convert_stream(
                stream, file_extension=suffix,
            ).text_content
    if engine == "docling":
        if suffix != ".pdf":
            raise ValueError("Docling 增强入口仅用于 PDF；其他 Office 文档使用 native/markitdown")
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise ValueError('请安装复杂 PDF 解析依赖：pip install -e ".[office-layout]"；首次使用需准备模型') from exc
        return DocumentConverter().convert(str(path)).document.export_to_markdown()
    raise ValueError("engine 只能是 native、markitdown 或 docling")
