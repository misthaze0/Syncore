"""
Embedding 工厂 - 文本向量化（入库 + 检索）

两种后端，由 `EMBEDDING_BACKEND` 选择：

| 后端 | 取值 | 说明 |
| :--- | :--- | :--- |
| 外部 API | `api`（默认） | 走 OpenAI 兼容的 `/embeddings` 接口 |
| 本地模型 | `local` | 进程内跑 sentence-transformers（默认 `BAAI/bge-small-zh-v1.5`） |

**为什么要有 `local`**：外部 embedding 每轮问答至少一次网络往返 + 排队；
本地模型加载一次后常驻内存，单条查询在 CPU 上是毫秒级。
另外入库是**全量分块**、条数远多于问答，本地不按 token 计费；
数据也不出机器。

两者对上层**完全等价** —— `get_embeddings()` / `get_embedding()` 的签名与返回不变，
检索（`rag/retriever.py`）、入库（`rag/indexer.py`）、评估（`eval/runner.py`）
三条链路都不需要知道自己用的是哪种后端。

---

## 一、query / passage 是两条路，别合并

`get_embedding(text)` 是**查询侧**，`get_embeddings(texts)` 是**入库侧**。
对 `bge-*-v1.5` 这类**非对称**模型，官方允许给查询加指令前缀，
但**入库侧绝不能加**。一旦两侧用了不同的前缀却不自知，相似度会整体偏移 ——
而且**不会报错**，只是「检索效果悄悄变差」，属于最难查的一类问题。
所以这里用两个函数名把语义钉死，再由 `kind` 参数显式传递；
默认值保证现有调用点自动落在正确的一侧。

## 二、本地后端的关键工程点

1. **惰性加载 + 双检锁**：权重在首次调用时才载入（冷启动 2~12 秒，其中绝大部分是
   首次导入 torch / sentence-transformers 的代价，权重反序列化只要 0.3 秒），
   不拖慢应用启动；与 `rag/reranker.py` 的本地精排后端同一套模式。
   应用启动时另有后台预热（见 `main.py` 的 Phase 3），首问不必付这笔钱。
2. **不阻塞事件循环**：`SentenceTransformer.encode` 是同步且 CPU 密集的，
   必须走 `asyncio.to_thread`，否则会把整个 FastAPI 的事件循环卡住 ——
   表现为「向量化期间所有请求都不响应」。
3. **维度以模型为准**：本地模型的维度由 `config.json` 的 `hidden_size` 决定，
   与 `EMBEDDING_DIMENSION` 不一致时**直接报错并给出该填的值**。
   否则会按错误维度建出 Milvus Collection，之后写入/检索全线失败，
   而报错来自 pymilvus，指向的是「向量维度」而不是「你配置写错了」。
4. **依赖缺失即失败**：torch / sentence-transformers 未安装时抛出带上下文的
   `EmbeddingError`，不静默兜底 —— 检索侧已有单路超时降级（不变量 14），
   这里静默兜底只会把「配置错了」伪装成「检索没结果」。

## 三、外部 API 后端的批大小要自适应

各家网关的单批上限并不统一：本项目实测所用的 qwen 网关在单批超过 20 条时直接返回
`400 InvalidParameter: batch size ... should not be larger than 20`。
写死一个「看起来安全」的大值（早期版本是 100）在文件块数少时毫无问题，
文件一大就必然 400 —— 属于典型的「只有数据量上去才暴露」的缺陷。
因此这里做两件事：

  a) 以 `EMBEDDING_MAX_BATCH` 配置项（默认 20）作为首选批大小；
  b) 收到「批大小不合法」类错误时**自动折半重试**，最小退到 1 条，
     这样换个上限更小的网关也不用改代码。

并显式传 `dimensions`，让返回向量长度与 `EMBEDDING_DIMENSION` 严格一致。
部分模型（如 qwen3.7-text-embedding-flash）会忽略该参数并固定输出 1024，
所以拿到结果后必须再校验一次，否则会在 Milvus 侧报出难以定位的维度错误。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# 批大小下限：折半重试的最小粒度
_MIN_BATCH = 1
# 单条文本是哪一侧的（见模块 docstring 第一节）
KIND_QUERY = "query"
KIND_PASSAGE = "passage"


class EmbeddingError(RuntimeError):
    """Embedding 调用失败（带上下文的可读错误）"""


class _BatchTooLarge(EmbeddingError):
    """
    内部信号：网关认为批大小超限。

    单独建类是为了把「批太大」与「真的调用失败」区分开 ——
    前者应该自动折半重试，后者应该直接把错误抛给调用方。
    """


# ---------------------------------------------------------------- 后端选择


def backend_name() -> str:
    """当前配置的 embedding 后端（小写；未知值原样返回，由调用方按 api 处理）"""
    return (settings.EMBEDDING_BACKEND or "api").strip().lower()


def effective_model_name() -> str:
    """
    当前后端**实际在用**的模型名。

    健康检查与 `GET /api/info` 都报这个值 —— 报 `EMBEDDING_MODEL` 会在
    本地后端下显示一个根本没用上的模型名，属于「报告与事实不符」。
    """
    if backend_name() == "local":
        return settings.EMBEDDING_LOCAL_MODEL
    return settings.EMBEDDING_MODEL


# ---------------------------------------------------------------- 维度解析

_LOCAL_DIM_CACHE: Dict[str, int] = {}


def local_model_dimension(ref: str | None = None) -> int:
    """
    读出本地模型的向量维度。

    **不需要加载 torch** —— 只读 `config.json` 的 `hidden_size`，
    所以可以在启动探针里零成本地校验「配置的维度」与「模型的维度」是否一致。
    返回 0 表示读不出来（HF 远程 id、目录不存在、字段缺失），
    由调用方决定退回配置值还是报错。
    """
    key = ref or settings.EMBEDDING_LOCAL_MODEL
    if key in _LOCAL_DIM_CACHE:
        return _LOCAL_DIM_CACHE[key]

    dim = 0
    try:
        cfg = Path(key) / "config.json"
        if cfg.is_file():
            dim = int(json.loads(cfg.read_text(encoding="utf-8")).get("hidden_size") or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取本地模型维度失败（%s）: %s", key, e)

    _LOCAL_DIM_CACHE[key] = dim
    return dim


def resolve_dimension() -> int:
    """
    当前后端实际会产出的向量维度。

    - `api`：只能相信 `EMBEDDING_DIMENSION`（真实维度要调用一次才拿得到，
      由 `_embed_batch_api` 兜底校验）。
    - `local`：以模型的 `hidden_size` 为准；与配置不一致就**报错**。

    为什么本地要报错而不是「以模型为准、忽略配置」：
    Milvus 建表用的是这个值，而**已存在的 Collection 无法改维度**。
    如果悄悄按模型维度走，用户会遇到「配置写着 1024，实际建了 512」，
    下次再切回 API 后端就得到一个维度不匹配的库；
    报错并给出该填的值，是唯一不会留下脏状态的选项。
    """
    declared = int(getattr(settings, "EMBEDDING_DIMENSION", 0) or 0)
    ref = settings.EMBEDDING_LOCAL_MODEL

    if backend_name() != "local":
        if declared <= 0:
            raise EmbeddingError(
                "EMBEDDING_DIMENSION 必须为正整数（EMBEDDING_BACKEND=api 时无法自动探测维度）"
            )
        return declared

    detected = local_model_dimension(ref)
    if detected and declared and detected != declared:
        raise EmbeddingError(
            f"本地模型 '{ref}' 输出 {detected} 维，但 EMBEDDING_DIMENSION={declared}。\n"
            f"请把 .env 里的 EMBEDDING_DIMENSION 改成 {detected}，"
            f"并重建 Milvus Collection（维度变了，旧向量不兼容）：\n"
            f'  python -c "from app.core.milvus_client import milvus_manager as m;'
            f' m.delete_collection()"'
        )
    if not detected and not declared:
        raise EmbeddingError(
            f"无法确定向量维度：EMBEDDING_DIMENSION 未设置，且读不到 '{ref}/config.json'。\n"
            f"若用的是 HuggingFace 远程 id，请显式设置 EMBEDDING_DIMENSION。"
        )
    return detected or declared


def embedding_identity() -> str:
    """
    当前**向量空间**的标识，形如 `local:bge-small-zh-v1.5:512` / `api:qwen3.7-text-embedding:512`。

    ★ 为什么需要它（不变量 53 的最后一公里）

    「维度一样」**不等于**「同一个向量空间」。把 embedding 从 A 换成同维度的 B，
    Milvus 那边看不出任何异常：写入成功、检索有结果，只是**相似度全是噪声** ——
    因为库里旧向量的坐标是 A 给的，新查询向量是 B 给的，两者本就不在同一个空间里。
    这个失效形态没有任何报错，只会表现为「换了模型之后效果反而更差了」。

    也不能靠路径来判断：把模型目录从 `C:/models/...` 挪到 `D:/ml/...`，
    向量空间一点没变，不该因此要求重建 9 万个块。

    所以标识里放进三样东西：**后端 + 模型名 + 维度**；
    本地模型只取目录名（`bge-small-zh-v1.5`），不含盘符与父目录。
    它会被写进 Milvus Collection 的 description（见 `core/milvus_client.py`），
    于是「这个库是谁建的」由库自己回答 —— 而不是靠一份可能和库对不上的旁路记录。

    Raises:
        EmbeddingError: 维度无法确定时（与 `resolve_dimension()` 同一条件）
    """
    if backend_name() == "local":
        raw = settings.EMBEDDING_LOCAL_MODEL or ""
        label = Path(raw).name or raw
    else:
        label = settings.EMBEDDING_MODEL
    return f"{backend_name()}:{label}:{resolve_dimension()}"


# ---------------------------------------------------------------- 本地后端


class _LocalEmbedder:
    """
    进程内 sentence-transformers 后端。

    生命周期与 `rag/reranker.py::LocalCrossEncoderReranker` 一致：
    惰性加载 → 双检锁 → 线程池执行。
    """

    def __init__(self, ref: str, device: str) -> None:
        self.ref = ref
        self.device = device
        self._model: Any = None
        self._lock = asyncio.Lock()

    # -- 加载 --

    def _load_sync(self) -> Any:
        """同步加载权重（调用方负责放进线程）"""
        try:
            from sentence_transformers import SentenceTransformer  # 惰性导入
        except ImportError as e:  # pragma: no cover - 取决于是否装了可选依赖
            raise EmbeddingError(
                "本地 Embedding 后端需要 sentence-transformers（含 torch）：\n"
                '  pip install torch --index-url https://download.pytorch.org/whl/cpu\n'
                '  pip install "sentence-transformers>=2.7.0"\n'
                f"原始错误: {e}"
            ) from e

        logger.info("正在加载本地 Embedding 模型 %s（device=%s）…", self.ref, self.device)
        started = time.perf_counter()
        try:
            model = SentenceTransformer(self.ref, device=self.device)
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(
                f"本地 Embedding 模型加载失败：'{self.ref}'\n"
                f"请确认目录存在且包含 config.json / model.safetensors / tokenizer.json。\n"
                f"原始错误: {type(e).__name__}: {e}"
            ) from e

        model.max_seq_length = max(
            8, int(getattr(settings, "EMBEDDING_LOCAL_MAX_LENGTH", 512) or 512)
        )
        actual = _model_dimension(model)

        # 与配置对齐（配置错的话，宁可在这里炸，也不要建出一个维度不对的库）
        declared = int(getattr(settings, "EMBEDDING_DIMENSION", 0) or 0)
        if declared and actual and declared != actual:
            raise EmbeddingError(
                f"本地模型 '{self.ref}' 实际输出 {actual} 维，但 EMBEDDING_DIMENSION={declared}。"
                f"请把 .env 里的 EMBEDDING_DIMENSION 改成 {actual} 并重建 Milvus Collection。"
            )

        logger.info(
            "✓ 本地 Embedding 模型已加载: %s（%d 维，max_len=%d，耗时 %.1fs）",
            self.ref, actual, model.max_seq_length, time.perf_counter() - started,
        )
        return model

    async def ensure_model(self) -> Any:
        """拿到模型实例（首次调用会加载权重）"""
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    # to_thread：加载要 1~3 秒且吃 CPU，不能占着事件循环
                    self._model = await asyncio.to_thread(self._load_sync)
        return self._model

    # -- 推理 --

    @staticmethod
    def _apply_prefix(texts: List[str], kind: str) -> List[str]:
        """查询侧可选加指令前缀；入库侧永远不加（见模块 docstring 第一节）"""
        if kind != KIND_QUERY:
            return list(texts)
        prefix = getattr(settings, "EMBEDDING_LOCAL_QUERY_PREFIX", "") or ""
        return [prefix + t for t in texts] if prefix else list(texts)

    def encode_sync(self, texts: List[str], *, kind: str = KIND_PASSAGE) -> List[List[float]]:
        """
        同步向量化（供脚本与 ragas 适配器使用；Web 侧请用 `encode`）。

        自己负责触发加载，所以可以在没有事件循环的地方直接调用。
        """
        if not texts:
            return []
        if self._model is None:
            self._model = self._load_sync()

        batch = int(getattr(settings, "EMBEDDING_LOCAL_BATCH_SIZE", 32) or 32)
        vectors = self._model.encode(
            self._apply_prefix(texts, kind),
            batch_size=max(1, batch),
            normalize_embeddings=True,   # Milvus 用的是 COSINE，归一化后内积即余弦
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

    async def encode(self, texts: List[str], *, kind: str = KIND_PASSAGE) -> List[List[float]]:
        """异步向量化（推理过程放线程池）"""
        if not texts:
            return []
        await self.ensure_model()
        return await asyncio.to_thread(self.encode_sync, texts, kind=kind)


_local_embedder: _LocalEmbedder | None = None


def _model_dimension(model: Any) -> int:
    """
    读出已加载模型的向量维度。

    ⚠️ sentence-transformers 6.x 把 `get_sentence_embedding_dimension()` 改名成
    `get_embedding_dimension()`，旧名还在但会打 FutureWarning。
    两个都试一遍，这样装的是 2.x/3.x 还是 6.x 都能跑。
    """
    for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        fn = getattr(model, name, None)
        if callable(fn):
            try:
                return int(fn() or 0)
            except Exception as e:  # noqa: BLE001
                logger.warning("调用 %s() 失败: %s", name, e)
    return 0


def _get_local_embedder() -> _LocalEmbedder:
    """
    本地后端单例。

    模型名或设备一变就换新实例 —— 让「改配置后重新加载」不需要重启进程，
    测试里也就能直接 monkeypatch 配置。
    """
    global _local_embedder
    ref, device = settings.EMBEDDING_LOCAL_MODEL, settings.EMBEDDING_LOCAL_DEVICE
    if _local_embedder is None or _local_embedder.ref != ref or _local_embedder.device != device:
        _local_embedder = _LocalEmbedder(ref, device)
    return _local_embedder


def local_embedder_loaded() -> bool:
    """本地模型是否已经载入内存（真机验证 / 健康检查用）"""
    return _local_embedder is not None and _local_embedder._model is not None


def reset_embedding_backend() -> None:
    """丢弃本地模型实例（测试 / 切配置用；不释放已占用的显存）"""
    global _local_embedder
    _local_embedder = None


async def warmup_local_embedding() -> str | None:
    """
    预热本地模型（本地后端专用）。

    为什么值得单独做：权重加载要 1~3 秒且**必须**在用户第一条提问之前完成，
    否则那 1~3 秒会直接加在首字延迟上 —— 而这正是本项目最在意的一项指标。
    启动时后台预热，代价是启动后几秒多占一点 CPU，收益是首问不再付这笔钱。

    Returns:
        None 表示成功；否则返回失败原因（调用方只记日志，不影响启动）。
    """
    if backend_name() != "local":
        return None
    try:
        await _get_local_embedder().ensure_model()
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 外部 API 后端


def _is_batch_size_error(status_code: int, body: str) -> bool:
    """
    判断响应是否是「批大小超限」。

    只对 4xx 生效（服务端 5xx 折半重试没意义，反而放大故障），
    并且要求响应里出现批大小相关的字样，避免把「输入内容非法」
    （例如单条文本过长）误判成批大小问题 —— 后者折半也解决不了。
    """
    if not (400 <= status_code < 500):
        return False
    lowered = body.lower()
    keywords = ("batch size", "batch_size", "batchsize", "too many", "批大小", "batch limit")
    return any(k in lowered for k in keywords)


def _preferred_batch_size() -> int:
    size = int(getattr(settings, "EMBEDDING_MAX_BATCH", 20) or 20)
    return max(_MIN_BATCH, size)


async def _embed_batch(texts: List[str]) -> List[List[float]]:
    """单批向量化（外部 API 后端，内部使用，不做分批）"""
    headers = {
        "Authorization": f"Bearer {settings.EMBEDDING_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": settings.EMBEDDING_MODEL,
        "input": texts,
    }
    # 显式声明期望维度；不支持该参数的模型会忽略它，下面再统一校验
    if settings.EMBEDDING_DIMENSION:
        payload["dimensions"] = settings.EMBEDDING_DIMENSION

    url = settings.EMBEDDING_BASE_URL.rstrip("/") + "/embeddings"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise EmbeddingError(
            f"Embedding 网关连接失败（{settings.EMBEDDING_BASE_URL}）: {type(e).__name__}: {e}"
        ) from e

    if resp.status_code != 200:
        body = resp.text[:300]
        if _is_batch_size_error(resp.status_code, body):
            raise _BatchTooLarge(
                f"Embedding 网关拒绝批大小 {len(texts)}: {body}"
            )
        raise EmbeddingError(
            f"Embedding 调用失败 [HTTP {resp.status_code}] model={settings.EMBEDDING_MODEL} "
            f"batch={len(texts)} url={url} → {body}"
        )

    data = resp.json()
    # 按 index 排序确保与输入顺序一一对应
    items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
    if len(items) != len(texts):
        raise EmbeddingError(
            f"Embedding 返回条数不匹配：输入 {len(texts)} 条，返回 {len(items)} 条"
        )

    vectors = [item["embedding"] for item in items]

    # 校验维度 —— 在 Milvus 侧报错之前先给出能直接定位问题的信息
    actual_dim = len(vectors[0])
    if actual_dim != settings.EMBEDDING_DIMENSION:
        raise EmbeddingError(
            f"Embedding 维度不一致：模型 '{settings.EMBEDDING_MODEL}' 实际返回 {actual_dim} 维，"
            f"但 EMBEDDING_DIMENSION={settings.EMBEDDING_DIMENSION}。\n"
            f"请把 .env 里的 EMBEDDING_DIMENSION 改成 {actual_dim}，"
            f"或换一个支持自定义 dimensions 的模型（换完需要重建 Milvus Collection）。"
        )

    return vectors


async def _embed_via_api(texts: List[str]) -> List[List[float]]:
    """外部 API 后端的分批 + 折半重试"""
    vectors: List[List[float]] = []
    batch_size = _preferred_batch_size()

    start = 0
    while start < len(texts):
        # 每轮按当前 batch_size 切一批；若因超限折半，则从同一位置重试
        chunk = texts[start : start + batch_size]
        try:
            vectors.extend(await _embed_batch(chunk))
        except _BatchTooLarge:
            if batch_size <= _MIN_BATCH:
                # 已经退到单条仍被拒，说明不是批大小问题
                raise
            batch_size = max(_MIN_BATCH, batch_size // 2)
            logger.warning(
                "Embedding 网关拒绝批大小 %s，自动折半为 %s 后重试（同一条文本）",
                len(chunk), batch_size,
            )
            continue

        start += len(chunk)

    return vectors


# ---------------------------------------------------------------- 对外接口


async def get_embeddings(
    texts: List[str],
    *,
    kind: str = KIND_PASSAGE,
) -> List[List[float]]:
    """
    批量获取文本向量（**入库侧**语义，默认不加任何查询前缀）

    Args:
        texts: 待向量化的文本列表
        kind: `KIND_PASSAGE`（默认）或 `KIND_QUERY`。只有查询侧可能加指令前缀，
              入库侧永远不加 —— 详见模块 docstring 第一节。

    Returns:
        向量列表，每个向量维度 = `resolve_dimension()`

    Raises:
        EmbeddingError: 后端不可用 / 返回维度与配置不一致 / 缺少可选依赖
    """
    if not texts:
        return []
    if backend_name() == "local":
        return await _get_local_embedder().encode(texts, kind=kind)
    return await _embed_via_api(texts)


async def get_embedding(text: str) -> List[float]:
    """获取单条**查询**向量（会按配置加指令前缀）"""
    result = await get_embeddings([text], kind=KIND_QUERY)
    return result[0] if result else []


def get_embeddings_sync(
    texts: List[str],
    *,
    kind: str = KIND_PASSAGE,
) -> List[List[float]]:
    """
    同步批量向量化 —— **仅本地后端可用**。

    给两类调用方：① 没有事件循环的脚本；② ragas 等只吃同步接口的评估框架。
    API 后端不提供同步版本：那会要求把 `httpx.AsyncClient` 换成同步客户端，
    等于维护第二套请求路径，而收益只是省一个 `await`。
    """
    if not texts:
        return []
    if backend_name() != "local":
        raise EmbeddingError(
            "get_embeddings_sync() 仅支持本地后端（EMBEDDING_BACKEND=local）；"
            "外部 API 后端请使用异步的 get_embeddings()。"
        )
    return _get_local_embedder().encode_sync(texts, kind=kind)


def embedding_info() -> Dict[str, Any]:
    """
    Embedding 后端状态摘要（供 `/api/info` 与 `/api/health` 展示）。

    只读配置、不触发加载 —— 与 `reranker_status()` 同一约定：
    状态查询永远不该因为依赖很慢而变慢。
    """
    backend = backend_name()
    info: Dict[str, Any] = {
        "backend": backend,
        "model": effective_model_name(),
        "degraded_reason": None,
    }
    try:
        info["dimension"] = resolve_dimension()
    except Exception as e:  # noqa: BLE001
        info["dimension"] = None
        info["degraded_reason"] = f"{type(e).__name__}: {e}"

    if backend == "local":
        info["path"] = settings.EMBEDDING_LOCAL_MODEL
        info["device"] = settings.EMBEDDING_LOCAL_DEVICE
        info["loaded"] = local_embedder_loaded()
    return info
