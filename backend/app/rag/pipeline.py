"""
RAG 完整管道 - 检索 → 构造 Prompt → LLM 流式输出
Phase 1 单 Agent 问答入口
"""

import logging
from typing import Any, AsyncGenerator, Dict

from app.core.llm_factory import create_llm
from app.core.prompts import RAG_SYSTEM_PROMPT
from app.rag.retriever import retrieve, serialize_sources

logger = logging.getLogger(__name__)


async def rag_answer_with_sources(
    query: str,
    top_k: int = 5,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    RAG 问答流式生成器

    按顺序 yield 不同类型的 SSE 事件:
    1. {"type": "sources", "content": [...]}  - 检索到的来源文档
    2. {"type": "token", "content": "字"}    - LLM 流式 token
    3. {"type": "done", "content": ""}       - 回答结束

    Args:
        query: 用户问题
        top_k: 检索文档数量

    Yields:
        SSE 事件字典
    """
    # 1. 检索
    #    检索失败（Milvus/ES 未启动、Embedding 服务不可用）时降级为纯 LLM 回答，
    #    而不是让整个对话失败 —— 保证服务可用性优先。
    try:
        docs = await retrieve(query, top_k=top_k)
    except Exception as e:  # noqa: BLE001
        logger.warning("检索失败，降级为纯 LLM 回答: %s", e)
        docs = []

    # 2. 推送来源（Phase 3 起附带召回方式与精排分数）
    yield {"type": "sources", "content": serialize_sources(docs)}

    # 3. 构造上下文
    if docs:
        context_parts = []
        for i, doc in enumerate(docs):
            context_parts.append(
                f"[文档{i + 1}] (来源: {doc['source']})\n{doc['text']}"
            )
        context = "\n\n---\n\n".join(context_parts)
    else:
        context = "（未检索到相关文档）"

    # 4. 构造 prompt
    system_prompt = RAG_SYSTEM_PROMPT.format(context=context)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]

    # 5. LLM 流式输出
    llm = create_llm()
    try:
        async for chunk in llm.astream(messages):
            token = chunk.content
            if token:
                yield {"type": "token", "content": token}
    except Exception as e:
        logger.error(f"LLM 流式输出错误: {e}")
        yield {"type": "error", "content": f"LLM 调用失败: {str(e)}"}
        return

    # 6. 结束
    yield {"type": "done", "content": ""}
