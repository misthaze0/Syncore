"""
图谱存储层 —— Neo4j 的连接、写入、按来源删除与遍历查询

★ 命名空间为什么绑定 `settings.MILVUS_COLLECTION`

图谱必须和向量库、BM25 索引**看到同一批数据**，否则三路召回的候选池不可比，
RRF 融合就失去意义。而"同一批数据"这件事在本项目里已经有一个唯一标识了：
`MILVUS_COLLECTION`（评估时被 `runner.py` 临时改成 `_eval_probe`）。

把这个标识复用到图的 `space` 属性上，带来三个好处：
1. **评估探针的隔离自动延伸到图** —— 评估脚本只改一处配置，
   图、向量、BM25 三边同时切到探针空间，收尾时同时清理，不需要额外开关。
2. 删掉一个 Collection 就等于"这份知识库到此为止"，语义一致。
3. 不会出现"图里还留着上一个语料的实体"这种最难排查的串台
   （换语料后旧实体仍在，查询会召回一批谁也没见过的块）。

★ 降级策略

Neo4j 是**第三路可选召回**，不是硬依赖：连不上时 `GraphStore` 标记为不可用、
所有查询返回空，混合检索自然退化为向量 + BM25 两路。
与 Milvus / ES / Redis 的容错哲学一致 —— 只有 LLM 是硬依赖。

但**能力状态必须可见**（`status()`），不能静默退化：
「图路候选恒为 0」和「图是空的」和「Neo4j 挂了」是三种完全不同的故障，
只有如实上报才能区分。

★ 为什么删除要连实体一起清

`MERGE` 的主键包含了 `source` + `chunk_index`（保证每块文档的每条关系独立留痕）。
代价是：文件内容一变，新边会以新主键建出来，**旧边不会自动消失** ——
这和不变量 24 的"孤儿块"是同一类 bug，只不过发生在图里。

所以 `delete_by_source()` 必须：
1. 删掉该来源的所有 `Chunk` 节点（DETACH，连带 MENTIONED_IN 边）
2. 删掉所有 `source` 属性指向它的 `RELATED` 边
3. 清掉因此变成孤立的实体节点（既没被任何块提到、也没有任何关系）

第 3 步不能省：残留的孤立实体虽然查不出内容，却会参与"查询命中实体"的匹配，
让图路返回一批指向已删除内容的空引用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.graph.models import GraphStats, Triple

logger = logging.getLogger(__name__)

#: 实体名索引的缓存时长（秒）。查询期匹配需要“图里有哪些实体”这份清单，
#: 每次都去查 Neo4j 会让每轮问答多一次往返；缓存 60 秒足够，
#: 代价是刚构建完图的一分钟内可能匹配不到新实体（可接受）。
_ENTITY_CACHE_TTL = 60.0


class GraphStore:
    """Neo4j 图存储（单例，惰性连接，全链路可降级）"""

    _instance: "GraphStore | None" = None

    def __init__(self) -> None:
        self._driver: Any = None
        self._loop: Any = None
        self._unavailable_reason: Optional[str] = None
        #: 冷却截止时间（`time.monotonic()`）。见 `_in_cooldown()` 的说明。
        self._unavailable_until: float = 0.0
        self._index_ready = False
        #: 实体名索引缓存：(过期时间戳, {归一化键: 展示名})
        self._entity_cache: Tuple[float, Dict[str, str]] = (0.0, {})
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "GraphStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- 命名空间 ----------
    @property
    def space(self) -> str:
        """
        当前图的命名空间 = 当前向量集合名。

        ⚠️ 每次访问都重新读 settings，**不要缓存成实例属性** ——
        评估脚本会在运行中切换 `MILVUS_COLLECTION`，缓存了就切不过去，
        表现为"评估把生产图谱当成探针一起删了"。
        """
        return settings.MILVUS_COLLECTION

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "GRAPH_ENABLED", False))

    # ---------- 连接 ----------
    @property
    def available(self) -> bool:
        return self._driver is not None and self._unavailable_reason is None

    @property
    def degraded_reason(self) -> Optional[str]:
        return self._unavailable_reason

    def _build_driver(self) -> Any:
        from neo4j import AsyncGraphDatabase

        return AsyncGraphDatabase.driver(
            settings.GRAPH_URI,
            auth=(settings.GRAPH_USER, settings.GRAPH_PASSWORD),
            connection_timeout=settings.GRAPH_TIMEOUT_SECONDS,
            max_connection_lifetime=3600,
        )

    def _in_cooldown(self) -> bool:
        """
        是否处于「Neo4j 刚被判定不可用」的冷却期内。

        ★ 为什么需要它（与 `ESManager._in_cooldown` 是同一类问题，不变量 14）

        `_ensure_driver` 失败时会 `_drop_driver()`（把句柄丢成 None）并记下原因。
        但**下一个请求进来时 `self._driver is None` 又成立**，于是重新
        `_build_driver()` + `verify_connectivity()` —— 一次完整的连接尝试，
        带 `GRAPH_TIMEOUT_SECONDS`（默认 5s）的超时。

        结果是：Neo4j 没起来时，**每一轮问答**都要在图这一路上白等 5 秒，
        而图路本来只是可选增益（三路里的一路）。实测端到端 `sources` 事件
        因此被推到 5.05s（本应约 2.7s）。

        真相不是「Neo4j 挂了」，而是「我们每轮都去敲一次已知敲不开的门」。
        """
        return time.monotonic() < self._unavailable_until

    def mark_unavailable(self, reason: str) -> None:
        """
        记下不可用原因，并进入一段冷却期（期间不再尝试建连）。

        ⚠️ 有两个调用方，**缺一不可**：

        1. 本类内部：`_ensure_driver` 建连失败时。
        2. `retriever._guarded` 的**超时回调**：单路预算是用 `asyncio.wait_for`
           实现的，到点会取消建连协程 —— 于是上面第 1 条那个 `except` 来不及跑，
           冷却装不上，下一轮又会重建重连。
        """
        self._unavailable_reason = reason
        cooldown = float(getattr(settings, "GRAPH_UNAVAILABLE_COOLDOWN_SECONDS", 30.0))
        self._unavailable_until = time.monotonic() + max(0.0, cooldown)

    async def _ensure_driver(self) -> Any:
        """
        确保驱动可用。

        ⚠️ neo4j 的异步驱动**绑定创建它的 event loop**。调用方换 loop 后
        复用旧驱动会抛 "attached to a different loop"。
        所以这里记录创建时的 loop，发现不一致就重建（旧驱动顺手关掉）。
        """
        if not self.enabled:
            self._unavailable_reason = "未启用（GRAPH_ENABLED=false）"
            return None

        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # 换了一个 event loop（测试套件里每个用例一个）：上一个 loop 的结论
            # ——驱动句柄与「不可用」冷却——都不再适用，全部从头算。
            # ⚠️ 冷却必须一起清：`graph_store` 是单例，用例 A 把 Neo4j 判定为不可用后
            # 留下的冷却，会让用例 B（新 loop）**一次都不尝试**就拿到降级结果。
            if self._driver is not None:
                await self._drop_driver()
            self._unavailable_reason = None
            self._unavailable_until = 0.0
        self._loop = loop

        # 冷却期内不再尝试建连：直接按不可用降级（图路返回空）。
        # 放在 loop 处理之后 —— 换 loop 相当于换了一个运行环境，冷却从头算。
        if self._driver is None and self._in_cooldown():
            return None

        if self._driver is None:
            try:
                self._driver = self._build_driver()
                await asyncio.wait_for(
                    self._driver.verify_connectivity(),
                    timeout=settings.GRAPH_TIMEOUT_SECONDS,
                )
                self._unavailable_reason = None
                self._unavailable_until = 0.0
                logger.info("✓ Neo4j 已连接: %s (database=%s)",
                            settings.GRAPH_URI, settings.GRAPH_DATABASE)
            except Exception as e:  # noqa: BLE001
                self.mark_unavailable(f"{type(e).__name__}: {e}")
                await self._drop_driver()
                logger.warning(
                    "✗ Neo4j 不可用，图检索这一路将返回空（%ds 内不再重试）: %s",
                    int(getattr(settings, "GRAPH_UNAVAILABLE_COOLDOWN_SECONDS", 30.0)), e,
                )
                return None

        if not self._index_ready:
            await self._ensure_schema()

        # ★ 走到这里说明「有一个可用的驱动句柄」，因此必须把上一次的失败原因清掉。
        #
        # 不清的话会有两种谎报：① 上一轮查询失败把原因写进去了，这一轮恢复了
        # 但原因还挂着；② 更隐蔽的一种 —— `GRAPH_ENABLED` 被临时关掉走过一次
        # `_ensure_driver`（它会写下“未启用”），之后重新打开时 `_driver`
        # 还在，于是直接跳过建连分支，那句“未启用”就永久留在 `available` 上。
        # 这条路径正好是评估/验收脚本会走的（它们会临时切开关）。
        self._unavailable_reason = None
        return self._driver

    async def _drop_driver(self) -> None:
        driver, self._driver = self._driver, None
        self._index_ready = False
        self._entity_cache = (0.0, {})
        if driver is not None:
            try:
                await driver.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        await self._drop_driver()
        # 显式关闭是「我确定要重来」：清掉冷却，下次调用重新尝试建连
        self._unavailable_until = 0.0
        self._unavailable_reason = None

    async def _ensure_schema(self) -> None:
        """建立索引（幂等）。失败只告警 —— 没有索引图仍然可用，只是慢。"""
        statements = [
            "CREATE INDEX xjms_entity_key IF NOT EXISTS FOR (n:Entity) ON (n.space, n.key)",
            "CREATE INDEX xjms_chunk_key IF NOT EXISTS FOR (n:Chunk) ON (n.space, n.key)",
            "CREATE INDEX xjms_rel_source IF NOT EXISTS FOR ()-[r:RELATED]-() ON (r.space, r.source)",
        ]
        try:
            async with self._driver.session(database=settings.GRAPH_DATABASE) as session:
                for stmt in statements:
                    await session.run(stmt)
            self._index_ready = True
        except Exception as e:  # noqa: BLE001
            logger.debug("Neo4j 索引创建失败（不影响功能，仅影响遍历速度）: %s", e)

    async def _run(self, cypher: str, **params: Any) -> List[Dict[str, Any]]:
        """
        执行一条 Cypher 并取回全部记录。

        任何异常都转成"空结果 + 记录降级原因"，调用方不必 try/except ——
        图路是增益项，它出问题不该让一次问答失败。
        """
        driver = await self._ensure_driver()
        if driver is None:
            return []
        try:
            async with driver.session(database=settings.GRAPH_DATABASE) as session:
                result = await session.run(cypher, **params)
                rows = [dict(record) async for record in result]
                self._unavailable_reason = None
                return rows
        except Exception as e:  # noqa: BLE001
            self._unavailable_reason = f"{type(e).__name__}: {e}"
            logger.warning("Neo4j 查询失败（图路本轮返回空）: %s", e)
            return []

    # ---------- 写入 ----------
    UPSERT_CYPHER = """
    UNWIND $rows AS row
    MERGE (h:Entity {space: $space, key: row.head_key})
      ON CREATE SET h.name = row.head, h.type = row.head_type
      ON MATCH  SET h.type = coalesce(h.type, row.head_type)
    MERGE (t:Entity {space: $space, key: row.tail_key})
      ON CREATE SET t.name = row.tail, t.type = row.tail_type
      ON MATCH  SET t.type = coalesce(t.type, row.tail_type)
    MERGE (c:Chunk {space: $space, key: row.source + '#' + toString(row.chunk_index)})
      ON CREATE SET c.source = row.source, c.chunk_index = row.chunk_index
    MERGE (h)-[r:RELATED {space: $space, relation: row.relation,
                          source: row.source, chunk_index: row.chunk_index}]->(t)
      ON CREATE SET r.confidence = row.confidence, r.created = timestamp()
      ON MATCH  SET r.confidence = row.confidence, r.updated = timestamp()
    MERGE (h)-[:MENTIONED_IN]->(c)
    MERGE (t)-[:MENTIONED_IN]->(c)
    RETURN count(r) AS written
    """

    async def upsert_triples(
        self,
        triples: Sequence[Triple],
        *,
        batch_size: Optional[int] = None,
    ) -> int:
        """
        批量写入三元组（MERGE，幂等）。

        Returns:
            写入条数；Neo4j 不可用时返回 0（降级，不抛异常）
        """
        rows = [
            t.to_row()
            for t in triples
            if isinstance(t, Triple) and t.valid and t.source
        ]
        if not rows:
            return 0

        size = batch_size or max(1, int(getattr(settings, "GRAPH_BUILD_BATCH_SIZE", 200)))
        written = 0
        for start in range(0, len(rows), size):
            batch = rows[start : start + size]
            result = await self._run(self.UPSERT_CYPHER, rows=batch, space=self.space)
            if result:
                written += int(result[0].get("written", 0) or 0)
        if written:
            # 新实体进来了，实体索引缓存必须失效，否则查询期匹配不到它们
            self._entity_cache = (0.0, {})
            logger.info("图谱写入完成: %s 条关系 → space=%s", written, self.space)
        return written

    # ---------- 删除 ----------
    DELETE_SOURCE_CYPHER = """
    MATCH (c:Chunk {space: $space, source: $source})
    DETACH DELETE c
    """

    DELETE_SOURCE_REL_CYPHER = """
    MATCH ()-[r:RELATED {space: $space, source: $source}]->()
    DELETE r
    RETURN count(r) AS removed
    """

    DELETE_ORPHAN_ENTITIES_CYPHER = """
    MATCH (e:Entity {space: $space})
    WHERE NOT (e)--()
    DELETE e
    RETURN count(e) AS removed
    """

    async def delete_by_source(self, source: str, *, strict: bool = False) -> int:
        """
        删除某个来源在图里的全部痕迹（关系 + 块节点 + 因此孤立的实体）。

        增量索引的 `modified` / `deleted` 必须调用它 —— 否则改短或改写的文档
        会在图里留下"指向旧内容的边"，症状与孤儿块完全一致。
        """
        if not source:
            return 0
        await self._run(self.DELETE_SOURCE_CYPHER, space=self.space, source=source)
        if strict and self.degraded_reason:
            raise RuntimeError(f"关联图谱未完成清理：{self.degraded_reason}")
        result = await self._run(
            self.DELETE_SOURCE_REL_CYPHER, space=self.space, source=source
        )
        removed = int(result[0].get("removed", 0) or 0) if result else 0

        orphans = await self._run(self.DELETE_ORPHAN_ENTITIES_CYPHER, space=self.space)
        orphan_count = int(orphans[0].get("removed", 0) or 0) if orphans else 0
        if strict and self.degraded_reason:
            raise RuntimeError(f"关联图谱未完成清理：{self.degraded_reason}")

        self._entity_cache = (0.0, {})
        if removed or orphan_count:
            logger.info(
                "图谱已清除来源 '%s'：%s 条关系 / %s 个孤立实体", source, removed, orphan_count
            )
        return removed

    async def clear_space(self) -> int:
        """清空整个命名空间（评估探针收尾 / 重建前使用）"""
        result = await self._run(
            """
            MATCH (n {space: $space})
            DETACH DELETE n
            RETURN count(n) AS removed
            """,
            space=self.space,
        )
        removed = int(result[0].get("removed", 0) or 0) if result else 0
        self._entity_cache = (0.0, {})
        if removed:
            logger.info("图谱已清空 space=%s（%s 个节点）", self.space, removed)
        return removed

    # ---------- 查询 ----------
    async def stats(self) -> GraphStats:
        """
        图规模快照。

        ⚠️ 刻意拆成几条简单查询，而不是用 `CALL {}` 子查询凑成一个 ——
        Neo4j 5.23+ 会把不带变量作用域的 `CALL {}` 判为弃用并**在每条查询上
        打一条通知**，而这些通知会经驱动日志刷屏。慢一点无所谓
        （stats 只在状态接口与启动日志里调），日志干净更重要。
        """
        rows = await self._run(
            "MATCH (e:Entity {space: $space}) RETURN count(e) AS entities",
            space=self.space,
        )
        entities = int(rows[0].get("entities", 0) or 0) if rows else 0

        rows = await self._run(
            "MATCH ()-[r:RELATED {space: $space}]->() RETURN count(r) AS relations",
            space=self.space,
        )
        relations = int(rows[0].get("relations", 0) or 0) if rows else 0

        rows = await self._run(
            "MATCH (c:Chunk {space: $space}) RETURN count(c) AS chunks",
            space=self.space,
        )
        chunks = int(rows[0].get("chunks", 0) or 0) if rows else 0

        rows = await self._run(
            """
            MATCH (e:Entity {space: $space})
            RETURN coalesce(e.type, '其他') AS type, count(e) AS n
            """,
            space=self.space,
        )
        types = {
            str(row.get("type") or "其他"): int(row.get("n", 0) or 0)
            for row in rows
            if row.get("type") is not None
        }

        return GraphStats(
            space=self.space,
            entities=entities,
            relations=relations,
            chunks=chunks,
            types=types,
        )

    async def entity_index(self, *, force: bool = False) -> Dict[str, str]:
        """
        返回 `{归一化键: 展示名}`，供查询期做确定性实体匹配。

        带 TTL 缓存：每轮问答都去拉一次全量实体名是 O(实体数) 的往返，
        而实体集合的变化只发生在构建/删除之后（低频）。
        """
        now = time.time()
        expires, cached = self._entity_cache
        if not force and cached and now < expires:
            return cached

        rows = await self._run(
            """
            MATCH (e:Entity {space: $space})
            RETURN e.key AS key, e.name AS name
            """,
            space=self.space,
        )
        index = {
            str(r.get("key")): str(r.get("name") or r.get("key"))
            for r in rows
            if r.get("key")
        }
        self._entity_cache = (now + _ENTITY_CACHE_TTL, index)
        return index

    async def neighbors(self, keys: Sequence[str]) -> List[Dict[str, Any]]:
        """
        取这批实体的一跳邻居（双向），并带上每条边来自哪一块文档。

        只做一跳，多跳由 `retriever` 在 Python 侧迭代 —— 这样每一跳的
        来源块与路径都能被显式记录（Cypher 的可变长路径拿得到路径，
        但拿不到"每条边各自的来源块"与跳数对应关系，那正是界面要展示的东西）。
        """
        if not keys:
            return []
        return await self._run(
            """
            MATCH (a:Entity {space: $space})-[r:RELATED]-(b:Entity {space: $space})
            WHERE a.key IN $keys
            RETURN a.key AS a_key, a.name AS a_name,
                   b.key AS b_key, b.name AS b_name,
                   r.relation AS relation,
                   r.source AS source, r.chunk_index AS chunk_index,
                   r.confidence AS confidence
            """,
            space=self.space,
            keys=list(keys),
        )

    async def chunks_of_entities(self, keys: Sequence[str]) -> List[Dict[str, Any]]:
        """这些实体被哪些块提到过（块级 provenance）"""
        if not keys:
            return []
        return await self._run(
            """
            MATCH (e:Entity {space: $space})-[:MENTIONED_IN]->(c:Chunk {space: $space})
            WHERE e.key IN $keys
            RETURN c.source AS source, c.chunk_index AS chunk_index,
                   collect(DISTINCT e.key) AS entity_keys
            """,
            space=self.space,
            keys=list(keys),
        )

    # ---------- 观测 ----------
    async def status(self) -> Dict[str, Any]:
        """能力状态（供 /api/graph/status 与启动日志）"""
        info: Dict[str, Any] = {
            "enabled": self.enabled,
            "available": False,
            "uri": settings.GRAPH_URI if self.enabled else None,
            "database": settings.GRAPH_DATABASE if self.enabled else None,
            "space": self.space,
            "max_hops": getattr(settings, "GRAPH_MAX_HOPS", 2),
            "degraded_reason": self._unavailable_reason,
        }
        if not self.enabled:
            return info
        driver = await self._ensure_driver()
        if driver is None:
            info["degraded_reason"] = self._unavailable_reason
            return info
        stats = await self.stats()
        info["available"] = True
        info["stats"] = stats.to_dict()
        info["degraded_reason"] = None
        return info


# 全局单例
graph_store = GraphStore.get_instance()


__all__ = ["GraphStore", "graph_store"]
