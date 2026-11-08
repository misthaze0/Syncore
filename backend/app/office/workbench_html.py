"""Restricted editor HTML and portable document import/export."""
from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(item if isinstance(item, str) else item.text for item in self.children)


class EditorHTML(HTMLParser):
    allowed = {"p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i", "u", "s", "del", "mark", "ul", "ol", "li", "blockquote", "pre", "code", "br", "hr", "table", "thead", "tbody", "tr", "th", "td", "a"}
    void = {"br", "hr"}
    blocked = {"script", "style", "iframe", "object", "svg", "math", "template"}

    def __init__(self, content: str):
        super().__init__(convert_charrefs=True)
        self.root = Element("root")
        self.stack = [self.root]
        self.skip = 0
        self.feed(content)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in self.blocked:
            self.skip += 1
        if self.skip or tag not in self.allowed:
            return
        clean = {}
        for key, value in attrs:
            value = value or ""
            if key == "href" and tag == "a" and re.match(r"^(https?://|mailto:|#)", value, re.I):
                clean[key] = value
                clean["rel"] = "noopener noreferrer"
            elif key == "style":
                match = re.search(r"(?:^|;)\s*text-align:\s*(left|right|center|justify)\s*(?:;|$)", value)
                if match:
                    clean[key] = f"text-align: {match[1]}"
            elif key in {"colspan", "rowspan", "start"} and value.isdigit() and 0 < int(value) < 1000:
                clean[key] = value
            elif key == "colwidth" and tag in {"td", "th"} and re.fullmatch(r"\d{1,4}(,\d{1,4}){0,29}", value):
                clean[key] = value
            elif key == "data-type" and value in {"taskList", "taskItem"}:
                clean[key] = value
            elif key == "data-checked" and value in {"true", "false"}:
                clean[key] = value
        node = Element(tag, clean)
        self.stack[-1].children.append(node)
        if tag not in self.void:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.blocked:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.skip:
            self.stack[-1].children.append(data)


def render(node) -> str:
    if isinstance(node, str):
        return html.escape(node)
    inside = "".join(render(item) for item in node.children)
    if node.tag == "root":
        return inside
    attrs = "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in node.attrs.items())
    return f"<{node.tag}{attrs}>" + ("" if node.tag in EditorHTML.void else f"{inside}</{node.tag}>")


def clean_html(content: str) -> str:
    return render(EditorHTML(content).root) or "<p></p>"


def plain_text(content: str) -> str:
    def visit(node):
        if isinstance(node, str):
            return node
        result = "".join(visit(child) for child in node.children)
        if node.tag in {"br", "hr", "p", "div", "h1", "h2", "h3", "h4", "li", "tr", "blockquote", "pre"}:
            result += "\n"
        if node.tag in {"td", "th"}:
            result += "\t"
        return result
    return visit(EditorHTML(content).root).strip()


def to_markdown(content: str) -> str:
    def visit(node):
        if isinstance(node, str):
            return node
        inside = "".join(visit(child) for child in node.children)
        tag = node.tag
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return f"\n{'#' * int(tag[1])} {inside.strip()}\n\n"
        if tag in {"strong", "b", "em", "i", "s", "del", "code"}:
            marker = {"strong": "**", "b": "**", "em": "*", "i": "*", "s": "~~", "del": "~~", "code": "`"}[tag]
            return f"{marker}{inside}{marker}"
        if tag == "a" and node.attrs.get("href"):
            return f"[{inside}]({node.attrs['href']})"
        if tag == "li":
            prefix = "- "
            if node.attrs.get("data-type") == "taskItem":
                prefix += "[x] " if node.attrs.get("data-checked") == "true" else "[ ] "
            return f"{prefix}{inside.strip()}\n"
        if tag == "ol":
            items = [child for child in node.children if isinstance(child, Element) and child.tag == "li"]
            start = int(node.attrs.get("start", "1"))
            return "\n" + "".join(re.sub(r"^- ", f"{index}. ", visit(child), count=1) for index, child in enumerate(items, start)) + "\n"
        if tag == "blockquote":
            return "\n" + "\n".join("> " + line for line in inside.strip().splitlines()) + "\n\n"
        if tag == "pre":
            return f"\n```\n{node.text}\n```\n\n"
        if tag == "hr":
            return "\n---\n\n"
        if tag == "br":
            return "\n"
        if tag == "table":
            rows = []
            def collect(item):
                if isinstance(item, str):
                    return
                if item.tag == "tr":
                    rows.append([visit(cell).strip().replace("|", "\\|").replace("\n", " ") for cell in item.children if isinstance(cell, Element) and cell.tag in {"th", "td"}])
                else:
                    for child in item.children:
                        collect(child)
            collect(node)
            if not rows:
                return ""
            lines = ["| " + " | ".join(row) + " |" for row in rows]
            lines.insert(1, "| " + " | ".join("---" for _ in rows[0]) + " |")
            return "\n" + "\n".join(lines) + "\n\n"
        return inside + ("\n\n" if tag in {"p", "div", "ul", "ol"} else "")
    return re.sub(r"\n{3,}", "\n\n", visit(EditorHTML(content).root)).strip() + "\n"


def export_docx(title: str, content: str) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
    from docx.shared import Pt, RGBColor

    doc = Document()
    doc.styles["Normal"].font.name = "等线"
    doc.styles["Normal"].font.size = Pt(11)
    doc.styles["Normal"].paragraph_format.space_after = Pt(8)
    doc.add_heading(title, 0)

    def inline(parent, node, marks=()):
        if isinstance(node, str):
            run = parent.add_run(node)
            run.bold = any(m in marks for m in {"strong", "b"})
            run.italic = any(m in marks for m in {"em", "i"})
            run.underline = "u" in marks
            run.font.strike = any(m in marks for m in {"s", "del"})
            if "mark" in marks:
                run.font.highlight_color = WD_COLOR_INDEX.YELLOW
            if "code" in marks:
                run.font.name = "Consolas"
            return
        if node.tag == "br":
            parent.add_run().add_break()
        for child in node.children:
            inline(parent, child, (*marks, node.tag))

    def blocks(container, node, list_style=None):
        if isinstance(node, str):
            if node.strip():
                container.add_paragraph(node)
            return
        if node.tag == "table":
            rows = []
            def collect(item):
                if not isinstance(item, Element):
                    return
                if item.tag == "tr":
                    rows.append([cell for cell in item.children if isinstance(cell, Element) and cell.tag in {"td", "th"}])
                else:
                    for child in item.children:
                        collect(child)
            collect(node)
            if rows:
                table = container.add_table(rows=len(rows), cols=max(map(len, rows)))
                table.style = "Light Shading Accent 1"
                for r, row in enumerate(rows):
                    for c, cell in enumerate(row):
                        inline(table.cell(r, c).paragraphs[0], cell)
            return
        if node.tag in {"ul", "ol"}:
            for child in node.children:
                blocks(container, child, "List Number" if node.tag == "ol" else "List Bullet")
            return
        if node.tag in {"p", "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}:
            style = list_style if node.tag == "li" else None
            if re.fullmatch(r"h[1-6]", node.tag):
                paragraph = container.add_heading(level=int(node.tag[1]))
            else:
                paragraph = container.add_paragraph(style=style)
            align = node.attrs.get("style", "").split(":")[-1].strip()
            paragraph.alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}.get(align, WD_ALIGN_PARAGRAPH.LEFT)
            inline(paragraph, node)
            return
        for child in node.children:
            blocks(container, child, list_style)

    for child in EditorHTML(content).root.children:
        blocks(doc, child)
    doc.core_properties.title = title
    for name in ("Title", "Heading 1", "Heading 2", "Heading 3"):
        doc.styles[name].font.color.rgb = RGBColor.from_string("8B1E3F")
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def import_file(filename: str, data: bytes) -> tuple[str, str, list[str]]:
    """Return editor HTML, kind, and explicit fidelity notices."""
    from pathlib import Path

    suffix = Path(filename).suffix.lower()
    if suffix in {".docx", ".xlsx", ".pptx"}:
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as package:
            if sum(item.file_size for item in package.infolist()) > 30 * 1024 * 1024:
                raise ValueError("文档解压后超过 30 MB，请拆分后导入")
    if suffix in {".md", ".markdown"}:
        import markdown

        return clean_html(markdown.markdown(data.decode("utf-8-sig"), extensions=["tables", "fenced_code"])), "document", []
    if suffix in {".txt", ".html", ".htm"}:
        text = data.decode("utf-8-sig")
        content = text if suffix != ".txt" else "".join(f"<p>{html.escape(line)}</p>" for line in text.splitlines())
        return clean_html(content), "document", []
    if suffix == ".docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(io.BytesIO(data))
        chunks = []
        for item in doc.element.body:
            if item.tag.endswith("}p"):
                paragraph = Paragraph(item, doc)
                inside = ""
                for run in paragraph.runs:
                    text = html.escape(run.text).replace("\n", "<br>")
                    for active, tag in ((run.bold, "strong"), (run.italic, "em"), (run.underline, "u"), (run.font.strike, "s"), (run.font.highlight_color, "mark")):
                        if active:
                            text = f"<{tag}>{text}</{tag}>"
                    inside += text
                style = paragraph.style.name if paragraph.style else ""
                heading = re.search(r"Heading (\d)", style)
                tag = f"h{min(int(heading[1]), 6)}" if heading else "p"
                alignment = {1: "center", 2: "right", 3: "justify"}.get(paragraph.alignment)
                attrs = f' style="text-align: {alignment}"' if alignment else ""
                chunks.append(f"<{tag}{attrs}>{inside}</{tag}>")
            elif item.tag.endswith("}tbl"):
                table = Table(item, doc)
                chunks.append("<table>" + "".join("<tr>" + "".join(f"<td><p>{html.escape(cell.text)}</p></td>" for cell in row.cells) + "</tr>" for row in table.rows) + "</table>")
        return clean_html("".join(chunks)), "document", ["已导入文字、标题、基础格式和表格；图片、页眉页脚与复杂版式请保留原件核对。"]
    if suffix in {".csv", ".xlsx"}:
        chunks = []
        if suffix == ".csv":
            import csv

            rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
            sheets = [("数据表", rows)]
        else:
            import openpyxl

            workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            try:
                sheets = []
                for sheet in workbook:
                    if (sheet.max_row or 0) * (sheet.max_column or 0) > 20000:
                        raise ValueError("单个工作表超过 20,000 个单元格，请拆分后导入")
                    sheets.append((sheet.title, list(sheet.values)))
            finally:
                workbook.close()
        for title, rows in sheets:
            if sum(len(row) for row in rows) > 20000:
                raise ValueError("数据表超过 20,000 个单元格，请拆分后导入")
            chunks.append(f"<h2>{html.escape(title)}</h2><table>")
            for index, row in enumerate(rows):
                tag = "th" if index == 0 else "td"
                chunks.append("<tr>" + "".join(f"<{tag}><p>{html.escape(str(value if value is not None else ''))}</p></{tag}>" for value in row) + "</tr>")
            chunks.append("</table>")
        return clean_html("".join(chunks)), "spreadsheet", ["已作为可编辑表格导入；公式保留为文本，不在编辑器内计算。"]
    if suffix == ".pptx":
        from pptx import Presentation

        presentation = Presentation(io.BytesIO(data))
        chunks = []
        for index, slide in enumerate(presentation.slides, 1):
            chunks.append(f"<h1>第 {index} 页</h1>")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    chunks.extend(f"<p>{html.escape(paragraph.text)}</p>" for paragraph in shape.text_frame.paragraphs)
            chunks.append("<hr>")
        return clean_html("".join(chunks)), "presentation", ["已导入为可编辑的演示大纲；原演示文稿的图片、动画和页面版式请保留原件。"]
    raise ValueError("支持导入 Word、Excel、PowerPoint、Markdown、TXT、HTML 和 CSV 文件")
