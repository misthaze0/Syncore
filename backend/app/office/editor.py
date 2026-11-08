"""定位编辑与模板填充。所有操作在内存完成后另存，失败不留下半成品。"""
from __future__ import annotations

import os
import re
import tempfile
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.office.reader import slide_paragraphs, word_paragraphs

Scalar = str | int | float | bool | None


class EditOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["replace_text", "replace_text_tracked", "set_cell", "append_row", "add_chart", "set_header", "set_footer", "add_toc"]
    target: str = Field(default="", description="读取工具返回的定位符；Excel 为 A1 坐标；Word append_row 为 t:1")
    old_text: str = Field(default="", description="replace_text 必填且须恰好匹配一次；set_cell 为已有单元格原值的字符串")
    text: str = ""
    sheet: str = Field(default="", description="Excel 工作表名，必须明确指定")
    value: Scalar = None
    values: list[Scalar] = Field(default_factory=list, max_length=1000)
    chart_type: Literal["line", "bar"] = "line"
    data_range: str = Field(default="", description="Excel 图表范围如 A1:C13，首列为分类、首行为系列名")
    author: str = Field(default="Syncore", min_length=1, max_length=100, description="Word 修订作者")


def open_document(path: Path):
    if path.suffix.lower() == ".docx":
        from docx import Document
        return Document(str(path))
    if path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook
        return load_workbook(str(path))
    if path.suffix.lower() == ".pptx":
        from pptx import Presentation
        return Presentation(str(path))
    raise ValueError("编辑与模板填充支持 docx / xlsx / pptx；旧格式请先转换")


def validate_file(path: Path) -> None:
    if not path.is_file() or not path.stat().st_size:
        raise ValueError("引擎未生成有效文件")
    if path.suffix.lower() == ".pdf":
        from app.office.pdf import validate_pdf
        validate_pdf(path)
    elif path.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
        doc = open_document(path)
        if hasattr(doc, "close"):
            doc.close()


def publish_file(staged: Path, target: Path, overwrite: bool = False) -> None:
    """新文件使用独占创建，已有文件只有显式 overwrite 时才替换。"""
    if overwrite:
        os.replace(staged, target)
    else:
        # hardlink 在同一文件系统内原子地发布；目标已存在时绝不覆盖。
        os.link(staged, target)
        staged.unlink()


def save_document(doc, target: Path, overwrite: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=target.suffix, prefix=".office-", dir=target.parent)
    os.close(fd)
    staged = Path(name)
    try:
        doc.save(str(staged))
        validate_file(staged)
        publish_file(staged, target, overwrite)
    finally:
        staged.unlink(missing_ok=True)
        if hasattr(doc, "close"):
            doc.close()


def replace_in_paragraph(paragraph, old: str, new: str) -> None:
    """跨 run 替换，未改动部分的 run 及其样式保持原样。"""
    if not old or paragraph.text.count(old) != 1:
        raise ValueError("原文必须在指定段落内恰好出现一次，请重新读取并缩小定位范围")
    start = paragraph.text.index(old)
    _replace_at(paragraph, start, start + len(old), new)


def _excel_sheet(doc, name):
    if not name or name not in doc.sheetnames:
        raise ValueError("请指定存在的 sheet 工作表名")
    return doc[name]


def _cell(ws, coordinate):
    if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", coordinate):
        raise ValueError("target 必须是 A1 形式的单个单元格坐标")
    from openpyxl.utils.cell import coordinate_to_tuple
    row, column = coordinate_to_tuple(coordinate)
    if row > 1048576 or column > 16384:
        raise ValueError("单元格坐标超出 Excel 范围")
    return ws[coordinate]


def apply_operations(doc, suffix: str, operations: list[EditOperation]) -> None:
    # 定位符按「读取时」的原文一次性解析成段落对象：同一批操作里 add_toc 会在文首
    # 插入段落，逐操作重解析会让后续定位符整体后移 —— 轻则报错让模型重读，
    # 重则静默改错段落（两个段落文本恰好相同时）。
    paragraphs = None
    if suffix in {".docx", ".pptx"}:
        paragraphs = dict(word_paragraphs(doc) if suffix == ".docx" else slide_paragraphs(doc))
    for op in operations:
        if op.action == "replace_text_tracked" and suffix == ".docx":
            if op.target not in paragraphs:
                raise ValueError("修订目标不存在，请先读取定位符")
            tracked_replace(paragraphs[op.target], op.old_text, op.text, op.author)
        elif op.action == "replace_text":
            if suffix == ".xlsx":
                cell = _cell(_excel_sheet(doc, op.sheet), op.target)
                old = "" if cell.value is None else str(cell.value)
                if not op.old_text or old.count(op.old_text) != 1:
                    raise ValueError("原文必须在指定单元格内恰好出现一次")
                cell.value = old.replace(op.old_text, op.text, 1)
            else:
                if op.target not in paragraphs:
                    raise ValueError(f"未找到段落 {op.target}，请先 read_document 获取定位符")
                replace_in_paragraph(paragraphs[op.target], op.old_text, op.text)
        elif op.action == "set_cell" and suffix == ".xlsx":
            cell = _cell(_excel_sheet(doc, op.sheet), op.target)
            old = "" if cell.value is None else str(cell.value)
            if old != op.old_text:
                raise ValueError(f"{op.sheet}!{op.target} 原值已变化，请重新读取")
            cell.value = op.value
        elif op.action == "append_row" and suffix == ".xlsx":
            if not op.values:
                raise ValueError("新增行 values 不能为空")
            ws = _excel_sheet(doc, op.sheet)
            if ws.max_row >= 1048576:
                raise ValueError("新增行超出 Excel 行数上限")
            ws.append(op.values)
        elif op.action == "append_row" and suffix == ".docx":
            match = re.fullmatch(r"t:([1-9][0-9]*)", op.target)
            if not match or int(match[1]) > len(doc.tables):
                raise ValueError("Word 新增表格行 target 必须是有效的 t:1 形式")
            table = doc.tables[int(match[1]) - 1]
            if len(op.values) != len(table.columns):
                raise ValueError("新增行 values 数量必须等于表格列数")
            for cell, value in zip(table.add_row().cells, op.values):
                cell.text = "" if value is None else str(value)
        elif op.action == "add_chart" and suffix == ".xlsx":
            from openpyxl.chart import BarChart, LineChart, Reference
            from openpyxl.utils.cell import range_boundaries
            ws = _excel_sheet(doc, op.sheet)
            if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}:[A-Z]{1,3}[1-9][0-9]{0,6}", op.data_range):
                raise ValueError("图表 data_range 必须是 A1:C13 形式")
            left, top, right, bottom = range_boundaries(op.data_range)
            if not (left < right <= min(ws.max_column, 16384) and top < bottom <= min(ws.max_row, 1048576)):
                raise ValueError("图表范围需包含分类列、系列标题及至少一行已有数据")
            _cell(ws, op.target or "E2")
            chart = LineChart() if op.chart_type == "line" else BarChart()
            chart.title = op.text or "数据分析"
            chart.add_data(Reference(ws, min_col=left + 1, max_col=right, min_row=top, max_row=bottom), titles_from_data=True)
            chart.set_categories(Reference(ws, min_col=left, min_row=top + 1, max_row=bottom))
            ws.add_chart(chart, op.target or "E2")
        elif op.action in {"set_header", "set_footer"} and suffix == ".docx":
            section = doc.sections[0]
            story = section.header if op.action == "set_header" else section.footer
            story.paragraphs[0].text = op.text
        elif op.action == "add_toc" and suffix == ".docx":
            from docx.oxml import OxmlElement
            from docx.oxml.ns import qn
            paragraph = doc.add_paragraph()
            field = OxmlElement("w:fldSimple")
            field.set(qn("w:instr"), 'TOC \\o "1-3" \\h \\z \\u')
            run = OxmlElement("w:r")
            text = OxmlElement("w:t")
            text.text = "打开 Word 后更新目录域"
            run.append(text)
            field.append(run)
            paragraph._p.append(field)
            doc.element.body.remove(paragraph._p)
            doc.element.body.insert(0, paragraph._p)
        else:
            raise ValueError(f"{suffix} 不支持操作 {op.action}")


def edit_document(source: Path, target: Path, operations: list[EditOperation], overwrite=False):
    doc = open_document(source)
    try:
        apply_operations(doc, source.suffix.lower(), operations)
        save_document(doc, target, overwrite)
    finally:
        if hasattr(doc, "close"):
            doc.close()


PLACEHOLDER = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")


def merge_document(source: Path, target: Path, values: dict[str, Scalar], overwrite=False):
    doc = open_document(source)
    try:
        suffix = source.suffix.lower()
        if suffix == ".xlsx":
            containers = [c for ws in doc.worksheets for row in ws for c in row if isinstance(c.value, str)]
            texts = [c.value for c in containers]
        else:
            containers = [p for _, p in (word_paragraphs(doc) if suffix == ".docx" else slide_paragraphs(doc))]
            texts = [p.text for p in containers]
        keys = {m[1] for text in texts for m in PLACEHOLDER.finditer(text)}
        if not keys:
            raise ValueError("模板中未找到 {{占位符}}")
        missing = keys - values.keys()
        if missing:
            raise ValueError("模板缺少数据：" + "、".join(sorted(missing)))
        # 只解析原模板一次，用户数据里的 {{...}} 不会被再次求值。
        for container, text in zip(containers, texts):
            matches = list(PLACEHOLDER.finditer(text))
            if not matches:
                continue
            if suffix == ".xlsx":
                full = PLACEHOLDER.fullmatch(text)
                container.value = values[full[1]] if full else PLACEHOLDER.sub(
                    lambda m: "" if values[m[1]] is None else str(values[m[1]]), text,
                )
                # 模板数据为文字时不得意外变成 Excel 公式。
                if isinstance(container.value, str):
                    container.data_type = "s"
            else:
                # 逆序按偏移替换，支持同段重复占位符且保持 run 样式。
                for match in reversed(matches):
                    _replace_at(container, match.start(), match.end(), "" if values[match[1]] is None else str(values[match[1]]))
        save_document(doc, target, overwrite)
    finally:
        if hasattr(doc, "close"):
            doc.close()


def _replace_at(paragraph, start, end, value):
    runs = list(paragraph.runs)
    if "".join(r.text for r in runs) != paragraph.text:
        raise ValueError("段落含超链接或特殊文本结构，基础编辑器无法安全修改")
    position = 0
    inserted = False
    for run in runs:
        text = run.text
        stop = position + len(text)
        if stop > start and position < end:
            from docx.oxml.ns import qn
            # 不让 run.text 的赋值顺带删掉同一 run 内的图片、域或对象。
            allowed = {qn("w:rPr"), qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr"), qn("a:rPr"), qn("a:t")}
            if any(child.tag not in allowed for child in run._r):
                raise ValueError("目标文本含图片或特殊对象，无法安全修改")
            run.text = text[:max(0, start - position)] + (value if not inserted else "") + text[max(0, end - position):]
            inserted = True
        position = stop


def tracked_replace(paragraph, old: str, new: str, author: str):
    """自主实现 OOXML 插入/删除修订。原文保存在 delText，可在 Word 接受或拒绝。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    if not old or paragraph.text.count(old) != 1:
        raise ValueError("原文必须在指定段落内恰好出现一次")
    if any(child.tag not in {qn("w:pPr"), qn("w:r")} for child in paragraph._p):
        raise ValueError("修订替换暂不支持含已有修订、超链接或特殊域的段落")
    runs = list(paragraph.runs)
    if any(child.tag not in {qn("w:rPr"), qn("w:t")} for run in runs for child in run._r):
        raise ValueError("修订替换仅支持普通文本 run，请分离图片、域或换行对象")
    start = paragraph.text.index(old)
    end = start + len(old)
    root = paragraph._p.getroottree().getroot()
    ids = root.xpath(".//w:ins/@w:id | .//w:del/@w:id")
    revision_id = max((int(value) for value in ids if value.isdecimal()), default=0) + 1
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def revision(kind, number):
        element = OxmlElement(f"w:{kind}")
        for key, value in {"id": str(number), "author": author, "date": stamp}.items():
            element.set(qn(f"w:{key}"), value)
        return element

    def piece(run, text, deleted=False):
        result = OxmlElement("w:r")
        if run._r.rPr is not None:
            result.append(deepcopy(run._r.rPr))
        node = OxmlElement("w:delText" if deleted else "w:t")
        node.set(qn("xml:space"), "preserve")
        node.text = text
        result.append(node)
        return result

    deletion = revision("del", revision_id)
    insertion = revision("ins", revision_id + 1)
    output = []
    position = 0
    inserted = False
    for run in runs:
        text = run.text
        stop = position + len(text)
        if stop <= start or position >= end:
            output.append(deepcopy(run._r))
        else:
            if position < start:
                output.append(piece(run, text[:start - position]))
            removed = text[max(0, start - position):min(len(text), end - position)]
            deletion.append(piece(run, removed, deleted=True))
            if not inserted:
                if new:
                    insertion.append(piece(run, new))
                output.extend([deletion, insertion])
                inserted = True
            if stop > end:
                output.append(piece(run, text[end - position:]))
        position = stop
    # 输出节点引用同一个 deletion，跨 run 的所有原文最终都保存在其中。
    for run in runs:
        paragraph._p.remove(run._r)
    for element in output:
        if element.tag != qn("w:ins") or new:
            paragraph._p.append(element)


def batch_merge(source: Path, target: Path, records: list[dict[str, Scalar]], overwrite=False):
    """整批成功后交付一个 ZIP；任意记录缺字段时不发布不完整批次。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".office-merge-", dir=target.parent) as folder:
        root = Path(folder)
        archive = root / "documents.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for i, values in enumerate(records, 1):
                document = root / f"{i:04d}{source.suffix.lower()}"
                try:
                    merge_document(source, document, values)
                except Exception as exc:
                    raise ValueError(f"第 {i} 条模板数据生成失败：{exc}") from exc
                bundle.write(document, arcname=document.name)
        publish_file(archive, target, overwrite)
