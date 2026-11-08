"""
知识库检索工具 —— 让助手在对话中途能主动再查一次

★ 与主流程的预取是什么关系
Supervisor 在 direct 模式下**已经**会预取一轮检索（与意图识别并发，不变量 50），
所以多数问题不需要这个工具。它补的是那一轮覆盖不到的两种情况：
① 用户接着追问，需要换一个说法再查；
② 预取那一路没命中，而模型判断「这确实该在知识库里找」，于是换个关键词试一次。
没有它，模型只拿着预取那几条来源作答，问出第二个问题时只能说「资料里没有」。

★ 为什么必须区分「没命中」与「查不了」
`retrieve()` 对这两种情况的返回都是空列表（每一路都是「宁可少一路，不可整条挂」的
降级语义，见 `app/rag/retriever.py` 模块注释）。如果工具把空列表原样报成
「知识库里没有」，模型会对用户说「资料里没写过这个」——
而真相可能是 Milvus 连不上、或者这个库根本没索引过。
这是整条检索链路上最贵的一句错话，所以这里额外探一次后端状态，
把「查不了」如实说出来。探测有超时上限，探测本身挂了也只说「状态未知」。

★ `graph` 开关沿用本轮选项
`ctx` 里带着用户在这轮顶栏选的图路开关（主对话路径有；
子图 tool_executor 的投影不带，那里就退回配置默认）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agents.tools.context import ToolContext
from app.agents.tools.decorator import agent_tool
from app.config import settings

logger = logging.getLogger(__name__)

#: 每条命中在工具结果里最多显示多少字符（正文本身还长，模型要全文可以再看）
_SNIPPET_CHARS = 400
_MAX_HITS = 8
#: 后端状态探测的等待上限：探测只是补一句话，不该把一次检索拖成十秒。
#: 4s 是实测取的 —— 2s 时一次冷启动的 Milvus 连接就会超时，
#: 于是「后端其实正常、只是没命中」被误报成「状态未知」，比不探测更糟。
_PROBE_TIMEOUT = 4.0


def _snippet(hit: dict[str, Any]) -> str:
    text = " ".join(str(hit.get("text") or "").split())
    return text[:_SNIPPET_CHARS] + ("…" if len(text) > _SNIPPET_CHARS else "")


def _lane(hit: dict[str, Any]) -> str:
    """这条是被哪几路召回的（模型据此判断证据强度：多路重合通常更可靠）"""
    recalled = [str(r) for r in (hit.get("recalled_by") or []) if r]
    if recalled:
        return "+".join(recalled)
    return str(hit.get("retrieval") or "混合")


def _probe_sync() -> str:
    """检索后端状态（同步，供 `asyncio.to_thread` 调用）。返回一句人话，正常时返回空串。"""
    notes: list[str] = []
    if not settings.ES_ENABLED:
        notes.append("关键词检索（Elasticsearch）未启用")
    try:
        from app.core.milvus_client import milvus_manager

        if not milvus_manager.client.has_collection(settings.MILVUS_COLLECTION):
            notes.append("向量库还没建好集合（多半是还没索引过文档）")
    except Exception as e:  # noqa: BLE001
        notes.append(f"向量库连不上（{type(e).__name__}）")
    return "；".join(notes)


async def _backend_note() -> str:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_probe_sync), timeout=_PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001
        # 探测失败本身不该改变结论，只能说「状态未知」——
        # 报「后端正常」是谎报，报「后端故障」是冤枉。
        return "检索后端状态未知"


@agent_tool(
    display_name="搜索知识库",
    description=(
        "在知识库（用户导入的文档、笔记、项目资料）里检索，返回最相关的原文片段与出处。"
        "用户问的是**资料里写过**的内容、或需要换关键词再查一次时用；"
        "实时信息（天气/汇率/新闻）要用联网工具，别在知识库里找。"
    ),
    category="知识",
    danger_level="safe",
    read_only=True,
)
async def kb_search(
    query: str,
    top_k: int = 5,
    ctx: ToolContext | None = None,
) -> str:
    """检索知识库。

    Args:
        query: 检索语句。用具体名词与关键词，别把整句口语原样丢进来
        top_k: 最多返回几条（1-8）
    """
    text = (query or "").strip()
    if not text:
        return "query 不能为空。"
    try:
        limit = max(1, min(int(top_k), _MAX_HITS))
    except (TypeError, ValueError):
        limit = 5

    from app.rag.retriever import retrieve

    employee_id = ctx.employee_id if ctx else ""
    # 数字员工发起的检索要带上它那个项目的知识边界；主对话与普通后台任务为空 = 不设边界
    try:
        from app.workspace.knowledge_scope import scope_for_employee

        scope = scope_for_employee(employee_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("解析知识边界失败（本轮不设边界继续检索）: %s", e)
        scope = None

    # 本轮的图路开关（状态里带就用它，不带就交给配置默认）
    graph: bool | None = None
    if ctx is not None and "graph_enabled" in ctx.extras:
        graph = bool(ctx.extras.get("graph_enabled"))

    try:
        hits = await retrieve(text, top_k=limit, graph=graph, scope=scope)
    except Exception as e:  # noqa: BLE001
        return f"（知识库检索失败：{type(e).__name__}: {e}）—— 这不代表知识库里没有相关内容。"

    if not hits:
        note = await _backend_note()
        if note and note != "检索后端状态未知":
            return (
                f"「{text}」没有检索到内容。\n"
                f"当前检索后端的状态：{note} —— 所以这次是**查不了或还没索引**，"
                "不能据此断定资料里没有写过。请如实告诉用户这一情况。"
            )
        if note == "检索后端状态未知":
            return (
                f"「{text}」没有检索到内容，而检索后端的健康状况这次也没能确认。"
                "请不要把它说成「资料里没有」——先说「这次没能查到」。"
            )
        return f"「{text}」在知识库里没有匹配到的内容（检索后端显示正常）。"

    out = [f"「{text}」命中 {len(hits)} 条："]
    for i, hit in enumerate(hits, 1):
        source = str(hit.get("source") or "未知来源")
        page = hit.get("page")
        where = f"（第 {page} 页）" if page else ""
        score = hit.get("score")
        tail = f"，相关度 {float(score):.3f}" if isinstance(score, (int, float)) else ""
        out.append(f"{i}. {source}{where}｜{_lane(hit)}{tail}")
        body = _snippet(hit)
        if body:
            out.append(f"   {body}")
    out.append("（片段可能被截断，需要更多上下文时把出处告诉我，或换更具体的关键词再查一次。）")
    return "\n".join(out)


__all__ = ["kb_search"]
