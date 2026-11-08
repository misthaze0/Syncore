"""
文本分块器 - 使用 RecursiveCharacterTextSplitter
优先按 段落 → 句子 → 词 分割
"""

from typing import List


def split_text(
    text: str,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> List[str]:
    """
    将文本分块

    Args:
        text: 原始文本
        chunk_size: 每块最大字符数
        chunk_overlap: 块之间的重叠字符数

    Returns:
        分块后的文本列表
    """
    if not text or not text.strip():
        return []

    # 惰性导入：`import langchain_text_splitters` 的包 __init__ 会急切拉起
    # sentence_transformers → torch（本机冷导入实测约 10s）。它本只在入库/分块
    # 时用到，却在桌面端启动链（app.main → api.indexing → rag.indexer → chunker）
    # 上被无条件触发。挪进函数体后，启动不再为分块付这笔导入税，第一次真正分块时才加载。
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", ".", "！", "？", "；", ";", " ", ""],
        length_function=len,
    )

    chunks = splitter.split_text(text)
    return [c.strip() for c in chunks if c.strip()]
