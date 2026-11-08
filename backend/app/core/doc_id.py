"""
文档块确定性主键 —— 向量库与关键词库共用的唯一标识

★ 为什么把主键派生逻辑单独抽出来

Phase 1 只有 Milvus 一个存储，主键派生写在 `milvus_client._chunk_id` 里就够了。
Phase 3 引入 Elasticsearch 之后，同一条 chunk 会同时写进两个存储，
此时两边必须使用**同一个标识**，否则：

- 重复上传文件时，Milvus 靠 upsert 覆盖、ES 却新增文档 → ES 里堆出重复块；
- 检索结果无法跨存储去重，RRF 融合会把同一条内容当成两条独立结果各投一票；
- 想按文件删除向量时，无法同步清理 ES 中的对应文档。

因此这里把「确定性主键」上升为跨存储的架构不变量：

    key = sha1("{source}::{chunk_index}")[:15]

- 取 sha1 前 15 个十六进制字符（60 bit）：远小于 INT64 上限，碰撞概率可忽略
- `chunk_key()` 返回字符串，可直接作为 Elasticsearch 的 `_id`
- `chunk_id()` 返回整型，作为 Milvus 的 INT64 主键

⚠️ 不要改成随机 UUID 或自增主键，否则「重复上传同一文件不产生重复块」这条性质会失效。
"""

from __future__ import annotations

import hashlib

# 主键摘要长度（十六进制字符数）
_KEY_LENGTH = 15


def chunk_key(source: str, chunk_index: int) -> str:
    """
    派生文档块的确定性字符串主键。

    Args:
        source: 来源标识（通常是文件名）
        chunk_index: 该文件内的块序号

    Returns:
        15 个十六进制字符，例如 "a1b2c3d4e5f6071"
    """
    raw = f"{source}::{chunk_index}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:_KEY_LENGTH]


def chunk_id(source: str, chunk_index: int) -> int:
    """
    派生 Milvus 使用的 INT64 主键（同一摘要的整数形式）。

    与 `chunk_key()` 同源，保证两个存储的标识一一对应。
    """
    return int(chunk_key(source, chunk_index), 16)


__all__ = ["chunk_key", "chunk_id"]
