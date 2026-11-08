"""
Golden Set —— 评估的「标准答案」

★ 它从哪来

Phase 3 的 `scripts/verify_hybrid_search.py` 里已经有一组**带精确 ground truth 的
查询集**（24 条标识符 + 10 条语义改写），但它内嵌在验证脚本里，生命周期只有一次运行。
Phase 4 要做的第一件事就是把它**提出来、持久化、可版本化** —— 于是它从
「一次性实验的输入」变成「可反复回归的资产」，后续所有调参都以它为基准。

★ 两种作用域（scope）

| scope        | 语料来源                     | 衡量什么                         |
|--------------|------------------------------|----------------------------------|
| `controlled` | Golden Set 自带受控语料        | 检索算法**本身**的能力（可复现）  |
| `live`       | 线上知识库（Milvus + ES）      | **当前部署**的实际效果（会漂移）  |

为什么要分：`controlled` 排除数据漂移，回答「算法改得对不对」；
`live` 回答「线上现在到底行不行」。只有前者会得出稳定的结论，
只有后者能发现「索引空了」这类部署问题。两者不可互相替代。

★ 期望命中的表示法

`expected` 用 `"源文件名#块序号"` 的字符串形式（如 `"配置速查.md#3"`），
而不是内部 doc_id 整数。理由：doc_id 是 `sha1("source::chunk_index")[:15]` 派生的
**实现细节**，写进 Golden Set 等于把主键算法固化成评估契约 ——
一旦主键规则调整（Phase 1 就调整过一次），所有标注会静默失效且没有任何报错。
用人类可读的 `source#index` 则永远可解释、可手工校对。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Golden Set 文件格式版本。改动字段语义时必须递增，
#: 否则旧文件会被按新语义解析 —— 表现为「指标突然全面下降」而没人知道为什么。
#:
#: v2（Phase 4b）：新增 `triples`（多跳场景的声明式图）与 `multi_hop` 类目。
#: 旧文件缺这两个字段时，多跳阶段会拿到一个**空图**并静默算出 0 召回 ——
#: 所以 `ensure_default_golden_set` 会在版本落后时重新生成内置集（见其实现）。
GOLDEN_SET_VERSION = 2

KIND_IDENTIFIER = "identifier"
KIND_SEMANTIC = "semantic"
KIND_OPEN = "open"
#: 多跳类目：答案只能通过「查询实体 → 中间实体 → 答案块」这条链到达
KIND_MULTI_HOP = "multi_hop"


def make_doc_ref(source: str, chunk_index: int) -> str:
    """构造 `expected` 用的文档引用字符串"""
    return f"{source}#{chunk_index}"


def parse_doc_ref(ref: str) -> Tuple[str, Optional[int]]:
    """解析文档引用；没有 `#序号` 时返回 (source, None)，表示「该文件任意块均可」"""
    if "#" in ref:
        source, _, idx = ref.rpartition("#")
        try:
            return source, int(idx)
        except ValueError:
            return ref, None
    return ref, None


# ---------- 数据模型 ----------
@dataclass
class CorpusChunk:
    """受控语料中的一块（与入库管道产出的 chunk 结构对齐）"""

    text: str
    source: str
    chunk_index: int
    page: int = 1

    @property
    def ref(self) -> str:
        return make_doc_ref(self.source, self.chunk_index)


@dataclass
class DeclaredTriple:
    """
    声明式三元组 —— 多跳评估用**构造好的图**，而不是让模型现抽。

    ★ 为什么评估探针的图必须由 Golden Set 声明

    多跳评估要衡量的是「**三路融合**带来的召回增益」，而图的构建质量是另一个
    独立的变量。如果探针图也由模型现抽，那么指标掉下来时你分不清是
    「融合逻辑改坏了」还是「模型这次抽得差」—— 两个变量的乘积无法归因。

    所以评估把图**锁死成声明的确定结构**，抽取本身的能力由
    `scripts/verify_graphrag.py` 单独用真实模型验收。这与「评估走生产入口
    `retrieve()`」是同一种思路：一次只让一个变量动。
    """

    head: str
    relation: str
    tail: str
    head_type: str = "其他"
    tail_type: str = "其他"
    #: 这条关系出自哪一块（必须与 corpus 的 source / chunk_index 对得上，
    #: 否则图路拿到的引用会在回填正文时找不到内容而被丢弃）
    source: str = ""
    chunk_index: int = 0

    @property
    def ref(self) -> str:
        """与 `Triple.chunk_ref` 同一种写法（`source#chunk_index`）

        两个类型一个在 `eval` 一个在 `graph`，但指向的是同一批块的同一套主键。
        对齐命名是为了让"声明的关系落在哪一块"在报告、接口与调试输出里
        永远用同一种表示法 —— 否则校验「声明的关系是否指向真实存在的块」
        这件事就得到处手写字符串拼接。
        """
        return make_doc_ref(self.source, self.chunk_index)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "head": self.head,
            "relation": self.relation,
            "tail": self.tail,
            "head_type": self.head_type,
            "tail_type": self.tail_type,
            "source": self.source,
            "chunk_index": self.chunk_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeclaredTriple":
        return cls(
            head=str(data.get("head", "")),
            relation=str(data.get("relation", "")),
            tail=str(data.get("tail", "")),
            head_type=str(data.get("head_type", "其他")),
            tail_type=str(data.get("tail_type", "其他")),
            source=str(data.get("source", "")),
            chunk_index=int(data.get("chunk_index", 0) or 0),
        )


@dataclass
class GoldenItem:
    """一条带标注的查询"""

    query: str
    id: str = ""
    kind: str = KIND_SEMANTIC
    #: 期望被召回并排在前的文档引用（`source#chunk_index`）
    expected: List[str] = field(default_factory=list)
    #: 人工备注（标注意图、易错点）
    note: str = ""
    tags: List[str] = field(default_factory=list)
    #: 可选的参考答案（用于答案质量评估的「参考答案对照」）
    reference_answer: str = ""
    #: 多跳类目专用：这条链要经过的实体（用于报告里解释“为什么它需要两跳”）
    hop_chain: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.kind}-{abs(hash(self.query)) % 10**8:08d}"


@dataclass
class GoldenSet:
    """一套完整的评估集"""

    name: str = "xjms-controlled"
    scope: str = "controlled"          # controlled | live
    version: int = GOLDEN_SET_VERSION
    description: str = ""
    #: 仅 controlled 模式有：自带语料
    corpus: List[CorpusChunk] = field(default_factory=list)
    items: List[GoldenItem] = field(default_factory=list)
    #: 仅 controlled 模式有：多跳场景的声明式图（见 DeclaredTriple）
    triples: List[DeclaredTriple] = field(default_factory=list)

    # ---------- 统计 ----------
    def counts_by_kind(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for item in self.items:
            out[item.kind] = out.get(item.kind, 0) + 1
        return out

    # ---------- 序列化 ----------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "scope": self.scope,
            "description": self.description,
            "corpus": [asdict(c) for c in self.corpus],
            "items": [asdict(i) for i in self.items],
            "triples": [t.to_dict() for t in self.triples],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GoldenSet":
        version = int(data.get("version", 0))
        if version != GOLDEN_SET_VERSION:
            # 只告警不拒绝：阅读旧文件总比直接报错好，但要让使用者知道
            # 指标可能不可比
            logger.warning(
                "Golden Set 版本为 %s，当前实现按 v%s 解析，指标可能与历史不可比",
                version, GOLDEN_SET_VERSION,
            )
        corpus = [CorpusChunk(**c) for c in data.get("corpus", [])]
        items = [GoldenItem(**i) for i in data.get("items", [])]
        triples = [
            DeclaredTriple.from_dict(t)
            for t in data.get("triples", [])
            if isinstance(t, dict)
        ]
        return cls(
            name=data.get("name", "unnamed"),
            scope=data.get("scope", "controlled"),
            version=version or GOLDEN_SET_VERSION,
            description=data.get("description", ""),
            corpus=corpus,
            items=items,
            triples=triples,
        )


def save_golden_set(golden: GoldenSet, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(golden.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


def load_golden_set(path: str | Path) -> GoldenSet:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Golden Set 不存在: {p}")
    return GoldenSet.from_dict(json.loads(p.read_text(encoding="utf-8")))


# 内置受控 Golden Set
#
# 语料形状与 Phase 3 的对照实验完全一致（那是被实测验证过、能区分两路的形状）：
#
#   A 类 · 参数速查表 —— 模板完全一致、**只差标识符**的同质行
#   B 类 · 错误码表   —— 同上
#   C 类 · 语义说明   —— 用口语化措辞提问，与原文几乎零字面重合
#
# A/B 是 BM25 的主责场景（词元在库即召回，确定性保证）；
# C 是向量路的主责场景（BM25 必然失手）。两类合起来才能测出**互补性**。
CONFIG_ROWS: List[Tuple[str, str]] = [
    ("XJMS_MILVUS_HOST", "localhost"),
    ("XJMS_MILVUS_PORT", "19530"),
    ("XJMS_MILVUS_COLLECTION", "knowledge_base"),
    ("XJMS_REDIS_URL", "redis://localhost:6379/0"),
    ("XJMS_ES_URL", "http://localhost:9200"),
    ("XJMS_ES_INDEX", "xjms_knowledge"),
    ("XJMS_EMBEDDING_MODEL", "qwen3.7-text-embedding"),
    ("XJMS_EMBEDDING_DIMENSION", "1024"),
    ("XJMS_EMBEDDING_MAX_BATCH", "20"),
    ("XJMS_UPLOAD_DIR", "./data/uploads"),
    ("XJMS_SANDBOX_DIR", "./data/workspace"),
    ("XJMS_LOG_LEVEL", "INFO"),
    ("XJMS_RRF_K", "60"),
    ("XJMS_RERANK_TOP_N", "5"),
    ("XJMS_APPROVAL_TIMEOUT", "300"),
    ("XJMS_MAX_TOOL_STEPS", "6"),
    ("XJMS_LLM_TIMEOUT", "90"),
    ("XJMS_LLM_MAX_RETRIES", "1"),
    ("XJMS_HYBRID_VECTOR_TOP_K", "20"),
    ("XJMS_HYBRID_BM25_TOP_K", "20"),
]

ERROR_ROWS: List[str] = [
    "ERR_1042_CONN_REFUSED",
    "ERR_2048_DIM_MISMATCH",
    "ERR_3001_PATH_ESCAPE",
    "ERR_4096_APPROVAL_TIMEOUT",
]

# (正文, 换一种说法的提问, 参考答案)
SEMANTIC_CHUNKS: List[Tuple[str, str, str]] = [
    (
        "会话状态被持久化到外部存储之后，服务进程被重新拉起时仍然可以接续之前的对话。"
        "如果外部存储不可用，则退化为仅在本进程内保留上下文，进程退出即丢失。",
        "我重启一下后台，之前跟它聊的内容还在吗",
        "会保留。会话状态持久化在外部存储（Redis），进程重启后可接续；"
        "外部存储不可用时才退化为进程内保存，进程退出即丢失。",
    ),
    (
        "系统会对同一份内容派生出固定不变的主键，重复提交时执行覆盖写入而不是追加，"
        "因此反复提交相同资料不会导致检索结果里出现重复片段。",
        "同一份资料我传了两遍，会不会搜出来两条一样的",
        "不会。主键由内容与块序号确定性派生，重复提交走覆盖写入，不会产生重复片段。",
    ),
    (
        "对可能造成不可逆影响的操作，系统会先暂停流程并向使用者发起确认；"
        "只有得到明确同意之后才会真正执行，否则该步骤被跳过并向模型回传拒绝说明。",
        "删东西之前它会不会先问我一声",
        "会。危险操作会先挂起流程等使用者确认，只有明确同意后才执行，拒绝则跳过并回传拒绝说明。",
    ),
    (
        "所有面向本地文件系统的读写都先做路径规范化，再判断是否仍位于许可目录之内，"
        "试图向上跳转的相对路径会被识别并拒绝。",
        "它能不能偷偷翻到我电脑里别的地方去",
        "不能。文件操作先做路径规范化再校验是否在许可目录内，向上跳转的相对路径会被拒绝。",
    ),
    (
        "当外部检索组件不可用时，问答会自动忽略检索环节直接由模型作答，"
        "这样做会让准确率下降，但保证服务本身始终可用。",
        "如果检索那套东西挂了，还能不能正常提问",
        "能。检索组件不可用时会跳过检索、直接由模型作答，准确率下降但服务保持可用。",
    ),
    (
        "把语义相似度和字面匹配两路结果按名次重新合并，可以避免因为两路打分尺度不同而"
        "需要手工调参，代价是丢弃了分差信息，所以要再接一层交叉编码器重新打分。",
        "为什么要把两种搜索结果按排名混在一起而不用分数相加",
        "因为向量余弦与 BM25 的分数量纲不可比，归一化需要先验分布假设、等于逐份语料调参；"
        "RRF 只看名次天然免疫量纲问题，代价是丢失分差，所以再接一层交叉编码器精排补偿。",
    ),
    (
        "负责对外发送消息或执行系统命令的能力被单独标记为高风险，"
        "调用之前必须经过使用者确认，只读类能力则直接放行。",
        "哪些功能会在动手前先弹窗让我点同意",
        "高风险能力（对外发消息、执行系统命令、删除文件等）在调用前需要确认，只读类能力直接放行。",
    ),
    (
        "领域概念需要整体保留为不可分割的最小单位，否则基于字面的检索算法会把它们切碎，"
        "导致文档里明明出现过却搜索不到。",
        "搜不到明明存在的词，一般是什么原因造成的",
        "常见原因是分词把标识符（配置项名、错误码）切成了碎片，导致字面检索无法精确匹配；"
        "所以分词前要先用正则把标识符整体保留。",
    ),
    (
        "每个执行单元在启动时只被授予完成自身职责所必需的那部分能力，"
        "越出授权范围的要求会在分发这一层被直接挡掉并返回结构化说明。",
        "某个助手会不会越权去用不属于它的功能",
        "不会。工具按最小权限授权给各执行单元，越出授权范围的调用会在分发层被挡掉并返回结构化说明。",
    ),
    (
        "把历史对话截断到最近若干条，并剔除结构不完整的工具消息块，"
        "避免请求体违规或者上下文被无关内容挤占。",
        "聊得时间长了它是怎么处理上下文的",
        "只保留最近若干条历史，并剔除结构不完整的工具消息块，避免请求体违规与上下文被挤占。",
    ),
]


def build_controlled_golden_set(name: str = "xjms-controlled") -> GoldenSet:
    """
    构造内置受控 Golden Set（24 标识符 + 10 语义 + 6 多跳 = 40 条查询）。

    前 34 条是 `verify_hybrid_search.py` 里那套语料的**持久化版本**，
    语料文本与查询一字未改 —— 改了就与 Phase 3 的对照实验结论失去可比性。

    后 6 条（Phase 4b）是**多跳**场景，配套声明式图，构造见 `_build_multi_hop`。
    """
    corpus: List[CorpusChunk] = []
    items: List[GoldenItem] = []
    triples: List[DeclaredTriple] = []

    # ---------- A. 参数速查表 ----------
    for i, (key, value) in enumerate(CONFIG_ROWS):
        corpus.append(CorpusChunk(
            text=(
                f"配置项 {key}：当前默认值为 {value}。"
                "该配置项属于系统参数，修改后需要重启服务才会生效。"
            ),
            source="参数速查表.md",
            chunk_index=i,
        ))
        items.append(GoldenItem(
            id=f"cfg-{key}",
            query=f"{key} 的默认值是多少",
            kind=KIND_IDENTIFIER,
            expected=[make_doc_ref("参数速查表.md", i)],
            note=key,
            tags=["配置项", "精确匹配"],
            reference_answer=f"{key} 的默认值是 {value}，修改后需要重启服务才会生效。",
        ))

    # ---------- B. 错误码表 ----------
    for i, code in enumerate(ERROR_ROWS):
        corpus.append(CorpusChunk(
            text=(
                f"错误码 {code}：表示运行期出现异常。"
                "排查时请优先检查该项对应的配置是否被改动。"
            ),
            source="错误码速查.md",
            chunk_index=i,
        ))
        items.append(GoldenItem(
            id=f"err-{code}",
            query=f"{code} 是什么错误",
            kind=KIND_IDENTIFIER,
            expected=[make_doc_ref("错误码速查.md", i)],
            note=code,
            tags=["错误码", "精确匹配"],
            reference_answer=f"{code} 表示运行期出现异常，排查时优先检查该项对应的配置是否被改动。",
        ))

    # ---------- C. 语义型说明 ----------
    for i, (text, paraphrase, reference) in enumerate(SEMANTIC_CHUNKS):
        corpus.append(CorpusChunk(text=text, source="设计说明.md", chunk_index=i))
        items.append(GoldenItem(
            id=f"sem-{i:02d}",
            query=paraphrase,
            kind=KIND_SEMANTIC,
            expected=[make_doc_ref("设计说明.md", i)],
            note=paraphrase,
            tags=["语义改写"],
            reference_answer=reference,
        ))

    # ---------- D. 多跳场景 ----------
    hop_corpus, hop_items, hop_triples = _build_multi_hop()
    corpus.extend(hop_corpus)
    items.extend(hop_items)
    triples.extend(hop_triples)

    return GoldenSet(
        name=name,
        scope="controlled",
        description=(
            "受控语料评估集：24 条标识符查询（BM25 主责）+ 10 条语义改写查询（向量主责）"
            "+ 6 条多跳查询（图路主责）。语料刻意做成三种极端形态："
            "「只差标识符的同质行」「零字面重合的口语提问」「答案与查询零字面重合且必须"
            "经另一个实体中转才能到达」，以便分别测出三路召回的互补性。"
        ),
        corpus=corpus,
        items=items,
        triples=triples,
    )


# 多跳场景的构造
#
# 每一条多跳样本由三部分构成：
#
#   桥接块   模块登记表.md#i      "模块「星图」的代号是 MOD-8801，由调度中心统一编排。"
#   答案块   构建窗口说明.md#i    "代号 MOD-8801 的构建窗口被设定为四十五秒，越界即视为未完成。"
#   查询                         “星图模块异常终止的判定阈值是什么？”
#
# 声明的图：  (星图模块) -[代号]-> (MOD-8801) -[构建窗口]-> (四十五秒)
#             桥接块                             答案块
#
# ★ 查询与答案块**零字面重合**，且答案块不含查询的任何词元
#
# 这不是为了“制造困难”，而是为了给出一个**确定性的**结论：
# BM25 无法召回答案块（它的词元与查询一个都不重合），这是由构造保证的、
# 与模型无关的事实 —— 与「BM25 在语义改写场景必然失手」是同一类可断言的性质。
#
# ★ 为什么 expected 只标答案块，不标桥接块
#
# 桥接块是**手段**不是**目标**。如果把桥接块也标进 expected，那么
# `first_relevant_rank` 只要命中桥接块就算成功 —— 而桥接块是 BM25 一定能
# 命中的，于是所有阶段的召回率都会是满分，这个类目就失去了区分度。
# 只标答案块，“单跳检索能不能自己够到答案”这个问题才有确定答案。
MULTI_HOP_MODULES: List[Tuple[str, str, str]] = [
    # (模块名, 代号, 构建窗口)
    ("星图", "MOD-8801", "四十五秒"),
    ("潮汐", "MOD-8802", "两分钟"),
    ("织机", "MOD-8803", "九十秒"),
    ("灯塔", "MOD-8804", "三十秒"),
    ("罗盘", "MOD-8805", "七十五秒"),
    ("熔炉", "MOD-8806", "一分半"),
]

BRIDGE_SOURCE = "模块登记表.md"
ANSWER_SOURCE = "构建窗口说明.md"


def _build_multi_hop() -> Tuple[List[CorpusChunk], List[GoldenItem], List[DeclaredTriple]]:
    """构造多跳语料 + 查询 + 声明式图"""
    corpus: List[CorpusChunk] = []
    items: List[GoldenItem] = []
    triples: List[DeclaredTriple] = []

    for i, (module, code, window) in enumerate(MULTI_HOP_MODULES):
        entity = f"{module}模块"

        corpus.append(CorpusChunk(
            text=f"模块「{module}」的代号是 {code}，由调度中心统一编排。",
            source=BRIDGE_SOURCE,
            chunk_index=i,
        ))
        corpus.append(CorpusChunk(
            text=f"代号 {code} 的构建窗口被设定为 {window}，越界即视为未完成。",
            source=ANSWER_SOURCE,
            chunk_index=i,
        ))

        items.append(GoldenItem(
            id=f"hop-{code}",
            query=f"{entity}异常终止的判定阈值是什么",
            kind=KIND_MULTI_HOP,
            # ⚠️ 只标答案块（理由见上方注释）
            expected=[make_doc_ref(ANSWER_SOURCE, i)],
            note=f"{entity} → {code} → 构建窗口",
            tags=["多跳", "跨文档", "零字面重合"],
            reference_answer=f"代号 {code} 的构建窗口被设定为 {window}，越界即视为未完成。",
            hop_chain=[entity, code, window],
        ))

        triples.append(DeclaredTriple(
            head=entity, relation="代号", tail=code,
            head_type="模块", tail_type="其他",
            source=BRIDGE_SOURCE, chunk_index=i,
        ))
        triples.append(DeclaredTriple(
            head=code, relation="构建窗口", tail=window,
            head_type="其他", tail_type="其他",
            source=ANSWER_SOURCE, chunk_index=i,
        ))

    return corpus, items, triples


def ensure_default_golden_set(path: str | Path) -> GoldenSet:
    """
    读取 Golden Set；文件不存在时**写出内置的那套**并返回。

    首次运行自动生成而不是直接报错，是因为「评估」这件事的第一次体验
    决定它会不会被继续用下去 —— 一个必须先手工准备标注文件的评估脚本，
    大概率只会被跑一次。

    ★ 版本落后时重新生成（这是 v2 新增的行为，很重要）

    磁盘上的 v1 文件没有 `triples` 与 `multi_hop` 条目。如果直接沿用，
    多跳阶段会拿到一个**空图**，图路恒返回空，指标算出一片零 ——
    而报告里不会有任何错误，看起来就是"图检索没用"。
    这种静默的假结论比报错危险得多，所以版本落后时直接重建内置集。

    ⚠️ 只重建**内置集**（按 `name` 判定）。用户自定义的 Golden Set 不动它 ——
    那里面可能是手工标注的心血，不能因为版本号就覆盖掉。
    """
    p = Path(path)
    builtin_name = "xjms-controlled"

    if p.exists():
        try:
            golden = load_golden_set(p)
        except Exception as e:  # noqa: BLE001
            logger.warning("Golden Set 读取失败，将重新生成内置集: %s", e)
        else:
            if golden.version >= GOLDEN_SET_VERSION:
                return golden
            if golden.name != builtin_name:
                logger.warning(
                    "Golden Set '%s' 版本为 v%s（当前 v%s），且不是内置集，"
                    "保留原文件不做改动；多跳类目将不可用",
                    golden.name, golden.version, GOLDEN_SET_VERSION,
                )
                return golden
            logger.info(
                "内置 Golden Set 版本 v%s → v%s，重新生成（新增多跳类目与声明式图）",
                golden.version, GOLDEN_SET_VERSION,
            )

    golden = build_controlled_golden_set()
    save_golden_set(golden, p)
    logger.info(
        "已生成内置 Golden Set: %s（%s 条查询 / %s 块语料 / %s 条声明关系）",
        p, len(golden.items), len(golden.corpus), len(golden.triples),
    )
    return golden


__all__ = [
    "ANSWER_SOURCE",
    "BRIDGE_SOURCE",
    "CorpusChunk",
    "DeclaredTriple",
    "GoldenItem",
    "GoldenSet",
    "KIND_IDENTIFIER",
    "KIND_MULTI_HOP",
    "KIND_SEMANTIC",
    "KIND_OPEN",
    "MULTI_HOP_MODULES",
    "GOLDEN_SET_VERSION",
    "build_controlled_golden_set",
    "ensure_default_golden_set",
    "load_golden_set",
    "make_doc_ref",
    "parse_doc_ref",
    "save_golden_set",
]
