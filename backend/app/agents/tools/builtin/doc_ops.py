"""
内置工具 —— 交付物生成 (doc_ops)

对标市面桌面 Agent 的「直接给成品」：用户说「帮我做一份周报 / 一张对比表 / 一个汇报 PPT」，
拿到的应该是**能直接打开的 Office 文件**，而不是聊天框里的一段 Markdown。

支持格式（按扩展名分派）:
| 扩展名    | 生成方式                    | 依赖          |
|-----------|-----------------------------|---------------|
| .docx     | 标题 / 段落 / 列表 / 表格   | python-docx   |
| .xlsx     | Markdown 表格或 CSV → 工作表 | openpyxl      |
| .pptx     | `# 标题` 分页 + `- ` 项目符号 | python-pptx   |
| .md/.txt  | 原样写入（纯文本路径）      | —             |
| .csv      | 原样写入                    | —             |
| .html     | 原样写入                    | —             |

★ 三条设计取舍

1. **内容用 Markdown-lite，不引入新的 DSL**。模型天生会写 Markdown 表格与 `- ` 列表，
   要求它输出某种自定义 JSON 只会增加失败率；这里把最常见的几种结构认出来就够了。
2. **依赖懒加载 + 明确降级**。`python-pptx` 等库在函数内 import：没装的表现是
   「这一个工具报错并说清缺什么」，而不是整个后端起不来（不变量 14 的同一套取舍）。
3. **危险等级 moderate，与 `write` 一致**：它只往沙箱里写文件，
   覆盖同名文件的风险与 `write` 完全相同，没有必要单独提级成 danger。
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.agents.tools.context import ToolContext
from app.agents.tools.decorator import agent_tool

logger = logging.getLogger(__name__)

#: 支持的扩展名 → 人类可读的格式名（用于错误提示）
SUPPORTED_SUFFIXES: Dict[str, str] = {
    ".docx": "Word 文档",
    ".xlsx": "Excel 表格",
    ".pptx": "PowerPoint 演示文稿",
    ".md": "Markdown 文档",
    ".txt": "纯文本",
    ".csv": "CSV 表格",
    ".html": "网页",
}

#: Excel 单元格文本长度上限。超出明确拒绝，不静默丢弃原文。
MAX_CELL_CHARS = 32767


def _health() -> bool:
    """
    健康检查：沙箱可写 + 至少一个文档库可用。

    放在工具函数之前定义 —— `@agent_tool` 的 `health_handler` 是在
    装饰那一刻取值的，写到文件末尾会得到一个尚未定义的名字。
    """
    try:
        from app.core.sandbox import sandbox_root

        if not sandbox_root().is_dir():
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        import docx  # noqa: F401

        return True
    except ImportError:
        return False


# ---------- Markdown-lite 解析 ----------
Block = Tuple[str, Any]


def _strip_cell(cell: str) -> str:
    return cell.strip()


def _split_table_row(line: str) -> List[str]:
    """把 `| a | b |` 切成 ['a', 'b']（首尾竖线可有可无）"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [_strip_cell(c) for c in text.split("|")]


def _is_separator_row(cells: List[str]) -> bool:
    """Markdown 表格的分隔行（`|---|---|`）—— 不当作数据"""
    return bool(cells) and all(set(c) <= set("-: ") and c for c in cells)


def parse_blocks(content: str) -> List[Block]:
    """
    把 Markdown-lite 文本切成结构化块。

    块类型: `h1` / `h2` / `bullet` / `table` / `text`
    （`table` 的 payload 是 `List[List[str]]`，其余是 str）
    """
    blocks: List[Block] = []
    table_rows: List[List[str]] = []

    def flush_table() -> None:
        nonlocal table_rows
        rows = [r for r in table_rows if not _is_separator_row(r)]
        if rows:
            blocks.append(("table", rows))
        table_rows = []

    for raw_line in (content or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("|"):
            cells = _split_table_row(stripped)
            if not _is_separator_row(cells):
                table_rows.append(cells)
            continue
        flush_table()

        if not stripped:
            continue
        if stripped.startswith("### ") or stripped.startswith("## "):
            blocks.append(("h2", stripped.lstrip("#").strip()))
        elif stripped.startswith("# "):
            blocks.append(("h1", stripped[2:].strip()))
        elif stripped.startswith(("- ", "* ", "• ")):
            blocks.append(("bullet", stripped[2:].strip()))
        else:
            blocks.append(("text", stripped))

    flush_table()
    return blocks


def _rows_from_content(content: str) -> List[List[str]]:
    """给表格类格式取数据行：优先 Markdown 表格，其次 CSV / TSV 文本"""
    for kind, payload in parse_blocks(content):
        if kind == "table":
            return payload

    lines = [ln for ln in (content or "").splitlines() if ln.strip()]
    if not lines:
        return []
    delimiter = "\t" if any("\t" in ln for ln in lines) else ","
    if delimiter == "," and not any("," in ln for ln in lines):
        # 既没有表格也没有分隔符：整段当成单列表格，至少不要生成空文件
        return [[ln.strip()] for ln in lines]
    try:
        return [[_strip_cell(c) for c in row] for row in csv.reader(lines, delimiter=delimiter)]
    except csv.Error:
        return [[_strip_cell(ln)] for ln in lines]


# ---------- 各格式写出 ----------
def _write_docx(target: Path, content: str, title: str) -> str:
    try:
        from docx import Document
    except ImportError as e:  # pragma: no cover - 依赖缺失时的明确降级
        raise RuntimeError(f"生成 Word 文档需要 python-docx，当前环境缺少该依赖：{e}") from e

    doc = Document()
    if title:
        doc.add_heading(title, level=0)

    for kind, payload in parse_blocks(content):
        if kind == "h1":
            doc.add_heading(str(payload), level=1)
        elif kind == "h2":
            doc.add_heading(str(payload), level=2)
        elif kind == "bullet":
            doc.add_paragraph(str(payload), style="List Bullet")
        elif kind == "table":
            rows = payload
            table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            table.style = "Light Grid Accent 1"
            for r, row in enumerate(rows):
                for c, cell in enumerate(row):
                    table.cell(r, c).text = cell
        else:
            doc.add_paragraph(str(payload))

    doc.save(str(target))
    paragraphs = len(parse_blocks(content))
    return f"已生成 Word 文档 {target.name}（{paragraphs} 个内容块）"


def _write_xlsx(target: Path, content: str, title: str) -> str:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(f"生成 Excel 表格需要 openpyxl，当前环境缺少该依赖：{e}") from e

    wb = Workbook()
    wb.remove(wb.active)
    blocks = parse_blocks(content)
    sections: list[tuple[str, list[Block]]] = []
    heading = title or "Sheet1"
    current: list[Block] = []
    for kind, payload in blocks:
        if kind in ("h1", "h2"):
            if current:
                sections.append((heading, current))
                current = []
            heading = str(payload)
        else:
            current.append((kind, payload))
    if current:
        sections.append((heading, current))
    if not any(kind == "table" for kind, _ in blocks):
        sections = [(title or "Sheet1", [("table", _rows_from_content(content))])]
    if not sections:
        sections = [(title or "Sheet1", [("text", "（无内容）")])]

    for heading, section in sections:
        base = re.sub(r"[\\/*?:\[\]]", " ", heading).strip().strip("'")[:31] or "Sheet"
        name, index = base, 2
        while name.casefold() in {s.casefold() for s in wb.sheetnames}:
            ending = f" ({index})"
            name = base[:31 - len(ending)] + ending
            index += 1
        ws = wb.create_sheet(name)
        header_rows = []
        row_count = 0
        for kind, payload in section:
            if kind == "table":
                header_rows.append(row_count + 1)
                rows = payload
            else:
                rows = [[str(payload)]]
            for row in rows:
                if any(len(str(value)) > MAX_CELL_CHARS for value in row):
                    raise ValueError("Excel 单元格文本超过 32767 字符，请拆分内容")
                ws.append(row)
                row_count += 1
                for cell in ws[ws.max_row]:
                    # Markdown/CSV is literal content, never an executable formula.
                    if cell.data_type == "f":
                        cell.data_type = "s"
                    elif row_count not in header_rows and isinstance(cell.value, str):
                        literal = cell.value.strip()
                        if re.fullmatch(r"[+-]?(?:0|[1-9]\d*)(?:\.\d+)?%?", literal):
                            numeric = float(literal.rstrip("%"))
                            if abs(numeric) < 1e15:
                                cell.value = numeric / 100 if literal.endswith("%") else numeric
                                if literal.endswith("%"):
                                    precision = len(literal.rstrip("%").split(".")[1]) if "." in literal else 0
                                    cell.number_format = "0" + ("." + "0" * precision if precision else "") + "%"
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
        for row_index in header_rows:
            for cell in ws[row_index]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="75334B")
        for col in ws.columns:
            width = max(sum(2 if ord(ch) > 255 else 1 for ch in str(cell.value or "")) for cell in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(68, max(14, width + 3))
        ws.freeze_panes = f"A{header_rows[0] + 1}" if header_rows else "A2"
        if len(header_rows) == 1 and len(section) == 1:
            ws.auto_filter.ref = ws.dimensions
        ws.sheet_view.showGridLines = False
    wb.save(str(target))
    details = "；".join(f"{ws.title}：{ws.max_row} 行 × {ws.max_column} 列" for ws in wb)
    return f"已生成 Excel 表格 {target.name}（{len(wb.worksheets)} 个工作表；{details}）"


def _write_pptx(target: Path, content: str, title: str) -> str:
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "生成 PowerPoint 需要 python-pptx，当前环境缺少该依赖"
            f"（pip install python-pptx 后重试）：{e}"
        ) from e

    blocks = parse_blocks(content)
    prs = Presentation()

    # 首页：标题页
    first = prs.slides.add_slide(prs.slide_layouts[0])
    first.shapes.title.text = title or next(
        (str(p) for k, p in blocks if k in ("h1", "h2")), "演示文稿"
    )
    if len(first.placeholders) > 1:
        first.placeholders[1].text = "由 Syncore 生成"

    current = None
    bullet_box = None

    def new_slide(heading: str) -> None:
        nonlocal current, bullet_box
        current = prs.slides.add_slide(prs.slide_layouts[1])
        current.shapes.title.text = heading[:120]
        bullet_box = current.placeholders[1]
        bullet_box.text_frame.text = ""

    for kind, payload in blocks:
        if kind in ("h1", "h2"):
            new_slide(str(payload))
        elif kind == "table":
            if current is None:
                new_slide(title or "内容")
            rows = payload
            shape = current.shapes.add_table(
                len(rows),
                max(len(r) for r in rows),
                Inches(0.6),
                Inches(1.6),
                Inches(8.5),
                Inches(0.35 * len(rows)),
            ).table
            for r, row in enumerate(rows):
                for c, cell in enumerate(row):
                    shape.cell(r, c).text = cell
            bullet_box = None
        else:
            if current is None or bullet_box is None:
                new_slide(title or "内容")
            tf = bullet_box.text_frame
            paragraph = tf.paragraphs[0] if not tf.text else tf.add_paragraph()
            paragraph.text = ("• " if kind == "bullet" else "") + str(payload)
            paragraph.font.size = Pt(18)

    prs.save(str(target))
    return f"已生成 PowerPoint {target.name}（{len(prs.slides)} 页）"


def _write_text(target: Path, content: str) -> str:
    target.write_text(content or "", encoding="utf-8")
    return f"已生成 {target.name}（{len(content or '')} 字符）"


# ---------- 工具入口 ----------
@agent_tool(
    display_name="生成文档",
    category="文档",
    danger_level="moderate",
    allowed_agents=["file_agent"],
    health_handler=_health,
)
async def generate_document(
    path: str, content: str, title: str = "", overwrite: bool = False, ctx: ToolContext | None = None
) -> str:
    """
    在工作目录内生成一个文件，按扩展名决定格式。

    Args:
        path: 相对工作目录的路径，**必须带扩展名**（如 `周报.docx`）
        content: 内容。docx/xlsx/pptx 支持 Markdown 子集；xlsx 按标题分工作表，保留全部表格和文字说明
        title: 文档标题（docx 首页标题、pptx 封面标题、xlsx 工作表名）
        overwrite: 仅用户要求覆盖已有输出文件时为 true，默认另存
    """
    if not (path or "").strip():
        raise ValueError("path 不能为空，例如 '会议纪要.docx'")

    from app.office.service import OfficeService
    return await OfficeService(ctx).generate(path, content or "", title, overwrite)


__all__ = ["generate_document", "parse_blocks", "SUPPORTED_SUFFIXES", "_health"]
