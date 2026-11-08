"""
Milvus 客户端 - 连接管理 + Collection 初始化 + 向量增删查

设计要点（踩坑记录）:
1. pymilvus 3.x 的快速建表 `create_collection(dimension=...)` 默认 auto_id=False，
   即主键 `id` 必须由调用方提供，否则 insert 直接抛
   `Insert missed an field 'id' to collection without set nullable==true or set default_value`。
   → 这里显式声明 schema，并用「source + chunk_index」派生确定性主键。
2. 用 `upsert` 而非 `insert`：同一文件重复上传时按主键覆盖，
   不会在库里堆出重复的 chunk（否则检索结果会出现重复片段）。
3. 检索必须带 `consistency_level="Strong"`：默认 Bounded 一致性下，
   刚 upsert 完立刻检索可能查不到（做「上传后马上提问」的演示时非常容易被坑）。

Phase 3 起主键派生逻辑上移到 `core/doc_id.py`，因为 Elasticsearch 侧
必须使用同一个标识才能保证「重复上传不产生重复内容」在两个存储上都成立。

4. **Collection 自带「产出模型」登记**（description 字段）：
   换 embedding 模型时，维度不一致会报错（好发现），但**换成另一个同维度的模型**
   完全不会报错 —— 库里旧向量的坐标来自旧模型，新查询向量来自新模型，
   相似度是噪声。所以建表时把 `backend:model:dim` 写进 description，
   由 `vector_space_state()` 判定一致性。详见该方法的注释。
"""

import asyncio
import logging
from typing import Any, Dict, List

from pymilvus import MilvusClient

from app.config import settings
from app.core.doc_id import chunk_id as _chunk_id
from app.core.embedding_factory import embedding_identity, resolve_dimension

logger = logging.getLogger(__name__)

PRIMARY_FIELD = "id"
VECTOR_FIELD = "vector"


class CollectionDimensionMismatch(RuntimeError):
    """
    已存在的 Collection 与当前 Embedding 后端产出的维度不一致。

    ★ 为什么要单独一个异常类型

    这种故障和「Milvus 连不上」长得完全不一样，但都发生在启动初始化那一步：

    - 连不上：Milvus 进程没起，用户会去查进程 —— 直觉正确；
    - 维度不一致：Milvus 好好的，只是库是**上一版 embedding 建的**。
      它的现象是**向量召回恒返回空** —— 检索不报错、界面照常出答案
      （还有 BM25 与图路兜底），只是效果差一截。最容易被人当成「模型不行」。

    分类型之后，启动日志才能点名说出「请重建 Collection」，
    而不是混在「Milvus 未就绪」里。
    """


class CollectionModelMismatch(RuntimeError):
    """Vectors produced by another model cannot be mixed, even at equal dimension."""


class MilvusManager:
    """Milvus 连接与 Collection 管理器"""

    _instance: "MilvusManager | None" = None

    def __init__(self):
        self._client: MilvusClient | None = None

    @classmethod
    def get_instance(cls) -> "MilvusManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def client(self) -> MilvusClient:
        if self._client is None:
            # 凭据来自 `settings.milvus_auth_kwargs`：没配用户就一个字段都不发，
            # 未启用认证的环境（老配置、测试基线）行为与改动前逐字一致。
            self._client = MilvusClient(
                uri=settings.milvus_uri, **settings.milvus_auth_kwargs
            )
        return self._client

    # ---------- Collection ----------
    def _existing_dimension(self, coll_name: str) -> int | None:
        """读出已存在 Collection 的向量维度；解析失败返回 None"""
        try:
            desc = self.client.describe_collection(coll_name)
            for field in desc.get("fields", []):
                if field.get("name") == VECTOR_FIELD:
                    return int(field.get("params", {}).get("dim"))
        except Exception as e:  # pragma: no cover - 仅在 schema 异常时触发
            logger.warning("读取 Collection '%s' 维度失败: %s", coll_name, e)
        return None

    def stored_identity(self, coll_name: str | None = None) -> str | None:
        """
        读回 Collection 里登记的**产出模型**（建表时写进 description 的那串）。

        ★ 为什么把「谁建的库」放在库自己身上

        换过 embedding 模型（尤其是换成另一个同维度的模型）之后，
        库里旧向量的坐标来自上一个模型、新查询向量来自当前模型 ——
        两者不在同一个空间，相似度是噪声。**没有任何报错**，
        只是检索效果变差（不变量 53）。

        要发现它，就必须能回答「这批向量是谁产的」。可选做法有两种：
          - 旁路记一份 json：可能和库对不上（有人直接灌数据、换了库但记录还在），
            属于典型的「第二个事实来源」；
          - 写在 Collection 的 description 里：库在哪，答案就在哪 —— 采用这个。

        Returns:
            已登记的标识；Collection 不存在 / 没有 description / 连不上时返回 None
            （None 的语义是**未知**，不是「不一致」—— 两者处置方式不同）
        """
        name = coll_name or settings.MILVUS_COLLECTION
        try:
            if not self.client.has_collection(name):
                return None
            raw = (self.client.describe_collection(name) or {}).get("description") or ""
            return raw.strip() or None
        except Exception as e:  # noqa: BLE001
            logger.debug("读取 Collection '%s' 的产出模型标识失败: %s", name, e)
            return None

    def vector_space_state(self, coll_name: str | None = None) -> Dict[str, Any]:
        """
        判定「向量库里这批向量」与「当前 embedding 配置」是否同一个空间。

        三种结果的**处置方式完全不同**，所以必须分开，不能压成一个 bool：

        | state | 含义 | 该怎么办 |
        | :--- | :--- | :--- |
        | `match` | 标识一致 | 什么都不用做 |
        | `mismatch` | 库是别的模型建的 | 检索会在错误的相似度下打转 → 重建 |
        | `unknown` | 读不到标识（旧库 / Milvus 连不上） | **不下结论**，只提示 |

        `unknown` 单独成一档是有意的：Milvus 没起来时 `describe_collection` 也会失败，
        把它当成 `mismatch` 会让人在容器没开的时候去重建向量库。

        只读、不触发建表 —— 与 `embedding_info()` 同一约定：状态查询不该有副作用。
        """
        name = coll_name or settings.MILVUS_COLLECTION
        out: Dict[str, Any] = {
            "collection": name,
            "expected": None,
            "registered": None,
            "dimension": None,
            "state": "unknown",
            "error": None,
        }
        try:
            out["expected"] = embedding_identity()
        except Exception as e:  # noqa: BLE001
            out["error"] = f"当前 embedding 配置无法确定向量空间: {e}"
            return out

        try:
            if not self.client.has_collection(name):
                out["state"] = "unknown"
                out["error"] = f"Collection '{name}' 尚不存在（首次入库时创建）"
                return out
            out["dimension"] = self._existing_dimension(name)
            out["registered"] = self.stored_identity(name)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"Milvus 不可达或查询失败: {type(e).__name__}: {e}"
            return out

        if out["registered"] is None:
            # 建表早于「登记产出模型」这项改动，或库是别人直接建的 —— 事实未知。
            out["state"] = "unknown"
            out["error"] = "向量库未登记产出模型（早于本功能建的库），无法确认是否同空间"
        elif out["registered"] == out["expected"]:
            out["state"] = "match"
        else:
            out["state"] = "mismatch"
        return out

    def ensure_collection(self) -> None:
        """
        确保 Collection 存在，不存在则创建。

        ⚠️ 维度来自 `embedding_factory.resolve_dimension()`，**不是**直接读
        `EMBEDDING_DIMENSION`：

        - `EMBEDDING_BACKEND=api` 时它等于配置值（行为与以前完全一致）；
        - `EMBEDDING_BACKEND=local` 时它会用模型的 `hidden_size` 交叉校验配置，
          不一致就抛出「你该把 EMBEDDING_DIMENSION 填成多少」的明确错误。

        为什么值得为此多一层间接：维度是**建表时定死**的，事后改不了。
        一旦按错误维度建了库，后续写入与检索会全线失败，而报错来自 pymilvus
        的向量字段校验 —— 指向的是「维度」而不是「你的配置写错了」，很难定位。
        在这里拦住，错误信息才落在真正的原因上。
        """
        coll_name = settings.MILVUS_COLLECTION
        expected = resolve_dimension()
        identity = embedding_identity()

        if self.client.has_collection(coll_name):
            actual = self._existing_dimension(coll_name)
            if actual is not None and actual != expected:
                raise CollectionDimensionMismatch(
                    f"Collection '{coll_name}' 向量维度为 {actual}，"
                    f"但当前 Embedding 后端（{settings.EMBEDDING_BACKEND} / "
                    f"{settings.EMBEDDING_LOCAL_MODEL if settings.EMBEDDING_BACKEND == 'local' else settings.EMBEDDING_MODEL}）"
                    f"要写入 {expected} 维。\n"
                    f"换过 Embedding 模型/后端时需要重建 Collection：\n"
                    f'  python -c "from app.core.milvus_client import milvus_manager as m; '
                    f'm.delete_collection()"\n'
                    f"重建后需重新入库（见 backend/scripts/index_samples.py）。"
                )
            registered = self.stored_identity(coll_name)
            if registered and registered != identity:
                raise CollectionModelMismatch(
                    "当前向量模型与知识库的产出模型不同，即使维度相同也不能混用。"
                    "请在检索模型设置中重建向量库后重试。"
                )
            return

        self.client.create_collection(
            collection_name=coll_name,
            dimension=expected,
            metric_type="COSINE",
            primary_field_name=PRIMARY_FIELD,
            id_type="int",
            vector_field_name=VECTOR_FIELD,
            auto_id=False,                # 主键由 _chunk_id 提供
            enable_dynamic_field=True,    # text / source / page / chunk_index 走动态字段
            # ★ 把「这个库是哪个模型建的」写在库自己身上（见 stored_identity 的注释）。
            #   只在建表时能写，之后改不了 —— 这正是我们想要的：
            #   维度对得上但换了模型时，这里会一直留着**旧**的标识，
            #   于是「该重建了」这件事可以被检测出来，而不是靠人记住。
            description=identity,
        )
        logger.info(
            "✓ 已创建 Milvus Collection '%s'（dim=%s, metric=COSINE, 产出模型=%s）",
            coll_name, expected, identity,
        )

    # ---------- 写入 ----------
    async def upsert(
        self,
        vectors: List[List[float]],
        metadata_list: List[Dict[str, Any]],
    ) -> int:
        """
        批量写入向量（按确定性主键 upsert，重复入库幂等）

        Args:
            vectors: 向量列表
            metadata_list: 对应的元数据（text, source, page, chunk_index）

        Returns:
            实际写入条数
        """
        if not vectors:
            return 0

        coll_name = settings.MILVUS_COLLECTION
        await asyncio.to_thread(self.ensure_collection)

        data: List[Dict[str, Any]] = []
        for vec, meta in zip(vectors, metadata_list):
            source = str(meta.get("source", ""))
            chunk_index = int(meta.get("chunk_index", 0))
            data.append({
                PRIMARY_FIELD: _chunk_id(source, chunk_index),
                VECTOR_FIELD: vec,
                "text": meta.get("text", ""),
                "source": source,
                "page": meta.get("page", 0),
                "chunk_index": chunk_index,
            })

        result = await asyncio.to_thread(self.client.upsert, collection_name=coll_name, data=data)
        written = int(result.get("upsert_count", len(data)))
        logger.debug("Milvus upsert %s 条 → %s", written, coll_name)
        return written

    # ---------- 检索 ----------
    async def search(
        self,
        query_vector: List[float],
        top_k: int = 5,
        *,
        source: str | None = None,
    ) -> List[Dict[str, Any]]:
        """
        向量相似度检索

        Args:
            query_vector: 查询向量
            top_k: 返回前 K 条

        Returns:
            [{"text": ..., "source": ..., "page": ..., "chunk_index": ..., "score": ...}, ...]
        """
        coll_name = settings.MILVUS_COLLECTION
        await asyncio.to_thread(self.ensure_collection)

        filters = {"filter": f'source == "{self._escape_expr_str(source)}"'} if source is not None else {}
        results = await asyncio.to_thread(
            self.client.search,
            collection_name=coll_name,
            data=[query_vector],
            limit=top_k,
            # 必须带上主键 id：Phase 3 的 RRF 融合要用它跟 ES 的结果做跨存储去重，
            # 两边的 doc_id 同源（见 core/doc_id.py），命中同一块时才能合并成一条
            output_fields=[PRIMARY_FIELD, "text", "source", "page", "chunk_index"],
            consistency_level="Strong",   # 刚 upsert 完也能立刻检索到
            **filters,
        )

        hits: List[Dict[str, Any]] = []
        if results and len(results) > 0:
            for hit in results[0]:
                entity = hit.get("entity", {}) or {}
                hits.append({
                    "text": entity.get("text", ""),
                    "source": entity.get("source", ""),
                    "page": entity.get("page", 0),
                    "chunk_index": entity.get("chunk_index", 0),
                    "doc_id": entity.get(PRIMARY_FIELD),
                    "score": float(hit.get("distance", 0.0)),
                })

        return hits

    # ---------- 维护 ----------
    def get_by_ids(self, ids: List[int], *, strict: bool = False) -> List[Dict[str, Any]]:
        """
        按主键批量取回块（Phase 6 起服务于「图路召回后的正文回填」）。

        ★ 为什么需要它

        图谱只存「实体—关系—来源块引用」，**不存正文副本**。
        如果图里也复制一份正文，文档一改就会立刻出现第二处孤儿数据：
        图里的副本还是旧的，而它照样会被召回、被注入 Prompt ——
        与不变量 24 描述的孤儿块症状完全一样。正文的权威来源只有 Milvus / ES。

        所以图路给出 `source#chunk_index` 之后，要回到这里按确定性主键
        `chunk_id(source, chunk_index)` 取回真正的内容。

        Returns:
            [{"doc_id", "text", "source", "page", "chunk_index"}, ...]
            Milvus 不可用或出错时返回空列表（降级，不抛异常）
        """
        wanted = [int(i) for i in ids if isinstance(i, (int, float)) or str(i).lstrip("-").isdigit()]
        if not wanted:
            return []

        coll_name = settings.MILVUS_COLLECTION
        try:
            if not self.client.has_collection(coll_name):
                return []
            rows = self.client.query(
                collection_name=coll_name,
                filter=f"{PRIMARY_FIELD} in {wanted}",
                output_fields=[PRIMARY_FIELD, "text", "source", "page", "chunk_index"],
                consistency_level="Strong",
            )
        except Exception as e:  # noqa: BLE001
            if strict:
                raise
            logger.warning("Milvus 按主键取回失败（图路正文回填将尝试 ES）: %s", e)
            return []

        out: List[Dict[str, Any]] = []
        for row in rows or []:
            out.append({
                "doc_id": row.get(PRIMARY_FIELD),
                "text": row.get("text", ""),
                "source": row.get("source", ""),
                "page": row.get("page", 0),
                "chunk_index": row.get("chunk_index", 0),
            })
        return out

    def count(self, *, strict: bool = False) -> int:
        """当前 Collection 内的实体数量（用于健康检查 / 演示）"""
        coll_name = settings.MILVUS_COLLECTION
        if not self.client.has_collection(coll_name):
            return 0
        try:
            res = self.client.query(
                collection_name=coll_name,
                filter=f"{PRIMARY_FIELD} >= 0",
                output_fields=["count(*)"],
                # 同 search：默认 Bounded 一致性下刚 upsert 完统计会偏少
                consistency_level="Strong",
            )
            if res:
                return int(res[0].get("count(*)", 0))
        except Exception as e:
            if strict:
                raise
            logger.warning("统计 Collection 数量失败: %s", e)
        return 0

    def delete_collection(self) -> None:
        """删除 Collection（开发/调试用；换 Embedding 模型后需要重建）"""
        coll_name = settings.MILVUS_COLLECTION
        if self.client.has_collection(coll_name):
            self.client.drop_collection(coll_name)
            logger.info("已删除 Collection '%s'", coll_name)

    # ---------- 按来源删除（增量索引的前置能力） ----------
    @staticmethod
    def _escape_expr_str(value: str) -> str:
        """
        转义 Milvus 过滤表达式里的字符串字面量。

        ⚠️ Milvus 的 `filter` 是**表达式字符串**，不是参数化查询 —— 文件名里
        出现 `"` 或 `\\` 会直接把表达式切断。后果分两种，都很糟：
          - 语法报错（还好，至少看得见）
          - 表达式被截断后仍合法，于是**删掉了错误的数据**（危险）
        转义顺序必须是先反斜杠后引号，反过来会把刚加的 `\\` 再转义一次。
        """
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def delete_by_source(self, source: str, *, from_chunk: int | None = None, strict: bool = False) -> int:
        """
        删除某个来源文件的全部块（增量索引「改动/删除」时调用）。

        Args:
            source: 入库时写入的 `source` 字段（通常就是文件名）

        Returns:
            删除条数。strict=True 时失败上抛，用于需要确认结果的管理操作。
            from_chunk 仅清理这个序号及之后的块，供文档改短时删除旧尾块。

        ⚠️ `source` 是动态字段（建表时 `enable_dynamic_field=True`），
        过滤表达式里可以直接按名引用。
        ⚠️ 删除后显式 `flush`：Milvus 的删除是墓碑 + 后台压缩，
        不 flush 的话「删完立刻检索」在部分版本上仍可能命中旧数据。
        删除只发生在文件变更时（低频），这次 flush 的代价可以接受。
        """
        coll_name = settings.MILVUS_COLLECTION
        if not source:
            return 0
        try:
            if not self.client.has_collection(coll_name):
                return 0
            expr = f'source == "{self._escape_expr_str(source)}"'
            if from_chunk is not None:
                expr += f" and chunk_index >= {int(from_chunk)}"
            result = self.client.delete(
                collection_name=coll_name,
                filter=expr,
            )
            deleted = int((result or {}).get("delete_count", 0) or 0)
            try:
                self.client.flush(collection_name=coll_name)
            except Exception as e:  # noqa: BLE001
                logger.debug("flush 失败（不影响已提交的删除）: %s", e)
            if deleted:
                logger.info("Milvus 已删除来源 '%s' 的 %s 个块", source, deleted)
            return deleted
        except Exception as e:  # noqa: BLE001
            if strict:
                raise
            logger.warning("Milvus 按来源删除失败（source=%s）: %s", source, e)
            return 0


# 全局单例
milvus_manager = MilvusManager.get_instance()
