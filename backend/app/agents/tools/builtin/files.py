"""
内置工具 —— 文件与检索 (files)

★ 与旧 `file_ops` 的差别不只是多了几个名字

旧实现只有「整文件读 / 整文件写」：模型要看 200 行里的第 30 行，只能把整个文件
读进上下文（还常被 8000 字符截断）；要改一个词，只能把整个文件重写一遍 ——
一次笔误就毁掉整篇。主流 Agent 的做法是把粒度交给模型：
`read` 带行区间、`edit` 做精确字符串替换、`glob`/`grep` 用来先定位再动手。
这一组工具合起来才让「改文档」这件事在长任务里可靠。

★ 路径边界
所有路径经 `resolve_agent_path` 判定：沙箱目录内自由读写，
沙箱外只允许用户配置在 `AGENT_ALLOWED_DIRS` 里的目录。
相对路径相对**沙箱根**解析，因此 `../x` 一律越界（校验在 resolve 之后做）。

★ 覆盖保护（`edit`/`write` 要求先 `read`）
模型没读过的文件就允许它整篇覆盖，等于把「凭印象重写用户文档」这条路打开。
所以本模块记录每个会话读过的路径，未读过的文件拒绝写/改并让它先读 ——
这条判定必须在同一进程内，代价见 `_READ_TRACK` 的注释。
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import time
from collections import OrderedDict
from pathlib import Path

from app.agents.tools.context import ToolContext, get_tool_context
from app.agents.tools.decorator import agent_tool
from app.core.sandbox import display_path, resolve_agent_path, sandbox_root

logger = logging.getLogger(__name__)

MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000
#: 超过这个体积的文件不做全文搜索（grep 会扫很多文件，单文件上限要收紧）
MAX_GREP_FILE_BYTES = 2_000_000
MAX_GREP_FILES = 20_000
#: 目录遍历默认忽略的名字：这些要么巨大要么不是内容
_SKIP_DIR_NAMES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".idea",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".agent_outputs",
}
#: 明显不是文本的后缀，读到就直说，不让模型把二进制当文本消化
_BINARY_SUFFIXES = {
    ".exe", ".dll", ".so", ".dylib", ".zip", ".gz", ".7z", ".rar", ".png",
    ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".mp3", ".mp4", ".avi",
    ".mov", ".wav", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}

# ---------- 读过的文件（覆盖保护的凭据） ----------
#: (thread_id, 绝对路径) → 读取时间。带上限，避免长会话把内存吃光。
_READ_TRACK: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_READ_TRACK_MAX = 4000


def _mark_read(path: Path) -> None:
    key = (get_tool_context().session_key or "*", str(path))
    _READ_TRACK[key] = time.time()
    _READ_TRACK.move_to_end(key)
    while len(_READ_TRACK) > _READ_TRACK_MAX:
        _READ_TRACK.popitem(last=False)


def _has_read(path: Path) -> bool:
    ctx = get_tool_context()
    if not ctx.session_key:
        # 没有会话身份（脚本直调、健康检查）时无从判断「读过没读过」，放行。
        # 主对话与子 Agent 都带 thread_id，真正的 agent 路径不会被放行。
        return True
    return (ctx.session_key, str(path)) in _READ_TRACK


def _require_prior_read(target: Path, verb: str) -> str | None:
    if _has_read(target):
        return None
    return (
        f"[需要先读] {display_path(target)} 还没被读取过，不能直接{verb}。"
        "请先用 read 看清现有内容，避免凭印象覆盖掉用户已有的东西。"
    )


def _health() -> bool:
    return sandbox_root().is_dir()


def _base(ctx: ToolContext | None) -> Path | None:
    """本轮的工作目录基准；会话没带时交给 `resolve_agent_path` 兜沙箱根"""
    return ctx.workspace_root if ctx and ctx.workspace_root else None


# ---------- read —— safe ----------
@agent_tool(
    display_name="读取文件",
    category="文件",
    danger_level="safe",
    read_only=True,
    allowed_agents=["file_agent"],
)
async def read(
    path: str,
    offset: int = 1,
    limit: int = MAX_READ_LINES,
    ctx: ToolContext | None = None,
) -> str:
    """读取文本文件的内容，按行返回并带行号前缀。

    一次最多 2000 行；文件更长时用 offset/limit 分段读，不要一口气读完整篇。
    要看目录用 ls，要找文件用 glob，要搜内容用 grep。

    Args:
        path: 文件路径（相对工作目录，或授权目录内的绝对路径）
        offset: 起始行号，从 1 开始
        limit: 最多返回多少行
    """
    target = resolve_agent_path(path, base=_base(ctx))
    if not target.exists():
        return f"[错误] 文件不存在：{path}"
    if target.is_dir():
        return f"[错误] '{path}' 是目录，请用 ls"
    if target.suffix.lower() in _BINARY_SUFFIXES:
        if target.suffix.lower() in {".docx", ".xlsx", ".pptx", ".pdf"}:
            return f"[提示] {target.name} 是办公文档，请使用 read_document 读取结构和正文；编辑原件使用 edit_document。"
        return (
            f"[错误] {target.name} 是二进制文件（{target.suffix}），不能按文本读取。"
            f"体积 {target.stat().st_size} 字节。"
        )

    try:
        raw = target.read_bytes()
    except OSError as e:
        return f"[错误] 读取失败：{e}"

    if b"\x00" in raw[:4096]:
        return f"[错误] {target.name} 看起来是二进制/编码无法识别的文件，不能按文本读取。"

    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    total = len(lines)

    start = max(1, int(offset))
    count = max(1, min(int(limit), MAX_READ_LINES))
    if start > total and total > 0:
        return f"[提示] 该文件共 {total} 行，offset={start} 已超出末尾。"

    window = lines[start - 1 : start - 1 + count]
    width = len(str(total))
    body = "\n".join(
        f"{start + i:>{width}}\t" + (ln[:MAX_LINE_CHARS] + "…[该行已截断]" if len(ln) > MAX_LINE_CHARS else ln)
        for i, ln in enumerate(window)
    )
    _mark_read(target)

    last_shown = start - 1 + len(window)
    header = f"{display_path(target)}（共 {total} 行，显示 {start}-{last_shown}）"
    if not body:
        return f"{header}\n（所选区间没有内容）"
    if last_shown < total:
        return f"{header}\n{body}\n\n[还有 {total - last_shown} 行，用 offset={last_shown + 1} 继续读]"
    return f"{header}\n{body}"


# ---------- ls —— safe ----------
@agent_tool(
    display_name="浏览目录",
    description="列出目录内容（目录在前、文件在后，带体积）。想按名字模式找文件用 glob，想搜内容用 grep。",
    category="文件",
    danger_level="safe",
    read_only=True,
    allowed_agents=["file_agent"],
)
async def ls(path: str = ".", include_hidden: bool = False, ctx: ToolContext | None = None) -> str:
    """
    Args:
        path: 目录路径，默认工作目录根
        include_hidden: 是否包含 . 开头的隐藏项
    """
    target = resolve_agent_path(path, base=_base(ctx))
    if not target.exists():
        return f"[错误] 目录不存在：{path}"
    if not target.is_dir():
        return f"[错误] '{path}' 不是目录，用 read 读它"

    entries = [p for p in target.iterdir() if include_hidden or not p.name.startswith(".")]
    entries.sort(key=lambda p: (p.is_file(), p.name.lower()))
    if not entries:
        return f"{display_path(target)}/ 是空目录"

    dirs = [e for e in entries if e.is_dir()]
    files = [e for e in entries if e.is_file()]
    lines = [f"[目录] {e.name}/" for e in dirs]
    lines += [f"[文件] {e.name}  {_human_size(e.stat().st_size)}" for e in files]
    return f"{display_path(target)}/（{len(dirs)} 个子目录，{len(files)} 个文件）\n" + "\n".join(lines[:500])


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


# ---------- glob —— safe ----------
@agent_tool(
    display_name="查找文件",
    category="文件",
    danger_level="safe",
    read_only=True,
    allowed_agents=["file_agent"],
)
async def glob(
    pattern: str,
    path: str = ".",
    limit: int = 200,
    ctx: ToolContext | None = None,
) -> str:
    """按文件名模式查找文件，结果按修改时间从新到旧排序。

    支持 `**` 递归，例如 `**/*.md`、`报告*.docx`、`src/**/*.py`。

    Args:
        pattern: 文件名模式（glob 语法）
        path: 从哪个目录开始找，默认工作目录根
        limit: 最多返回多少条
    """
    base = resolve_agent_path(path, base=_base(ctx))
    if not base.is_dir():
        return f"[错误] '{path}' 不是目录"

    # `**/x` 与 `x` 等价处理：rglob 本身已覆盖递归语义
    clean = pattern.lstrip("/")
    if clean.startswith("**/"):
        clean = clean[3:]

    hits: list[Path] = []
    for candidate in _walk(base, clean):
        try:
            # clean 已去掉 `**/` 前缀：单层模式（`*.md`）匹配文件名，
            # 带层级的模式（`src/**/*.py`）匹配相对路径
            rel = str(candidate.relative_to(base)).replace(os.sep, "/")
            if fnmatch.fnmatch(candidate.name, clean) or fnmatch.fnmatch(rel, pattern):
                hits.append(candidate)
                if len(hits) >= max(1, int(limit)) * 4:
                    break
        except (OSError, ValueError):
            continue

    if not hits:
        return f"没找到匹配 '{pattern}' 的文件（搜索范围：{display_path(base)}）"

    hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    shown = hits[: max(1, int(limit))]
    lines = [f"{display_path(p)}  {_human_size(p.stat().st_size)}" for p in shown]
    more = f"\n\n[还有 {len(hits) - len(shown)} 条未显示，缩小 pattern 或提高 limit]" if len(hits) > len(shown) else ""
    return f"匹配 '{pattern}' 共 {len(hits)} 个文件：\n" + "\n".join(lines) + more


def _walk(base: Path, name_pattern: str = ""):
    """遍历目录，跳过明显的依赖/构建目录与隐藏目录；给了 name_pattern 时按名字先滤一层"""
    for root, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")
        ]
        for name in (fnmatch.filter(filenames, name_pattern) if name_pattern else filenames):
            yield Path(root) / name


# ---------- grep —— safe ----------
@agent_tool(
    display_name="搜索文件内容",
    category="文件",
    danger_level="safe",
    read_only=True,
    allowed_agents=["file_agent"],
)
async def grep(
    pattern: str,
    path: str = ".",
    glob: str = "",
    output_mode: str = "files_with_matches",
    ignore_case: bool = False,
    context_lines: int = 0,
    head_limit: int = 100,
    ctx: ToolContext | None = None,
) -> str:
    """在文件内容里做正则搜索（内置实现，不依赖外部 rg/grep 命令）。

    output_mode 三种：files_with_matches 只给文件名（默认，适合先定位）、
    content 给匹配行（配 context_lines 看上下文）、count 给每个文件的命中条数。

    Args:
        pattern: 正则表达式
        path: 搜索起始目录
        glob: 只搜匹配这个文件名模式的文件，如 `*.md`
        output_mode: files_with_matches / content / count
        ignore_case: 忽略大小写
        context_lines: content 模式下每个命中前后各带几行
        head_limit: 最多返回多少条结果
    """
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"[错误] 正则不合法：{e}"

    base = resolve_agent_path(path, base=_base(ctx))
    mode = output_mode if output_mode in {"files_with_matches", "content", "count"} else "files_with_matches"
    cap = max(1, int(head_limit))

    matched_files: list[str] = []
    count_lines: list[str] = []
    content_lines: list[str] = []
    scanned = 0

    for candidate in _walk(base, glob):
        if candidate.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            if candidate.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        lines = text.splitlines()
        hits = [i for i, ln in enumerate(lines) if regex.search(ln)]
        if not hits:
            continue

        rel = display_path(candidate)
        if mode == "content":
            for i in hits[:cap]:
                lo = max(0, i - int(context_lines))
                hi = min(len(lines), i + int(context_lines) + 1)
                content_lines.append(f"{rel}:{i + 1}:")
                content_lines.extend(f"  {lo + j + 1}\t{lines[lo + j][:300]}" for j in range(hi - lo))
                if len(content_lines) > cap * 4:
                    break
        elif mode == "count":
            count_lines.append(f"{rel}: {len(hits)}")
        else:
            matched_files.append(rel)

        if len(matched_files or count_lines or content_lines) >= cap:
            break
        if scanned >= MAX_GREP_FILES:
            break

    if mode == "content" and content_lines:
        return f"匹配 '{pattern}'（content 模式）：\n" + "\n".join(content_lines[: cap * 4])
    if mode == "count" and count_lines:
        return f"匹配 '{pattern}'，共 {len(count_lines)} 个文件：\n" + "\n".join(count_lines)
    if matched_files:
        return (
            f"匹配 '{pattern}'，共 {len(matched_files)} 个文件：\n"
            + "\n".join(f"  {f}" for f in matched_files)
        )
    return f"没有文件包含 '{pattern}'（范围：{display_path(base)}，已扫描 {scanned} 个文件）"


# ---------- write —— moderate ----------
@agent_tool(
    display_name="写入文件",
    category="文件",
    danger_level="moderate",
    allowed_agents=["file_agent"],
)
async def write(
    path: str,
    content: str,
    ctx: ToolContext | None = None,
) -> str:
    """创建新文件，或整体覆盖一个已存在且本轮读过的文本文件。

    改文件里的某几句请用 edit，不要用本工具重写整篇 —— 整篇重写很容易丢内容。
    要生成 Word/Excel/PPT 用 generate_document。父目录会自动创建。

    Args:
        path: 文件路径
        content: 完整文本内容
    """
    target = resolve_agent_path(path, base=_base(ctx), for_write=True)
    if target.exists() and target.is_dir():
        return f"[错误] '{path}' 是一个已存在的目录，不能写成文件"

    if target.exists():
        blocked = _require_prior_read(target, "覆盖")
        if blocked:
            return blocked

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _mark_read(target)
    return f"已写入 {display_path(target)}（{len(content)} 字符）"


# ---------- edit —— moderate ----------
@agent_tool(
    display_name="修改文件",
    category="文件",
    danger_level="moderate",
    allowed_agents=["file_agent"],
)
async def edit(
    path: str,
    old_string: str,
    new_string: str,
    allow_multiple: bool = False,
    ctx: ToolContext | None = None,
) -> str:
    """在文件里做精确字符串替换，一次改一处。改前先 read。

    old_string 必须与文件内容**逐字符一致**（含空格与缩进），且在文件里唯一；
    出现多次时会失败，此时请扩大 old_string 的上下文直到能唯一定位，
    或确实想全量替换时传 allow_multiple=true。

    Args:
        path: 文件路径
        old_string: 要被替换的原文
        new_string: 替换成的新内容
        allow_multiple: old_string 出现多次时是否全部替换
    """
    target = resolve_agent_path(path, base=_base(ctx), for_write=True)
    if not target.exists():
        return f"[错误] 文件不存在：{path}（要新建请用 write）"
    if not old_string:
        return "[错误] old_string 不能为空"
    if old_string == new_string:
        return "[错误] old_string 与 new_string 相同，这是一次空操作"

    blocked = _require_prior_read(target, "修改")
    if blocked:
        return blocked

    text = target.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(old_string)
    if occurrences == 0:
        return (
            f"[错误] 在 {display_path(target)} 里找不到 old_string。"
            "请重新 read 看清原文的空白与缩进后逐字复制 —— 不要凭记忆构造。"
        )
    if occurrences > 1 and not allow_multiple:
        return (
            f"[错误] old_string 在文件里出现 {occurrences} 次，无法确定改哪一处。"
            "请扩大上下文直到唯一，或传 allow_multiple=true 全量替换。"
        )

    updated = (
        text.replace(old_string, new_string)
        if allow_multiple
        else text.replace(old_string, new_string, 1)
    )
    target.write_text(updated, encoding="utf-8")
    _mark_read(target)
    n = occurrences if allow_multiple else 1
    return f"已替换 {display_path(target)} 中的 {n} 处（文件现有 {len(updated.splitlines())} 行）"


# ---------- delete —— danger ----------
@agent_tool(
    display_name="删除文件",
    description="删除文件或目录（危险操作，需要用户审批）。删除目录必须显式传 recursive=true。",
    category="文件",
    danger_level="danger",
    allowed_agents=["file_agent"],
)
async def delete(path: str, recursive: bool = False, ctx: ToolContext | None = None) -> str:
    """
    Args:
        path: 要删除的文件或目录
        recursive: 删除非空目录时必须显式置为 true
    """
    target = resolve_agent_path(path, base=_base(ctx), for_write=True)
    if not target.exists():
        return f"[错误] 目标不存在：{path}"

    root = sandbox_root()
    if target == root:
        return "[错误] 不允许删除工作目录本身"

    if target.is_dir():
        children = list(target.iterdir())
        if children and not recursive:
            return (
                f"[错误] {display_path(target)} 是非空目录（{len(children)} 项）。"
                "确认要连带删除全部内容时传 recursive=true。"
            )
        shutil.rmtree(target)
        return f"已删除目录 {display_path(target)} 及其 {len(children)} 项内容"

    target.unlink()
    return f"已删除文件 {display_path(target)}"


__all__ = ["delete", "edit", "glob", "grep", "ls", "read", "write"]
