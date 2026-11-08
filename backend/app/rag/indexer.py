"""Document ingestion with bounded staging and explicit partial-success reporting."""

import asyncio
import logging
import pickle
import tempfile
from dataclasses import dataclass, field

from app.core.embedding_factory import EmbeddingError, get_embeddings
from app.core.milvus_client import milvus_manager
from app.rag.chunker import split_text
from app.rag.es_client import es_manager
from app.rag.parser import parse_file

logger = logging.getLogger(__name__)
INDEX_BATCH_SIZE = 200


@dataclass
class IndexResult:
    chunks: int
    keyword_indexed: bool | None = None
    warnings: list[str] = field(default_factory=list)


async def index_document(
    file_path: str, filename: str = "", *, sync_keyword_index: bool = True,
) -> IndexResult:
    """Prepare all embeddings before writing; remove obsolete tail chunks afterward.

    The staging file spills to disk after 8 MiB. Parsing/embedding failures leave
    the existing index intact. A storage failure during commit is reported to the
    caller; this is not a transaction across Milvus and Elasticsearch.
    """
    text = await parse_file(file_path, filename)
    if not text or not text.strip():
        raise ValueError("未解析出可检索的文字；扫描版 PDF 请先进行文字识别")
    chunks = await asyncio.to_thread(split_text, text)
    if not chunks:
        raise ValueError("文档没有可入库的文本块")
    # Fail before paid embedding work if the store or its model space is invalid.
    await asyncio.to_thread(milvus_manager.ensure_collection)

    result = IndexResult(chunks=len(chunks))
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as staged:
        for start in range(0, len(chunks), INDEX_BATCH_SIZE):
            batch = chunks[start:start + INDEX_BATCH_SIZE]
            vectors = await get_embeddings(batch)
            if len(vectors) != len(batch) or any(not vector for vector in vectors):
                raise EmbeddingError(f"向量化结果不完整：需要 {len(batch)} 条，收到 {len(vectors)} 条")
            metadata = [
                {"text": chunk, "source": filename, "page": 0, "chunk_index": start + i}
                for i, chunk in enumerate(batch)
            ]
            pickle.dump((vectors, metadata), staged)

        # Only data written by this function is deserialized from this private file.
        staged.seek(0)
        keyword_written = 0
        for _ in range(0, len(chunks), INDEX_BATCH_SIZE):
            vectors, metadata = pickle.load(staged)
            written = await milvus_manager.upsert(vectors, metadata)
            if written != len(metadata):
                raise RuntimeError(f"向量写入不完整：需要 {len(metadata)} 块，写入 {written} 块；请重试")
            if sync_keyword_index and es_manager.enabled:
                try:
                    keyword_written += await es_manager.index_chunks(metadata)
                except Exception as exc:
                    logger.warning("关键词索引写入失败：%s", exc)

        # Upsert alone leaves old chunks when a document becomes shorter.
        await asyncio.to_thread(
            milvus_manager.delete_by_source, filename, from_chunk=len(chunks), strict=True,
        )
        if sync_keyword_index and es_manager.enabled:
            result.keyword_indexed = keyword_written == len(chunks)
            if result.keyword_indexed:
                try:
                    await es_manager.delete_by_source(filename, from_chunk=len(chunks), strict=True)
                except Exception as exc:
                    result.keyword_indexed = False
                    logger.warning("关键词旧块清理失败：%s", exc)
            if not result.keyword_indexed:
                result.warnings.append("向量索引已完成，关键词索引未完全同步，可重新索引修复")

    logger.info("文档 %s 入库完成：%s 块", filename, result.chunks)
    return result


async def index_file(file_path: str, filename: str = "", *, sync_keyword_index: bool = True) -> int:
    """Compatibility entry point for scripts that only need the chunk count."""
    result = await index_document(file_path, filename, sync_keyword_index=sync_keyword_index)
    return result.chunks


__all__ = ["index_file", "index_document", "IndexResult", "INDEX_BATCH_SIZE"]
