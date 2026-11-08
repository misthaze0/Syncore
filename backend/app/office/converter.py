"""Filee 本地转换适配，不执行模型提供的命令行。"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import settings
from app.core.subproc import safe_env
from app.office.editor import publish_file, validate_file
from app.office.manifest import plugin_manifest

INPUT_SUFFIXES = frozenset({".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".odt", ".ods", ".odp", ".rtf"})
OUTPUT_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx", ".pdf"})


def executable() -> str | None:
    configured = settings.OFFICE_FILEE_PATH
    if configured:
        return shutil.which(configured)
    return next((found for name in ("filee-cli", "filee") if (found := shutil.which(name))), None)


def capabilities() -> dict:
    return {
        "plugin": plugin_manifest(),
        "native": {"read": ["docx", "xlsx", "pptx", "pdf"], "edit": ["docx", "xlsx", "pptx"]},
        "markitdown": importlib.util.find_spec("markitdown") is not None,
        "docling": importlib.util.find_spec("docling") is not None,
        "filee": bool(executable()),
        "officecli_mcp": bool(shutil.which("officecli")),
        "notes": ["openpyxl 不执行公式重算", "PDF 反向转换不保证还原版式", "外部转换引擎及 Docling/OCR 模型须预先部署才能离线使用"],
    }


def convert(source: Path, target: Path, overwrite: bool = False) -> str:
    if source.suffix.lower() not in INPUT_SUFFIXES or target.suffix.lower() not in OUTPUT_SUFFIXES:
        raise ValueError("转换输入支持 Office/PDF/ODF/RTF，输出支持 docx/xlsx/pptx/pdf")
    if source.suffix.lower() == target.suffix.lower():
        raise ValueError("源格式与目标格式相同，无需转换")
    binary = executable()
    if not binary:
        raise ValueError("未安装本地转换引擎。请配置 OFFICE_FILEE_PATH（filee-cli.exe）；安装后重试")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".office-convert-", dir=target.parent) as folder:
        root = Path(folder)
        incoming = root / "input"
        output = root / "output"
        incoming.mkdir()
        output.mkdir()
        staged_source = incoming / ("document" + source.suffix.lower())
        shutil.copyfile(source, staged_source)
        args = [binary, "convert", str(staged_source), "--to", target.suffix[1:].lower(), "--output", str(output), "--json"]
        try:
            result = subprocess.run(
                args, cwd=root, env=safe_env(), capture_output=True,
                timeout=settings.OFFICE_CONVERT_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("本地文档转换超时，未发布输出文件") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")[-1500:]
            raise ValueError(f"filee 转换失败：{detail}")
        produced = list(output.glob("*" + target.suffix.lower()))
        if len(produced) != 1:
            raise ValueError("转换引擎未产生唯一的目标文件，请确认格式受支持且转换引擎完整")
        validate_file(produced[0])
        publish_file(produced[0], target, overwrite)
    return "filee"
