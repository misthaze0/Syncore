"""
三元组抽取 —— 把一块文档变成「实体 —关系→ 实体」

★ 为什么抽取必须离线做，而不是查询时现抽

查询时现抽会同时毁掉三件事：
1. **延迟**：每轮问答都要多一次模型往返（且是最慢的那种，输出结构化 JSON）。
2. **可复现**：同一段文字两次抽取的结果不完全一致，评估指标会随模型抖动漂移。
3. **可达性**：多跳检索的前提是"图已经存在"—— 查询时才知道 A 连着 B，
   那就等于没有图，只能做一次性的直接匹配。

所以抽取是**入库侧的批处理**，与 Embedding 同一个定位：一次付钱，之后每次查询都受益。
代价是文档更新要重新抽取，这件事由增量索引的 `modified` 分支顺带解决
（见 `rag/incremental.py`：先按来源删干净，再入库 —— 图也必须走同一条路）。

★ 为什么逐块抽取而不是整篇抽取

整篇抽一次看起来更省调用，但会带来两个问题：
- **provenance 丢失**：产出的关系不知道该挂到哪一块上，图路就无法投影回 chunk，
  也就无法参与 RRF（见 models.py 的说明）。
- **失败粒度粗**：一次 JSON 解析失败就丢掉整篇文档的全部关系。

逐块抽取的代价是调用次数 = 块数，所以这个能力是**显式触发**的
（CLI / 手动接口），不做成后台自动 —— 与 `INDEX_WATCH_ENABLED` 同样的取舍：
主动花钱的开关一律 opt-in。

★ 解析为什么要写成纯函数

模型输出不可能 100% 规范：会带 ```json 围栏、会前后加解释、
会漏字段、会把 relation 写成一整句话。这些情况必须能被**单测穷举**，
而不是等到真实跑批时才发现某一种形态解析失败。
所以 `parse_triples()` 不碰网络、不碰图，输入输出都是纯数据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from app.graph.models import (
    DEFAULT_ENTITY_TYPE,
    ENTITY_TYPES,
    Triple,
)

logger = logging.getLogger(__name__)

#: 单块最多接受多少条三元组（超出通常是模型把整块复述了一遍）
MAX_TRIPLES_PER_CHUNK = 20

EXTRACT_PROMPT = """你负责从一段文档片段中抽取**实体关系三元组**，用于构建知识图谱。

抽取规则（严格遵守）：
1. 只抽取文档**明确写出**的事实。不要用你自己的知识补充，不要做推理。
2. 实体名用文档里的**原样写法**。标识符（配置项名、错误码、版本号、编号）必须
   一字不改地保留，包括下划线与大小写 —— 它们是后续精确匹配的依据。
3. 实体名要具体：「XJMS_MILVUS_PORT」「错误码 ERR_2048_DIM_MISMATCH」「星图模块」
   都是好实体；「它」「该配置」「这个功能」这类指代**不要**作为实体。
4. 关系用一个简短的动词短语，中文或英文皆可，不要写成完整句子。
   好的关系：是 / 默认值为 / 依赖 / 属于 / 负责 / 触发 / 使用 / 包含 / 位于
   坏的关系：是一个非常重要的参数 / 在系统启动的时候会被读取
5. 一段话里没有明确的关系就返回空数组。**宁可少抽，不要编造** ——
   错误的关系会让图检索把不相关的文档当成"相关"召回，比没有图更糟。

实体类型只能取以下之一：{entity_types}

文档片段：
{text}

只返回 JSON 数组，不要输出任何解释或 Markdown 代码块。元素格式：
[{{"head": "实体A", "relation": "关系", "tail": "实体B",
  "head_type": "模块", "tail_type": "配置项"}}]

没有任何可抽取的关系时，返回 []"""


# ---------- 解析（纯函数） ----------
def _extract_json_array(text: str) -> Optional[List[Any]]:
    """
    从模型输出里抠出 JSON 数组。

    容忍三种常见形态：
    - ```json [...] ``` 围栏
    - 前后带一句解释（"好的，以下是抽取结果：[...]"）
    - 被包成 `{"triples": [...]}` 的对象
    """
    if not text:
        return None

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    # 退一步：模型可能返回了对象包裹
    obj_start = text.find("{")
    obj_end = text.rfind("}")
    if obj_start != -1 and obj_end > obj_start:
        try:
            obj = json.loads(text[obj_start : obj_end + 1])
        except json.JSONDecodeError:
            return None
        if isinstance(obj, dict):
            for value in obj.values():
                if isinstance(value, list):
                    return value
    return None


def _clean_entity(raw: Any) -> str:
    """清理实体名字符串（去掉首尾标点与空白）"""
    text = str(raw or "").strip().strip("，,。.；;：:\"'`“”「」『』")
    return re.sub(r"\s+", " ", text).strip()


def _clean_relation(raw: Any) -> str:
    """清理关系名（同样去掉首尾标点）"""
    text = str(raw or "").strip().strip("，,。.；;：:\"'`")
    return re.sub(r"\s+", " ", text).strip()


def _clean_type(raw: Any, default: str = DEFAULT_ENTITY_TYPE) -> str:
    """类型只接受白名单内的取值，其余归「其他」"""
    text = str(raw or "").strip()
    return text if text in ENTITY_TYPES else default


def parse_triples(
    raw_text: str,
    *,
    source: str = "",
    chunk_index: int = 0,
    limit: int = MAX_TRIPLES_PER_CHUNK,
) -> List[Triple]:
    """
    把模型输出解析成 Triple 列表（纯函数，可穷举单测）。

    逐条严格校验：字段缺失、自环、超长、类型非法的一律丢弃。
    丢弃比入库便宜得多 —— 一条错误的关系会一直参与多跳遍历。
    """
    data = _extract_json_array(raw_text)
    if not data:
        return []

    out: List[Triple] = []
    seen: set[tuple[str, str, str]] = set()

    for row in data:
        if not isinstance(row, dict) or len(out) >= limit:
            continue
        head = _clean_entity(row.get("head"))
        tail = _clean_entity(row.get("tail"))
        relation = _clean_relation(row.get("relation"))
        if not head or not tail or not relation:
            continue

        triple = Triple(
            head=head,
            relation=relation,
            tail=tail,
            head_type=_clean_type(row.get("head_type")),
            tail_type=_clean_type(row.get("tail_type")),
            source=source,
            chunk_index=int(chunk_index),
            confidence=_safe_float(row.get("confidence"), 1.0),
        )
        if not triple.valid:
            continue
        # 同一块内重复的三元组去重（模型偶尔会把同一条事实说两遍）
        key = (triple.head_key, triple.relation_key, triple.tail_key)
        if key in seen:
            continue
        seen.add(key)
        out.append(triple)

    return out


def _safe_float(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, value))


# ---------- 抽取（打真实模型） ----------
async def extract_triples(
    text: str,
    *,
    source: str = "",
    chunk_index: int = 0,
    model: Optional[str] = None,
) -> List[Triple]:
    """
    对一块文本调模型抽取三元组。

    ⚠️ 任何失败（模型不可用、超时、输出不是 JSON）都返回空列表，**绝不抛异常** ——
    抽取是入库批处理的一个环节，单块失败的正确表现是"这块没有关系"，
    而不是让整个构建任务崩掉（其余块还能继续）。
    """
    if not (text or "").strip():
        return []

    from app.config import settings

    prompt = EXTRACT_PROMPT.format(
        entity_types=" / ".join(ENTITY_TYPES),
        text=text[: settings.GRAPH_EXTRACT_MAX_CHARS],
    )

    try:
        from app.core.llm_factory import create_llm

        llm = create_llm(
            streaming=False,
            model=model or settings.GRAPH_EXTRACT_MODEL or None,
            timeout=settings.GRAPH_EXTRACT_TIMEOUT_SECONDS,
        )
        message = await llm.ainvoke(prompt)
        content = message.content
        raw = content if isinstance(content, str) else str(content)
    except Exception as e:  # noqa: BLE001
        logger.warning("图谱抽取调用模型失败（本块跳过）: source=%s#%s %s",
                       source, chunk_index, e)
        return []

    triples = parse_triples(raw, source=source, chunk_index=chunk_index)
    if triples:
        logger.debug("图谱抽取: %s#%s → %s 条关系", source, chunk_index, len(triples))
    return triples


async def extract_many(
    chunks: Sequence[Dict[str, Any]],
    *,
    concurrency: int = 4,
    model: Optional[str] = None,
) -> List[Triple]:
    """
    并发抽取多块（带并发上限）。

    Args:
        chunks: [{"text", "source", "chunk_index"}, ...]
        concurrency: 同时最多几个模型请求 —— 无上限并发会撞网关限流，
                     表现为"偶发大面积失败"，比慢更糟。
    """
    if not chunks:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(chunk: Dict[str, Any]) -> List[Triple]:
        async with sem:
            return await extract_triples(
                str(chunk.get("text", "")),
                source=str(chunk.get("source", "")),
                chunk_index=int(chunk.get("chunk_index", 0) or 0),
                model=model,
            )

    results = await asyncio.gather(*(one(c) for c in chunks))
    out: List[Triple] = []
    for group in results:
        out.extend(group)
    return out


__all__ = [
    "EXTRACT_PROMPT",
    "MAX_TRIPLES_PER_CHUNK",
    "extract_many",
    "extract_triples",
    "parse_triples",
]
