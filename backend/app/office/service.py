"""办公统一服务：路径、冲突保护、异步执行、结果与交付元数据。"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.tools.context import ToolContext
from app.config import settings
from app.core.sandbox import resolve_agent_path
from app.office import editor
from app.office.manifest import PLUGIN_ID


class OfficeError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


class Artifact(BaseModel):
    path: str
    format: str
    size: int
    sha256: str


class OfficeResult(BaseModel):
    schema_version: str = "1.0"
    plugin: str = PLUGIN_ID
    operation: str
    summary: str
    content: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_sha256: str = ""
    data: dict = Field(default_factory=dict)

    def text(self) -> str:
        return self.model_dump_json()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class OfficeService:
    def __init__(self, ctx: ToolContext | None = None):
        self.base = ctx.workspace_root if ctx else None

    def source(self, path: str, expected_sha256: str = "") -> Path:
        target = resolve_agent_path(path, base=self.base)
        if not target.is_file():
            raise OfficeError("source_not_found", f"文件不存在：{path}")
        if target.stat().st_size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise OfficeError("file_too_large", f"文档超过 {settings.MAX_FILE_SIZE_MB}MB 限制")
        if expected_sha256 and digest(target) != expected_sha256:
            raise OfficeError("source_changed", "原件自读取后已改变，请重新读取再编辑")
        return target

    def output(self, path: str, overwrite=False, source: Path | None = None) -> Path:
        target = resolve_agent_path(path, base=self.base, for_write=True)
        originals = (settings.attachments_path / "originals").resolve()
        if target == originals or originals in target.parents:
            raise PermissionError("附件原件只读，请另存到工作目录")
        if source is not None and source.suffix.lower() != target.suffix.lower():
            raise OfficeError("format_mismatch", "编辑/填充时输出扩展名必须与源文件相同；转换格式请用 convert_document")
        if target.exists() and not overwrite:
            raise OfficeError("output_exists", "输出文件已存在，请使用新文件名，或在用户要求覆盖后设置 overwrite=true")
        return target

    async def execute(self, operation: str, action, *, target: Path | None = None,
                      source: Path | None = None, summary: str = "", warnings=None) -> str:
        def work():
            try:
                source_hash = digest(source) if source else ""
                content = action()
                artifacts = []
                if target:
                    artifacts.append(Artifact(path=str(target), format=target.suffix[1:].lower(),
                                              size=target.stat().st_size, sha256=digest(target)))
                data = {}
                if operation == "generate" and target and target.suffix.lower() == ".xlsx":
                    from openpyxl import load_workbook
                    book = load_workbook(target, read_only=True)
                    try:
                        data["sheets"] = [{"name": sheet.title, "rows": sheet.max_row,
                                           "columns": sheet.max_column} for sheet in book]
                    finally:
                        book.close()
                return OfficeResult(
                    operation=operation, summary=summary, content=content or "",
                    artifacts=artifacts, warnings=warnings or [], source_sha256=source_hash,
                    data=data,
                ).text()
            except (OfficeError, PermissionError):
                raise
            except FileExistsError as exc:
                raise OfficeError("output_exists", "输出文件已存在，请另存") from exc
            except ImportError as exc:
                raise OfficeError("dependency_missing", str(exc)) from exc
            except ValueError as exc:
                raise OfficeError("invalid_operation", str(exc)) from exc
            except Exception as exc:
                raise OfficeError("processing_failed", str(exc)) from exc

        return await asyncio.to_thread(work)

    async def generate(self, path: str, content: str, title: str, overwrite=False):
        from app.agents.tools.builtin.doc_ops import (
            SUPPORTED_SUFFIXES, _write_docx, _write_xlsx, _write_pptx, _write_text,
        )
        target = self.output(path, overwrite)
        suffix = target.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise OfficeError("unsupported_format", f"不支持的生成格式：{suffix}")
        if not content.strip():
            raise OfficeError("empty_content", "content 不能为空")

        def action():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".office-generate-", dir=target.parent) as folder:
                staged = Path(folder) / target.name
                writer = {".docx": _write_docx, ".xlsx": _write_xlsx, ".pptx": _write_pptx}.get(suffix)
                summary = writer(staged, content, title) if writer else _write_text(staged, content)
                editor.validate_file(staged)
                editor.publish_file(staged, target, overwrite)
                return summary

        return await self.execute("generate", action, target=target, summary=f"已生成 {target.name}")


def capability_result(capabilities: dict) -> str:
    return OfficeResult(operation="capabilities", summary="办公文档能力状态",
                        data=capabilities).text()
