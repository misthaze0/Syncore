"""
聊天接口 - SSE 流式输出

Phase 1: 直接走 RAG 管道
Phase 2: 替换为 LangGraph 多 Agent 事件流（见 chat.py 中 graph 分支）

★ 为什么用 sse-starlette 的 EventSourceResponse 而不是裸 StreamingResponse

本项目的事件流有两段**可能非常长的静默期**：
1. HITL 审批等待 —— 用户可能几分钟都不点弹窗；
2. 思考型模型（qwen3.x-flash 等）的首字延迟 —— 可达十几秒。

静默期内没有任何字节流动，Nginx 默认 60s 空闲超时会掐断连接，
前端表现为「弹窗还没点，会话就断了」。EventSourceResponse 的 ping 机制
每 15s 注入一行 `: ping` 注释 —— 浏览器忽略它，但代理会重置空闲计时器。
这是 SSE 生产实践的标准解法，且 sse-starlette 本就是项目依赖（零新增）。

其余对齐项：断连时 Starlette 会取消本响应的生成器，
进而触发 `get_agent_event_stream` 的 CancelledError 路径 ——
后台图任务被取消、LLM 调用停止（「断连即停止计费」），
见 `agents/graph.py::get_agent_event_stream`。
"""

import json
import logging
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.schemas.chat import ChatRequest
from app.workdir import resolve_thread_root as _resolve_workdir_root

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

# Phase 2 标记：是否启用 LangGraph 多 Agent 模式
_GRAPH_ENABLED = False
_graph_instance = None
_active_threads: set[str] = set()


class ChatLifecycleEvent(BaseModel):
    thread_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    action: Literal["stream_started", "stream_completed", "stream_incomplete", "stop_requested",
                    "new_session", "history_restore_ignored", "scope_restore_deferred"]
    target_thread_id: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")


@router.post("/lifecycle")
async def record_chat_lifecycle(event: ChatLifecycleEvent):
    """Local diagnostics: identifiers and action only, never messages or credentials."""
    logger.info("聊天生命周期: thread=%s action=%s target=%s",
                event.thread_id, event.action, event.target_thread_id)
    return {"ok": True}


def enable_graph(graph):
    """Phase 2 调用：注册 LangGraph 实例，切换到多 Agent 模式"""
    global _GRAPH_ENABLED, _graph_instance
    _GRAPH_ENABLED = True
    _graph_instance = graph
    logger.info("已启用 LangGraph 多 Agent 模式")


def get_enabled_graph():
    """已编译的图；None = 还在 Phase 1 纯 RAG 模式。

    微信通道 worker 走的是与桌面端**同一条**主链路 —— 它必须拿到同一个实例，
    而不是自己再编译一张图（两份 checkpointer / 两套工具注册会立刻漂移）。
    """
    return _graph_instance


async def _stream_rag(request: ChatRequest):
    """Phase 1: RAG 直连模式"""
    from app.rag.pipeline import rag_answer_with_sources

    async for event in rag_answer_with_sources(request.message, top_k=request.top_k):
        yield {"data": json.dumps(event, ensure_ascii=False)}


async def _stream_graph(request: ChatRequest):
    """
    Phase 2: LangGraph 多 Agent 模式
    通过 graph.astream 推送全量 Agent 事件流
    """
    from app.agents.graph import get_agent_event_stream
    from app.agents.dialogue import split_assistant_messages
    from app.agents.think import split_think
    from app.attachments import load_attachments
    from app.channels.wechat_worker import wechat_channel
    from app.conversations.store import ASSISTANT_MAIN_THREAD_ID

    # 附件在进图之前解析好：正文注入本轮 HumanMessage（state.initial_state），
    # 元数据随轮次落盘。缺失/过期的附件在这里被跳过并告警 ——
    # 不让「一份 24 小时前传的文件过期了」升级成整轮对话失败。
    attachments = load_attachments(request.attachments)

    # 本轮工作区：只可能来自服务端的「会话 ↔ 工作区」绑定表（见 app/workdir）。
    # 请求体里**没有**这个字段 —— 所以模型、钩子、任何一段被注入的文本
    # 都没办法把这一轮挪到别的目录里去，改目录只有用户在输入框下面那一次点击。
    root = _resolve_workdir_root(request.thread_id)

    answer_parts = []
    reply_messages = None
    mirrored = False
    async for event in get_agent_event_stream(
        user_input=request.message,
        thread_id=request.thread_id,
        graph=_graph_instance,
        attachments=attachments,
        web=request.web,
        use_graph=request.graph,
        model=request.model,
        thinking=request.thinking,
        permission_mode=request.permission_mode,
        workdir_root=str(root) if root else "",
    ):
        # 只在桌面入口转发；微信 worker 直接进图，已有的微信回信不会再走一遍。
        if request.thread_id == ASSISTANT_MAIN_THREAD_ID:
            if event.get("type") == "token":
                answer_parts.append(str(event.get("content") or ""))
            elif event.get("type") == "assistant_messages":
                reply_messages = list(event.get("content") or [])
            elif event.get("type") == "done" and not mirrored:
                mirrored = True
                _, clean_answer = split_think("".join(answer_parts))
                answer = str(event.get("answer", clean_answer) or "")
                answer = "\n\n".join(split_assistant_messages(answer))
                try:
                    options = {"reply_messages": reply_messages} if reply_messages is not None else {}
                    wechat_channel.mirror_turn(request.thread_id, answer, **options)
                except Exception as exc:  # noqa: BLE001 - 渠道故障不影响桌面回答
                    logger.warning("微信对话同步启动失败: %s", exc)
        yield {"data": json.dumps(event, ensure_ascii=False)}


@router.post("")
async def chat(request: ChatRequest):
    """
    SSE 流式聊天

    Phase 1: 直接 RAG 问答
    Phase 2: LangGraph 多 Agent 编排（含工具调用/HITL/状态流转）

    ping=15：静默期心跳，防止代理按空闲超时掐断长等待（审批/首字延迟）。
    """
    return EventSourceResponse(
        _stream_exclusive(request),
        ping=15,
        headers={
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_exclusive(request: ChatRequest):
    """语音与键盘共用入口；跨窗口的忙碌提示不能代替服务端互斥。"""
    thread_id = request.thread_id
    # 检查和登记之间没有 await；在同一个事件循环中原子占用。
    if thread_id in _active_threads:
        yield {"data": json.dumps({"type": "error", "content": "这段对话还在处理上一条消息，请稍后再试。"}, ensure_ascii=False)}
        return
    _active_threads.add(thread_id)
    try:
        stream = _stream_graph(request) if _GRAPH_ENABLED and _graph_instance is not None else _stream_rag(request)
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()
    finally:
        _active_threads.discard(thread_id)
