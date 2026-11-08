"""
图谱数据模型 —— 实体、关系，以及「这条关系凭哪一块文档得出」

★ 全模块唯一不可妥协的约束：图的产出必须能投影回 chunk

向量路与 BM25 路之所以能被 RRF 融合，是因为它们返回的都是**块级**结果，
且都带同一套主键（`source` + `chunk_index`，同源于 `core/doc_id.py`）。
图路天生返回的是「实体」与「关系」—— 如果它直接把这些交出去，
就永远无法参与融合，只能当一个平行的、和主检索无关的玩具。

所以 `Triple` 必然携带 `source` / `chunk_index`：图的每一条结论都能回答
「你凭哪一块文档这么说」。这样图路才能把自己**投影回 chunk**，
变成第三路召回参与 RRF；界面上也才能展示「这条来源是顺着哪条关系找过来的」。

★ 为什么实体名要归一化（而不是原样存）

同一个东西在文档里会有多种写法：`XJMS_MILVUS_PORT` 与 `xjms_milvus_port`、
「星图模块」与「星图」、带全角括号与不带。归一化只做**大小写与空白/包裹符号**
这类不会改变语义的折叠，**绝不**做同义词替换或词干化 ——
那属于实体消解（entity resolution），需要模型或词典，一旦判错就会把两个不同的
东西合并成一个节点，而且不可逆。本项目选择「宁可多几个节点，不可错合并」。

★ 关系名为什么强制 UPPER_SNAKE

关系名由模型自由生成，若不收敛会出现 `使用` / `uses` / `USE` / `使用的是`
四种写法指向同一个语义。统一成 UPPER_SNAKE 后，同一个关系的边能被聚合，
`GRAPH_MAX_HOPS` 的遍历也才有稳定的语义。中文关系名会被转写成
拼音化的形式会丢失语义，所以规则是：**英文词 → 大写；中文词 → 原样保留**
（中文本身没有大小写，保留原样即是稳定形态）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

# 实体类型
#
# 刻意保持小集合。类型越多，模型在抽取时越容易犹豫、越容易把同一个东西
# 归到不同类；而类型在本项目里只用于展示与过滤，不参与检索打分，
# 因此「够用」比「精确」重要。
ENTITY_TYPES: tuple[str, ...] = (
    "配置项",   # XJMS_MILVUS_PORT 这类键名
    "错误码",   # ERR_2048_DIM_MISMATCH
    "组件",     # Milvus / Redis / Elasticsearch / Neo4j
    "模块",     # 星图模块 / 检索层 / 工具治理中心
    "概念",     # 混合检索 / RRF / 幂等 / 降级
    "角色",     # 用户 / 管理员 / 子 Agent
    "其他",
)

DEFAULT_ENTITY_TYPE = "其他"

# 归一化时要去掉的包裹符号（全角半角都算）
_WRAPPER_CHARS = "\"'`“”‘’「」『』【】（）()[]{}<>《》 "
_WRAPPER_RE = re.compile(f"^[{re.escape(_WRAPPER_CHARS)}]+|[{re.escape(_WRAPPER_CHARS)}]+$")
_WHITESPACE_RE = re.compile(r"\s+")
_RELATION_NOISE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")

# 关系名长度上限（超出通常是模型把一整句抄了下来）
MAX_RELATION_CHARS = 32
MAX_ENTITY_CHARS = 64


def normalize_entity(name: str) -> str:
    """
    实体名的归一化键（用于匹配与去重）。

    只做三件事，全部是无损的语义保持操作：
    1. 去掉首尾的引号 / 括号 / 空白等包裹符号
    2. 内部连续空白压成一个空格
    3. ASCII 部分转小写（中文无大小写，不受影响）

    ⚠️ 不要在这里加同义词映射。把「星图模块」和「星图」合并成一个键，
    会让一次错误的合并永久污染图，而且无法通过重跑修复（图里已经没有
    「本来是两个实体」这个信息了）。
    """
    text = _WRAPPER_RE.sub("", (name or "").strip())
    text = _WHITESPACE_RE.sub(" ", text)
    return text.lower()


def normalize_relation(relation: str) -> str:
    """
    关系名归一化：英文转大写、非字母数字汉字字符压成下划线。

    `使用` / `uses` / `used-by` → `使用` / `USES` / `USED_BY`。
    中文保留原样：它没有大小写，原样即稳定形态。
    """
    text = _RELATION_NOISE_RE.sub("_", (relation or "").strip())
    text = re.sub(r"_{2,}", "_", text).strip("_")
    return text.upper()


def display_entity(name: str) -> str:
    """展示用实体名：去掉包裹符号，但**保留原始大小写**"""
    return _WHITESPACE_RE.sub(" ", _WRAPPER_RE.sub("", (name or "").strip()))


# ---------- 数据模型 ----------
@dataclass
class Triple:
    """
    一条「实体 —关系→ 实体」，并携带它的来源块。

    这是图与检索之间的**唯一接口**：抽取产出 Triple，入库写 Triple，
    检索时从 Triple 读回 `source` / `chunk_index` 去回填正文。
    """

    head: str
    relation: str
    tail: str
    head_type: str = DEFAULT_ENTITY_TYPE
    tail_type: str = DEFAULT_ENTITY_TYPE
    #: 得出这条关系的来源文件（与 Milvus / ES 的 `source` 同源）
    source: str = ""
    #: 该文件内的块序号（与 Milvus / ES 的 `chunk_index` 同源）
    chunk_index: int = 0
    #: 抽取置信度（模型给不出时按 1.0 处理，不做惩罚）
    confidence: float = 1.0

    # ---------- 归一化视图 ----------
    @property
    def head_key(self) -> str:
        return normalize_entity(self.head)

    @property
    def tail_key(self) -> str:
        return normalize_entity(self.tail)

    @property
    def relation_key(self) -> str:
        return normalize_relation(self.relation)

    @property
    def chunk_ref(self) -> str:
        """`源文件#块序号`，与 Golden Set 的 `expected` 表示法一致"""
        return f"{self.source}#{self.chunk_index}"

    @property
    def valid(self) -> bool:
        """
        是否是一条可用的事实。

        自环（head == tail）一律丢弃：模型在长文档里很容易产出
        「X 的别称是 X」这类空洞事实，它对多跳检索毫无价值，
        却会污染邻居展开（让 X 在每跳都重新出现）。
        """
        if not self.head_key or not self.tail_key or not self.relation_key:
            return False
        if self.head_key == self.tail_key:
            return False
        if len(self.head) > MAX_ENTITY_CHARS or len(self.tail) > MAX_ENTITY_CHARS:
            return False
        if len(self.relation_key) > MAX_RELATION_CHARS:
            return False
        return True

    def to_row(self) -> Dict[str, Any]:
        return {
            "head": display_entity(self.head),
            "head_key": self.head_key,
            "head_type": self.head_type,
            "relation": self.relation_key,
            "tail": display_entity(self.tail),
            "tail_key": self.tail_key,
            "tail_type": self.tail_type,
            "source": self.source,
            "chunk_index": int(self.chunk_index),
            "confidence": float(self.confidence),
        }

@dataclass
class GraphChunk:
    """
    图路召回的一个候选块 —— **投影回 chunk 之后**的结果。

    它与向量/BM25 的 hit 同构（同样的 `source` / `chunk_index`），
    因此能直接丢进 `rrf_fuse`。额外带的是「为什么它被找到」：
    `hops`（跳数）、`path`（走过的实体链）、`via_relations`（用到的关系）。
    """

    source: str
    chunk_index: int
    score: float = 0.0
    #: 从查询命中实体走到本块所需的最小跳数（0 = 本块直接提到了查询实体）
    hops: int = 0
    #: 实体链，如 ["星图模块", "ZX-77"]，用于界面展示推理路径
    path: List[str] = field(default_factory=list)
    #: 经过的关系名（与 path 对齐：第 i 条关系连接 path[i] 与 path[i+1]）
    via_relations: List[str] = field(default_factory=list)
    #: 回填的正文（由 store 之外的 hydrate 步骤填入）
    text: str = ""
    page: int = 0

    def to_hit(self) -> Dict[str, Any]:
        """转成与 `retrieve_vector` / `retrieve_bm25` 同构的 hit 字典"""
        return {
            "text": self.text,
            "source": self.source,
            "page": self.page,
            "chunk_index": int(self.chunk_index),
            "score": float(self.score),
            "retrieval": "graph",
            "recalled_by": ["graph"],
            "graph_hops": int(self.hops),
            "graph_path": list(self.path),
            "graph_relations": list(self.via_relations),
        }

    @property
    def ref(self) -> str:
        return f"{self.source}#{self.chunk_index}"


@dataclass
class GraphStats:
    """图的规模快照（供 /api/graph/status 与启动日志）"""

    space: str = ""
    entities: int = 0
    relations: int = 0
    chunks: int = 0
    types: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "space": self.space,
            "entities": self.entities,
            "relations": self.relations,
            "chunks": self.chunks,
            "types": dict(self.types),
        }


__all__ = [
    "DEFAULT_ENTITY_TYPE",
    "ENTITY_TYPES",
    "MAX_ENTITY_CHARS",
    "MAX_RELATION_CHARS",
    "GraphChunk",
    "GraphStats",
    "Triple",
    "display_entity",
    "normalize_entity",
    "normalize_relation",
]
