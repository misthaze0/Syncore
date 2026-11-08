"""
「这一轮产出了哪些文件」—— 从事件流快照里派生

★ 为什么不新增一种 SSE 事件（例如 `file_written`）

事件流是前后端**唯一契约**（不变量 15），加一种事件的成本不只是后端多 yield 一行：
前端 reducer、历史重放、类型定义三处都要同步，且任何一处漏了都会出现
「实时流有、重放没有」这种最难查的偏差。

而这件事完全可以从**已有事件**里推出来：`tool_call` 带 `args`（含 `path`）、
`tool_result` 带 `success`。两者都在事件流里且都已落盘，
于是「这一轮写了哪些文件」是一份**派生物**，不必成为契约的一部分。

★ 为什么判据是「tool_call 与成功的 tool_result 配对」

只看 `tool_call` 会把「用户拒绝执行 / 执行失败」也算成产出（审批被拒的
`write` 会发一条 `success=False` 的 `tool_result`）；
只看 `tool_result` 又拿不到路径（`result` 是给人读的一句话）。
所以必须按 `call_id` 配对，且以 `success=True` 为准。

同一路径在一轮里被写多次只记一条（后写覆盖前写，取第一次出现的顺序位置）——
它对应磁盘上同一个文件，列两遍只会让用户以为产出两份。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List
from app.office.manifest import WRITING_TOOLS

#: 会产出文件的工具 —— 新增文件类工具时**必须**在这里登记，
#: 否则「本次产出 N 个文件」会静默漏掉它（`test_conversation_files.py` 有断言盯着）。
FILE_WRITING_TOOLS = WRITING_TOOLS | {"write", "edit"}


def _sandbox_relative(path: str, base: str = "") -> str:
    """
    工具入参里的路径 → **沙箱相对路径**（下载 / 预览 / 打包唯一认得的形状）。

    ★ 为什么必须换一次算，而不是原样记下来

    工具把相对路径按**本轮工作区**解析（`resolve_agent_path(path, base=workdir)`）。
    对话自动分配了 `2026102901/` 之后，模型写的是「报告.docx」，
    磁盘上的真实位置是 `<沙箱>/2026102901/报告.docx`。
    如果照抄入参，下载接口会在沙箱根下找一个不存在的文件 —— 卡片有、点开 404。

    绝对路径同理：模型偶尔会传绝对路径，这里统一换算回沙箱内的相对形式。

    沙箱之外的路径返回空串（不收）：那类目录是用户自己点选授权的工作区，
    Agent 写进去没问题，但「产出文件」这套接口刻意只服务沙箱内的文件
    （`resolve_in_sandbox` 是严格边界），做成下载按钮只会点出一次越权。
    """
    from app.core.sandbox import sandbox_root

    raw = str(path or "").strip()
    if not raw:
        return ""
    root = sandbox_root()
    anchor = root
    if base:
        try:
            anchor = Path(base).expanduser().resolve()
        except OSError:
            anchor = root
    try:
        candidate = Path(raw).expanduser()
        target = (candidate if candidate.is_absolute() else anchor / candidate).resolve()
        rel = target.relative_to(root)
    except (OSError, ValueError):
        return ""
    return "" if str(rel) == "." else rel.as_posix()


def collect_written_files(
    events: Iterable[Dict[str, Any]] | None, base: str = ""
) -> List[Dict[str, str]]:
    """
    按事件顺序提取本轮**成功写入**的文件。

    返回 `[{"path": "2026102901/报告.docx", "tool": "generate_document"}, …]`，
    路径是**相对沙箱根**的形式，磁盘上的真实位置由下载接口用
    `resolve_in_sandbox()` 现算 —— 这里不落绝对路径，
    免得工作目录换了之后历史记录指向一个不存在的地方。

    `base` 是本轮工作区的绝对路径（可以就是沙箱根）。它只影响「相对路径按哪儿解析」，
    不影响存下来的形状：同一段对话换不换工作区，记录里都是沙箱相对路径。
    """
    pending: Dict[str, Dict[str, str]] = {}
    collected: Dict[str, Dict[str, str]] = {}

    for event in events or []:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")

        if etype == "tool_call":
            tool = str(event.get("tool_name") or "")
            if tool not in FILE_WRITING_TOOLS:
                continue
            args = event.get("args")
            path = ""
            if isinstance(args, dict):
                path = str(args.get("path") or "").strip()
            if not path:
                continue
            rel = _sandbox_relative(path, base)
            if not rel:
                continue
            pending[str(event.get("call_id") or "")] = {"path": rel, "tool": tool}

        elif etype == "tool_result":
            if event.get("success"):
                for path in event.get("files") or []:
                    rel = _sandbox_relative(str(path), base)
                    if rel:
                        collected.setdefault(rel, {"path": rel, "tool": str(event.get("tool_name") or "")})
            info = pending.pop(str(event.get("call_id") or ""), None)
            # 失败 / 被拒绝的执行不算产出：审批被拒也会发一条 success=False 的
            # tool_result，把它当产出会让界面凭空多出一个并不存在的文件。
            if info is None or not event.get("success"):
                continue
            collected.setdefault(info["path"], info)

    return list(collected.values())


__all__ = ["collect_written_files", "FILE_WRITING_TOOLS"]
