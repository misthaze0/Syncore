"""
中文分词层 —— 为 Elasticsearch BM25 提供「词级」切分

★ 为什么需要这一层

Elasticsearch 内置分析器没有中文支持：
- `standard` 分析器把中文按**单字**切开 → "向量数据库" 变成 5 个 token，
  BM25 打分退化为字频统计，无法区分「数据库」和「数据」+「库」；
- 想用 `ik_max_word` 这类专业中文分词器，必须给 ES 装 analysis-ik 插件，
  而插件要从 GitHub 下载，在离线/受限网络环境下不可复现。

折中方案（本项目采用）：**入库侧预分词**
1. Python 端用 jieba 把 chunk 切好，用空格连接写入独立的 `text_seg` 字段；
2. ES 侧对 `text_seg` 使用 `whitespace` 分析器 —— 它是纯空格切分，
   不会把中文再拆散，于是 jieba 的切分结果被原样保留为词项；
3. 检索时对 query 做**同一套** jieba 切分，保证两侧词典一致。

代价是 ES 无法在检索期动态分词，收益是零插件依赖、行为完全可控，
并且分词结果对调试可见（可以直接看 `text_seg` 字段确认切得对不对）。

⚠️ 关键约束：`segment()` 与 `segment_to_text()` 必须使用同一套参数
（同一个 jieba 实例、同一个停用词表），否则入库与检索的词项对不上，
表现为「明明文档里有这个词，BM25 却搜不到」。
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Iterable

logger = logging.getLogger(__name__)

# 领域词典
#
# jieba 的默认词典是通用语料训出来的，对本项目的专有名词会切错
# （例如「向量库」可能被切成「向量」+「库」，「混合检索」切成「混合」+「检索」）。
# 这里把知识库里高频出现、且切错会直接影响 BM25 命中的词显式加入。
#
# 维护方式：发现「文档里明明有、但 BM25 搜不到」的词，就补到这里。
DOMAIN_WORDS: tuple[str, ...] = (
    # 项目 / 产品
    "Syncore", "知识库", "向量库", "向量数据库", "混合检索", "语义检索",
    "关键词检索", "精排", "重排", "召回", "粗排",
    # Agent / 编排
    "多智能体", "子 Agent", "主 Agent", "意图识别", "任务拆解", "状态机",
    "工具调用", "工具治理", "注册中心", "权限分级", "危险等级",
    # 安全 / 审批
    "人工审批", "人工确认", "审批弹窗", "危险操作", "沙箱", "越权",
    "最小权限", "黑名单", "白名单",
    # 技术组件
    "向量化", "嵌入模型", "分词器", "倒排索引", "相似度", "余弦相似度",
    "检查点", "持久化", "降级", "幂等", "流式输出", "会话记忆",
    # 模型名（保留原样，避免被拆散）
    "Embedding", "Reranker", "Milvus", "Redis", "Elasticsearch", "LangGraph",
    "LangChain", "FastAPI", "BM25", "RRF", "MCP", "HITL", "RAG",
)

# 停用词
#
# 只收「对 BM25 打分没有区分度、但会稀释权重」的高频虚词。
# 刻意保持极简：停用词表越长，越容易误删实义词（如「不」「无」这类否定词
# 在问答语料里其实很重要，这里刻意**不**过滤）。
STOPWORDS: frozenset[str] = frozenset({
    "的", "了", "和", "与", "及", "或", "是", "在", "有", "为", "对",
    "中", "上", "下", "里", "个", "这", "那", "你", "我", "他", "它",
    "就", "也", "都", "而", "并", "被", "把", "让", "给", "从", "到",
    "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "着", "过", "得", "地",
    "一", "二", "三", "很", "会", "能", "可以", "需要", "进行", "一个",
})

# 纯符号 / 空白 token（jieba 会把标点也切出来）
_NOISE_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

# 标识符保护
#
# ★ 为什么必须单独处理
#
# jieba 的词典是通用语料训出来的，遇到 `XJMS_MILVUS_PORT`、`ERR_2048_DIM_MISMATCH`
# 这类标识符会按自己的规则切碎（下划线、数字混排都是它的切分点）。
# 一旦切开，BM25 的词项就和检索时的词项对不上 —— 而这恰恰是**引入 BM25 的初衷**：
# 「配置项名 / 错误码 / 端口号」这类字面精确的东西必须能被原样命中。
#
# 所以这里先用正则把「标识符形状」的片段整段抠出来，原样作为单个词项，
# 其余文本再交给 jieba。入库存与检索用的是同一个函数，两侧天然一致。
#
# 认定规则（满足其一即视为标识符）：
#   - 含下划线 / 点 / 连字符 / 斜杠等连接符（XJMS_A、a.b、X-123、api/v1）
#   - 同时含字母与数字（BM25、ERR1001、v3）
#   - 全大写且长度 ≥ 2（ID、API、URL）
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-/+]*")
_HAS_CONNECTOR_RE = re.compile(r"[_.\-/]")
_HAS_ALPHA_RE = re.compile(r"[A-Za-z]")
_HAS_DIGIT_RE = re.compile(r"\d")


def _is_identifier(token: str) -> bool:
    """判断一个 ASCII 串是否应按「标识符」整体保留"""
    if len(token) < 2:
        return False
    if _HAS_CONNECTOR_RE.search(token):
        return True
    if _HAS_ALPHA_RE.search(token) and _HAS_DIGIT_RE.search(token):
        return True
    # 全大写且长度 ≥ 2（避免把普通的 "A"、"I" 当成标识符）
    return token.isupper() and _HAS_ALPHA_RE.search(token) is not None


def _split_identifiers(text: str) -> list[tuple[str, bool]]:
    """
    把文本切成 [(片段, 是否标识符), ...]。

    标识符片段原样保留，其余片段交由 jieba 处理。
    """
    segments: list[tuple[str, bool]] = []
    cursor = 0
    for match in _IDENTIFIER_RE.finditer(text):
        token = match.group(0)
        if not _is_identifier(token):
            continue
        if match.start() > cursor:
            segments.append((text[cursor : match.start()], False))
        segments.append((token, True))
        cursor = match.end()
    if cursor < len(text):
        segments.append((text[cursor:], False))
    return segments

# 是否过滤停用词（可被调用方覆盖，便于做 A/B 对比）
_FILTER_STOPWORDS = True


# ---------- jieba 初始化 ----------
@lru_cache(maxsize=1)
def _ensure_jieba():
    """
    惰性初始化 jieba：加载词典 + 注册领域词。

    jieba 首次 import 会加载主词典（约 1~2 秒，词典文件 ~19MB），
    因此放在函数里惰性执行，避免拖慢应用启动。
    用 lru_cache 保证只加载一次（jieba 自身也是全局单例）。
    """
    import jieba as _jieba

    # jieba 默认往 stderr 打调试日志（"Building prefix dict..."），关掉
    try:
        _jieba.setLogLevel(logging.WARNING)
    except Exception:  # noqa: BLE001 - 版本差异，失败不影响分词
        pass

    for word in DOMAIN_WORDS:
        # 领域词给足词频，避免被统计模型拆散
        _jieba.add_word(word, freq=1_000_000)

    _jieba.initialize()
    logger.info("jieba 分词器就绪（领域词 %s 个，停用词 %s 个）",
                len(DOMAIN_WORDS), len(STOPWORDS))
    return _jieba


def segment(
    text: str,
    *,
    drop_stopwords: bool | None = None,
    min_length: int = 1,
) -> list[str]:
    """
    把文本切成词项列表（入库与检索共用）。

    处理顺序：
    1. 先用正则抠出标识符片段（配置项名 / 错误码 / 端口号）并**原样保留**
    2. 其余文本交给 jieba 做词级切分
    3. 过滤标点、空白、停用词

    Args:
        text: 待切分文本
        drop_stopwords: 是否过滤停用词；None 时用模块配置
        min_length: 词项最小长度，短于该长度的 token 丢弃

    Returns:
        词项列表（保留原顺序，不去重 —— BM25 的词频统计需要重复词）
    """
    if not text or not text.strip():
        return []

    use_filter = _FILTER_STOPWORDS if drop_stopwords is None else drop_stopwords
    jieba = _ensure_jieba()

    tokens: list[str] = []
    for piece, is_identifier in _split_identifiers(text):
        if not piece:
            continue

        if is_identifier:
            # 标识符不经停用词过滤（它天然不是虚词），也不经 jieba
            token = piece.strip()
            if len(token) >= min_length and not _NOISE_RE.match(token):
                tokens.append(token)
            continue

        for raw in jieba.lcut(piece, cut_all=False):
            token = raw.strip()
            if not token or len(token) < min_length:
                continue
            if _NOISE_RE.match(token):
                continue
            if use_filter and token.lower() in STOPWORDS:
                continue
            tokens.append(token)

    return tokens


def segment_to_text(text: str, **kwargs) -> str:
    """
    切分后用空格连接，供 ES 的 `whitespace` 分析器消费。

    即 ES 中 `text_seg` 字段的写入值。
    """
    return " ".join(segment(text, **kwargs))


def segment_join(parts: Iterable[str], **kwargs) -> str:
    """批量切分并拼接（入库时一次处理多段文本）"""
    out: list[str] = []
    for part in parts:
        out.extend(segment(part, **kwargs))
    return " ".join(out)


def normalize_whitespace(text: str) -> str:
    """把连续空白压成一个空格（避免索引里堆积无用空格）"""
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def configure(*, drop_stopwords: bool | None = None, reload: bool = False) -> None:
    """
    运行时调整分词策略（测试 / 调参用）。

    Args:
        drop_stopwords: 是否过滤停用词
        reload: 是否强制刷新 jieba 缓存（改过 DOMAIN_WORDS 后需要）
    """
    global _FILTER_STOPWORDS
    if drop_stopwords is not None:
        _FILTER_STOPWORDS = drop_stopwords
    if reload:
        _ensure_jieba.cache_clear()
        _ensure_jieba()


__all__ = [
    "DOMAIN_WORDS",
    "STOPWORDS",
    "segment",
    "segment_to_text",
    "segment_join",
    "normalize_whitespace",
    "configure",
]
