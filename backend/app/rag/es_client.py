"""
Elasticsearch 客户端 —— 混合检索的「关键词召回」这一路（BM25）

★ 设计要点

1. **中文分词走入库侧预分词**（详见 `rag/segmenter.py`）：
   `text` 存原文（standard 分析器，用于精确短语匹配），
   `text_seg` 存 jieba 切分后的空格串（whitespace 分析器，承载 BM25 主信号）。

2. **文档 _id 与 Milvus 主键同源**：都来自 `core/doc_id.chunk_key()`，
   因此重复上传文件时 ES 侧是覆盖而非新增，两个存储始终一一对应。

3. **写入后必须 refresh**：默认 1 秒刷新周期下，「上传完立刻提问」会搜不到
   刚写入的文档 —— 与 Milvus 的 `consistency_level="Strong"` 是同一类坑。
   这里用 `refresh="wait_for"` 等待刷新完成，保证读己之写。

4. **可降级**：ES 不可用时不抛异常打断上传/问答流程，
   由上层（retriever）把 BM25 这一路视为空召回，退化为纯向量检索。
   —— 与 Milvus 缺失时退化为纯 LLM 是同一套容错哲学。

5. **单节点副本数为 0**：本地只有 1 个 ES 节点，若保留副本，
   分片无法分配，集群健康度会一直是 yellow。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from app.config import settings
from app.core.doc_id import chunk_key
from app.rag.segmenter import normalize_whitespace, segment_to_text

logger = logging.getLogger(__name__)

# BM25 调参：k1 控制词频饱和速度，b 控制文档长度归一化强度。
# 这两个值是 ES 默认值，也是绝大多数中文检索场景的稳健起点，
# 调优时需要 Golden Set 支撑，不要凭感觉改。
BM25_K1 = 1.2
BM25_B = 0.75

# 自定义分析器名（text_seg 用）
_ANALYZER_SEG = "xjms_seg"


class ESUnavailable(RuntimeError):
    """Elasticsearch 不可用（连接失败 / 索引缺失且无法创建）"""


class ESManager:
    """Elasticsearch 连接与索引管理器（单例）"""

    _instance: "ESManager | None" = None

    def __init__(self) -> None:
        self._client: Any = None
        self._unavailable_reason: str | None = None
        #: 冷却截止时间（`time.monotonic()`）。见 `_in_cooldown()` 的说明：
        #: ES 不可用时**不许每个请求都去重试一遍**，否则用户每问一句都要
        #: 陪我们等满一次连接重试（实测 5s）。
        self._unavailable_until: float = 0.0

    @classmethod
    def get_instance(cls) -> "ESManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- 连接 ----------
    @property
    def enabled(self) -> bool:
        return bool(settings.ES_ENABLED)

    @property
    def index_name(self) -> str:
        return settings.ES_INDEX

    def _build_client(self) -> Any:
        """构造异步客户端（延迟到首次使用，避免启动时因 ES 未就绪而失败）"""
        from elasticsearch import AsyncElasticsearch

        kwargs: Dict[str, Any] = {
            "hosts": [settings.ES_URL],
            "request_timeout": settings.ES_TIMEOUT_SECONDS,
            # 本地开发关闭校验；ES 关闭安全模块时不传证书也不会告警
            "verify_certs": False,
        }
        # 仅在显式配置了凭据时才带 Basic Auth：
        # 本地 ES 关掉了 xpack.security，带凭据没有意义且容易被误认为鉴权失败
        if settings.ES_USERNAME and settings.ES_PASSWORD:
            kwargs["basic_auth"] = (settings.ES_USERNAME, settings.ES_PASSWORD)

        return AsyncElasticsearch(**kwargs)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    @property
    def probe_client(self) -> Any:
        """
        「探测类」请求专用的客户端视图：**不重试 + 短超时**。

        ★ 为什么探测不能沿用主客户端的重试策略

        elastic_transport 默认在连接失败时重试 3 次并逐次退避 —— 这是给**正常查询**
        准备的（偶发网络抖动不该让一次检索失败），实测代价约 9 秒。
        但 `ensure_index()` 是**探测**：我们要的只是「现在到底通不通」这个是非题，
        重试 3 次只是把同一个「不通」的答案排练三遍，而那几十秒是用户在等。

        改成不重试 + `ES_PROBE_TIMEOUT_SECONDS` 后：
        「连接被拒」这类失败立刻返回，探测不再是延迟的来源；
        真正需要耐心的正常检索仍走主客户端（超时 `ES_TIMEOUT_SECONDS`）。
        """
        timeout = float(getattr(settings, "ES_PROBE_TIMEOUT_SECONDS", 2.0))
        return self.client.options(request_timeout=timeout, max_retries=0)

    async def ping(self) -> bool:
        """连通性探测（不抛异常）"""
        if not self.enabled:
            return False
        try:
            return bool(await self.probe_client.ping())
        except Exception as e:  # noqa: BLE001
            logger.debug("Elasticsearch ping 失败: %s", e)
            return False

    # ---------- 索引管理 ----------
    def _mapping(self) -> Dict[str, Any]:
        return {
            "settings": {
                "number_of_shards": 1,
                # 单节点开发环境：副本分片无法分配，保留副本会让健康度卡在 yellow
                "number_of_replicas": 0,
                "analysis": {
                    "analyzer": {
                        # text_seg 已是 jieba 切好的空格串，
                        # whitespace 不会再动中文，只做大小写归一
                        _ANALYZER_SEG: {
                            "type": "custom",
                            "tokenizer": "whitespace",
                            "filter": ["lowercase"],
                        }
                    }
                },
            },
            "mappings": {
                "properties": {
                    # 原文：standard 分析器。中文会按单字切分，
                    # 因此主要用于 match_phrase 做「项目编号 X-123」这类精确短语匹配
                    "text": {"type": "text", "analyzer": "standard"},
                    # jieba 预分词结果：BM25 主信号
                    "text_seg": {"type": "text", "analyzer": _ANALYZER_SEG},
                    "source": {"type": "keyword"},
                    "page": {"type": "integer"},
                    "chunk_index": {"type": "integer"},
                    # 与 Milvus 主键同源的整数标识，便于跨存储对齐与按文件删除
                    "doc_id": {"type": "long"},
                }
            },
        }

    def _in_cooldown(self) -> bool:
        """
        是否处于「ES 刚被判定不可用」的冷却期内。

        ★ 为什么需要它（不变量 14 的后半句：降级不仅要对，还要**快**）

        `ensure_index()` 每次都会真发一次 HEAD 请求，失败时 ES 客户端自身还要
        重试 3 次（含退避，实测约 9 秒）。改前它在失败后**不做任何记忆** ——
        于是用户每问一句，检索层都要先陪我们把这个注定失败的握手重来一遍。
        在检索那边的表现是：BM25 这一路每次都被 5 秒的预算切掉
        （`RETRIEVAL_LANE_TIMEOUT_SECONDS`），而这 5 秒里什么都不会发生。

        代价对比（ES 未启动，同一轮问答的检索耗时）：
            改前：约 5.1s（被单路预算切掉，白等）
            改后：约 0.1s（直接判定「这路这轮不用试了」）

        用**冷却期**而不是永久闩锁：ES 有可能在运行中被拉起来，
        闩死了就再也回不来（那是把「降级」变成了「永久停用」）。
        """
        return time.monotonic() < self._unavailable_until

    def mark_unavailable(self, reason: str) -> None:
        """
        记下不可用原因，并进入一段冷却期（期间不再发起连接尝试）。

        ⚠️ 有两个调用方，**缺一不可**：

        1. 本类内部：`ensure_index` / `search` / `mget` 等真实请求失败时。
        2. `retriever._guarded` 的**超时回调**：单路预算是用 `asyncio.wait_for`
           实现的，到点会取消协程 —— 于是上面第 1 条那个 `except` 根本来不及跑，
           冷却永远装不上。所以「超时」必须由超时的那一侧自己记（详见回调处的说明）。
        """
        self._unavailable_reason = reason
        cooldown = float(getattr(settings, "ES_UNAVAILABLE_COOLDOWN_SECONDS", 30.0))
        self._unavailable_until = time.monotonic() + max(0.0, cooldown)

    def _mark_available(self) -> None:
        """恢复：清掉不可用标记与冷却（ES 又回来了，别让它背着旧结论）"""
        self._unavailable_reason = None
        self._unavailable_until = 0.0

    async def ensure_index(self, force_recreate: bool = False) -> bool:
        """
        确保索引存在（含 mapping）。

        Args:
            force_recreate: 是否先删除再重建（换 embedding 模型或改 mapping 后用）

        Returns:
            索引是否可用
        """
        if not self.enabled:
            return False

        # 冷却期内直接判定不可用：不发请求（`force_recreate` 是显式的人工动作，
        # 绕过冷却 —— 用户就是要重建索引时，不该被上一轮的结论拦住）
        if not force_recreate and self._in_cooldown():
            return False

        try:
            index = self.index_name
            # `exists` 是探测：用不重试的短超时视图（见 `probe_client`）。
            # 建/删索引是真写操作，走主客户端（正常超时）。
            exists = bool(await self.probe_client.indices.exists(index=index))

            if exists and force_recreate:
                await self.client.indices.delete(index=index)
                exists = False
                logger.info("已删除 ES 索引 '%s'（force_recreate）", index)

            if not exists:
                await self.client.indices.create(index=index, **self._mapping())
                logger.info("✓ 已创建 ES 索引 '%s'（BM25 + jieba 预分词）", index)
            self._mark_available()
            return True
        except Exception as e:  # noqa: BLE001
            self.mark_unavailable(str(e))
            logger.warning(
                "ES 索引不可用（关键词召回将降级，%ds 内不再重试）: %s",
                int(getattr(settings, "ES_UNAVAILABLE_COOLDOWN_SECONDS", 30.0)), e,
            )
            return False

    async def delete_index(self) -> None:
        """删除索引（开发/调试用）"""
        if not self.enabled:
            return
        try:
            if await self.client.indices.exists(index=self.index_name):
                await self.client.indices.delete(index=self.index_name)
                logger.info("已删除 ES 索引 '%s'", self.index_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除 ES 索引失败: %s", e)

    # ---------- 写入 ----------
    async def index_chunks(
        self,
        chunks: List[Dict[str, Any]],
        *,
        wait_refresh: bool = True,
    ) -> int:
        """
        批量写入文档块（按确定性 _id 覆盖，重复入库幂等）。

        Args:
            chunks: [{"text", "source", "page", "chunk_index"}, ...]
            wait_refresh: 是否等待刷新完成（True 保证「写入完立刻能搜到」）

        Returns:
            成功写入条数；ES 不可用时返回 0（不抛异常）
        """
        if not chunks or not self.enabled:
            return 0

        if not await self.ensure_index():
            return 0

        from elasticsearch.helpers import async_bulk

        actions: List[Dict[str, Any]] = []
        for chunk in chunks:
            source = str(chunk.get("source", ""))
            chunk_index = int(chunk.get("chunk_index", 0))
            text = chunk.get("text", "") or ""
            key = chunk_key(source, chunk_index)

            actions.append({
                "_index": self.index_name,
                "_id": key,
                # text_seg 是 BM25 的实际检索字段，这里做一次空白归一
                "text": text,
                "text_seg": normalize_whitespace(segment_to_text(text)),
                "source": source,
                "page": int(chunk.get("page", 0) or 0),
                "chunk_index": chunk_index,
                "doc_id": int(key, 16),
            })

        try:
            success, failed = await async_bulk(
                self.client,
                actions,
                # 与 Milvus 的 Strong 一致性同理：不等刷新就会「写完搜不到」
                refresh="wait_for" if wait_refresh else False,
                raise_on_error=False,
                stats_only=False,
            )
            if failed:
                # failed 里是逐条错误详情，只打印前几条避免刷屏
                logger.warning("ES 写入部分失败: %s 条，示例 %s", len(failed), failed[:2])
            return int(success)
        except Exception as e:  # noqa: BLE001
            self.mark_unavailable(str(e))
            logger.warning("ES 批量写入失败（不影响向量入库）: %s", e)
            return 0

    # ---------- 检索 ----------
    def _build_query(self, query: str) -> Dict[str, Any]:
        """
        构造 BM25 查询。

        两路 should 相加：
        1. `text_seg`：jieba 切分后的 query 做 match —— 承载主要 BM25 信号，
           因为两侧词典一致，分词粒度对齐
        2. `text`：原始 query 做 match_phrase —— standard 分析器会把
           「X-123」切成 [x, 123]，因此能实现编号/英文串的精确相邻匹配，
           这是纯语义检索完全做不到的场景（也是引入 BM25 的核心动机）

        不设 minimum_should_match：单路命中也要保留（召回优先，后面还有 RRF 与精排）。
        """
        segmented = normalize_whitespace(segment_to_text(query))
        return {
            "bool": {
                "should": [
                    {
                        "match": {
                            "text_seg": {
                                "query": segmented or query,
                                "boost": settings.HYBRID_BM25_WEIGHT,
                            }
                        }
                    },
                    {
                        "match_phrase": {
                            "text": {
                                "query": query,
                                # 精确短语是补充信号，权重压低，避免压过词级 BM25
                                "boost": 0.5 * settings.HYBRID_BM25_WEIGHT,
                            }
                        }
                    },
                ]
            }
        }

    async def search(
        self,
        query: str,
        top_k: int = 20,
        *,
        source: str | None = None,
    ) -> List[Dict[str, Any]]:
        """
        BM25 关键词检索。

        Returns:
            [{"text", "source", "page", "chunk_index", "score", "retrieval"}, ...]
            score 为 BM25 原始分（不同 query 之间不可直接比较，仅用于排序）
            ES 不可用时返回空列表。
        """
        if not query or not query.strip() or not self.enabled:
            return []

        if not await self.ensure_index():
            return []

        try:
            query_body = self._build_query(query)
            if source is not None:
                query_body = {"bool": {"must": [query_body], "filter": [{"term": {"source": source}}]}}
            resp = await self.client.search(
                index=self.index_name,
                query=query_body,
                size=top_k,
                source=["text", "source", "page", "chunk_index", "doc_id"],
            )
        except Exception as e:  # noqa: BLE001
            self.mark_unavailable(str(e))
            logger.warning("ES 检索失败（降级为纯向量召回）: %s", e)
            return []

        hits: List[Dict[str, Any]] = []
        for hit in (resp.get("hits", {}) or {}).get("hits", []) or []:
            src = hit.get("_source", {}) or {}
            hits.append({
                "text": src.get("text", ""),
                "source": src.get("source", ""),
                "page": src.get("page", 0),
                "chunk_index": src.get("chunk_index", 0),
                "doc_id": src.get("doc_id"),
                "score": float(hit.get("_score") or 0.0),
                "retrieval": "bm25",
            })
        return hits

    # ---------- 维护 / 观测 ----------
    async def mget_by_keys(self, refs: List[tuple[str, int]], *, strict: bool = False) -> List[Dict[str, Any]]:
        """
        按 `(source, chunk_index)` 批量取回文档（图路召回的正文回填兜底）。

        ★ 为什么 ES 侧也要有这个方法

        图路召回的正文优先从 Milvus 取（那是向量的权威来源），但 Milvus 可能
        不可用而 ES 还在。两级回退让「图检索」这个能力不因为向量库挂掉而
        连带失效 —— 与检索层「宁可少一路，不可整条挂」是同一个原则。

        用 `mget` 而不是 `search`：这里已经知道确切的主键了，
        走检索会多一次打分与排序，且可能因为分片路由拿到不完整的结果。

        Returns:
            [{"doc_id", "text", "source", "page", "chunk_index"}, ...]
            ES 不可用或出错时返回空列表（降级，不抛异常）
        """
        if not refs or not self.enabled:
            return []
        if not await self.ensure_index():
            if strict:
                raise ESUnavailable(self._unavailable_reason or "关键词索引不可用")
            return []

        keys = [chunk_key(source, index) for source, index in refs]
        try:
            resp = await self.client.mget(
                index=self.index_name,
                ids=keys,
                source=["text", "source", "page", "chunk_index", "doc_id"],
            )
        except Exception as e:  # noqa: BLE001
            self.mark_unavailable(str(e))
            if strict:
                raise
            logger.warning("ES 按主键取回失败（图路该块将被丢弃）: %s", e)
            return []

        out: List[Dict[str, Any]] = []
        for doc in (resp.get("docs") or []):
            if not doc.get("found"):
                continue
            src = doc.get("_source", {}) or {}
            out.append({
                "doc_id": src.get("doc_id"),
                "text": src.get("text", ""),
                "source": src.get("source", ""),
                "page": src.get("page", 0),
                "chunk_index": src.get("chunk_index", 0),
            })
        return out

    async def count(self, *, strict: bool = False) -> int:
        """索引内文档数（健康检查 / 演示用）"""
        if not self.enabled:
            return 0
        try:
            if not await self.client.indices.exists(index=self.index_name):
                return 0
            resp = await self.client.count(index=self.index_name)
            return int(resp.get("count", 0))
        except Exception as e:  # noqa: BLE001
            if strict:
                raise
            logger.debug("ES 统计文档数失败: %s", e)
            return 0

    async def health(self) -> Dict[str, Any]:
        """健康状态摘要（供 /api/health 展示）"""
        if not self.enabled:
            return {"status": "disabled", "index": self.index_name, "docs": 0}

        if not await self.ping():
            return {
                "status": "down",
                "index": self.index_name,
                "docs": 0,
                "reason": self._unavailable_reason or "ping 失败",
            }

        try:
            info = await self.client.cluster.health()
            return {
                "status": "up",
                "cluster_status": info.get("status"),
                "index": self.index_name,
                "docs": await self.count(),
            }
        except Exception as e:  # noqa: BLE001
            return {"status": "degraded", "index": self.index_name, "reason": str(e)}

    async def close(self) -> None:
        """关闭连接（应用退出时调用）"""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ---------- 按来源删除（增量索引的前置能力） ----------
    async def delete_by_source(self, source: str, *, from_chunk: int | None = None, strict: bool = False) -> int:
        """
        删除某个来源文件的全部文档（增量索引「改动/删除」时调用）。

        `source` 在 mapping 里是 `keyword`，所以用 `term` 精确匹配 ——
        不能用 `match`：那会做分词，文件名 "报告 v2.md" 可能顺带命中 "报告 v3.md"。

        Returns:
            删除条数。strict=True 时失败上抛，管理接口不会把故障当成成功。
            from_chunk 仅清理这个序号及之后的旧尾块。
        """
        if not source or not self.enabled:
            return 0
        if not await self.ensure_index():
            if strict:
                raise ESUnavailable(self._unavailable_reason or "关键词索引不可用，未确认删除")
            return 0
        try:
            query: Dict[str, Any] = {"term": {"source": source}}
            if from_chunk is not None:
                query = {"bool": {"filter": [query, {"range": {"chunk_index": {"gte": from_chunk}}}]}}
            resp = await self.client.delete_by_query(
                index=self.index_name,
                query=query,
                # 与写入同理：不等刷新的话「删完立刻检索」还能搜到旧文档
                refresh=True,
                conflicts="proceed",
            )
            if resp.get("failures") or resp.get("timed_out") or resp.get("version_conflicts"):
                raise RuntimeError("关键词索引未完成删除，请重试")
            deleted = int((resp or {}).get("deleted", 0) or 0)
            if deleted:
                logger.info("ES 已删除来源 '%s' 的 %s 个块", source, deleted)
            return deleted
        except Exception as e:  # noqa: BLE001
            self.mark_unavailable(str(e))
            if strict:
                raise
            logger.warning("ES 按来源删除失败（source=%s）: %s", source, e)
            return 0


# 全局单例
es_manager = ESManager.get_instance()


__all__ = ["ESManager", "ESUnavailable", "es_manager", "BM25_K1", "BM25_B"]
