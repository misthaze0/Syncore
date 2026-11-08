"""办公工具：结构读取、保留原件的定点编辑、模板填充和本地转换。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.tools.context import ToolContext
from app.agents.tools.decorator import agent_tool
from app.office import converter, editor, reader
from app.office.service import OfficeService, capability_result


class EditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(description="已有 docx/xlsx/pptx 路径，可以是上传附件原件路径")
    path: str = Field(description="输出路径，扩展名须与 source 一致；默认另存新文件")
    operations: list[editor.EditOperation] = Field(min_length=1, max_length=100)
    overwrite: bool = Field(default=False, description="仅用户要求覆盖时设置 true；附件原件不可覆盖")
    expected_sha256: str = Field(default="", description="可选：读取结果的 source_sha256，防止读取后原件被外部修改")


class MergeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    path: str
    values: dict[str, editor.Scalar]
    overwrite: bool = False


class BatchMergeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    path: str = Field(description="输出 ZIP 路径，包含按记录顺序编号的可编辑文档")
    records: list[dict[str, editor.Scalar]] = Field(min_length=1, max_length=500)
    overwrite: bool = False


def _source(path: str, ctx: ToolContext | None, expected_sha256: str = "") -> Path:
    return OfficeService(ctx).source(path, expected_sha256)


def _output(source: Path, path: str, ctx: ToolContext | None, overwrite: bool, same_format=True):
    return OfficeService(ctx).output(path, overwrite, source if same_format else None)


@agent_tool(display_name="办公能力状态", category="文档", read_only=True, allowed_agents=["file_agent"])
def office_capabilities() -> str:
    """查看本机办公能力及可选转换、复杂解析引擎是否已安装。"""
    return capability_result(converter.capabilities())


@agent_tool(display_name="读取办公文档", category="文档", read_only=True, allowed_agents=["file_agent"])
async def read_document(
    path: str, offset: int = 1, limit: int = 200,
    engine: Literal["native", "markitdown", "docling"] = "native",
    ctx: ToolContext | None = None,
) -> str:
    """读取 Word/Excel/PPT/PDF 的结构化文本、表格和公式，按行分页。

    原生读取返回段落定位符，编辑已有文档前先用本工具确认目标与原文。
    MarkItDown 用于 Markdown 提取，Docling 用于复杂 PDF（需可选依赖及模型）。

    Args:
        path: 工作目录或上传附件的原件路径
        offset: 起始行，从 1 开始
        limit: 返回行数，上限 1000；超长段落/行拆成连续文本片段，可继续分页
        engine: native 默认无需额外安装；markitdown/docling 为可选解析增强
    """
    source = _source(path, ctx)
    if source.suffix.lower() not in reader.OFFICE_SUFFIXES:
        raise ValueError("支持 docx/xlsx/pptx/pdf，纯文本使用 read")
    start = max(1, offset)
    count = max(1, min(limit, 1000))

    def window():
        original = reader.native_lines(source) if engine == "native" else iter(reader.extract_text(source, engine=engine).splitlines())

        def chunks():
            try:
                for line in original:
                    if not line:
                        yield ""
                    for pos in range(0, len(line), 4000):
                        yield ("[同一结构续文] " if pos else "") + line[pos:pos + 4000]
            finally:
                if hasattr(original, "close"):
                    original.close()

        lines = chunks()
        result = []
        chars = 0
        more = False
        try:
            for i, line in enumerate(lines, 1):
                if i < start:
                    continue
                if len(result) >= count or (result and chars + len(line) > 24000):
                    more = True
                    break
                result.append(f"{i}\t{line}")
                chars += len(line)
        finally:
            if hasattr(lines, "close"):
                lines.close()
        tail = f"\n[还有内容，下次 offset={start + len(result)}]" if more else "\n[文件结束]"
        return f"{source.name}（engine={engine}）\n" + "\n".join(result) + tail

    return await OfficeService(ctx).execute("read", window, source=source, summary=f"已读取 {source.name}")


@agent_tool(display_name="编辑办公文档", category="文档", danger_level="moderate", allowed_agents=["file_agent"], args_model=EditArgs)
async def edit_document(source: str, path: str, operations: list[dict], overwrite: bool = False, expected_sha256: str = "", ctx: ToolContext | None = None) -> str:
    """定点编辑已有 Word/Excel/PPT，保留其他内容和样式。先 read_document 获取定位符与原文。

    Word/PPT 用 replace_text（target、old_text、text）；Excel 用 set_cell（sheet、target、old_text、value）、append_row（sheet、values）、add_chart（sheet、data_range、target、text）。
    Word 表格 append_row 用 target=t:1、values；支持页眉页脚、目录及 replace_text_tracked 修订替换。默认另存，操作失败不发布文件。
    """
    src = _source(source, ctx, expected_sha256)
    target = _output(src, path, ctx, overwrite)
    ops = EditArgs(source=source, path=path, operations=operations, overwrite=overwrite).operations
    return await OfficeService(ctx).execute("edit", lambda: editor.edit_document(src, target, ops, overwrite),
        source=src, target=target, summary=f"已编辑并校验 {target.name}（{len(ops)} 项操作）")


@agent_tool(display_name="填充文档模板", category="文档", danger_level="moderate", allowed_agents=["file_agent"], args_model=MergeArgs)
async def merge_document(source: str, path: str, values: dict, overwrite: bool = False, ctx: ToolContext | None = None) -> str:
    """用 JSON 数据填充 docx/xlsx/pptx 模板的 {{占位符}}，保留格式并另存。缺少字段时报错，不留下半成品。

    批量合同/周报可由工作流为每条数据调用一次，path 使用不同文件名。
    """
    src = _source(source, ctx)
    target = _output(src, path, ctx, overwrite)
    data = MergeArgs(source=source, path=path, values=values, overwrite=overwrite)
    return await OfficeService(ctx).execute("merge", lambda: editor.merge_document(src, target, data.values, overwrite),
        source=src, target=target, summary=f"已填充模板并校验 {target.name}")


@agent_tool(display_name="批量填充文档", category="文档", danger_level="moderate", allowed_agents=["file_agent"], args_model=BatchMergeArgs)
async def batch_merge_documents(source: str, path: str, records: list[dict], overwrite: bool = False, ctx: ToolContext | None = None) -> str:
    """一次用多条 JSON 数据填充同一 docx/xlsx/pptx 模板，交付含可编辑文档的 ZIP。

    records 最多 500 条，按顺序生成 0001.docx 等文件；任一条缺字段则整批失败，不交付不完整压缩包。
    """
    src = _source(source, ctx)
    target = _output(src, path, ctx, overwrite, same_format=False)
    if target.suffix.lower() != ".zip":
        raise ValueError("批量填充的 path 必须是 .zip 路径")
    data = BatchMergeArgs(source=source, path=path, records=records, overwrite=overwrite)
    return await OfficeService(ctx).execute("batch_merge", lambda: editor.batch_merge(src, target, data.records, overwrite),
        source=src, target=target, summary=f"已批量填充并校验 {len(data.records)} 份文档：{target.name}")


@agent_tool(display_name="转换办公文档", category="文档", danger_level="moderate", allowed_agents=["file_agent"])
async def convert_document(source: str, path: str, overwrite: bool = False, ctx: ToolContext | None = None) -> str:
    """用本地 Filee 引擎转换 Office 与 PDF，需先部署转换引擎。path 扩展名决定目标格式。

    PDF 转 Word 版式可能改变；不会用纯文本重建冒充高保真转换。
    """
    src = _source(source, ctx)
    target = _output(src, path, ctx, overwrite, same_format=False)
    return await OfficeService(ctx).execute("convert", lambda: converter.convert(src, target, overwrite),
        source=src, target=target, summary=f"已转换并校验 {target.name}",
        warnings=["PDF 反向转换需核对版式"] if src.suffix.lower() == ".pdf" else [])
