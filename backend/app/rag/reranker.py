"""
Rerank 精排层 —— 混合检索的最后一道质量闸门

★ 为什么融合之后还需要精排

向量检索和 BM25 都是 **双塔（bi-encoder）** 结构：query 和 document
各自独立编码成向量，再算相似度。好处是文档向量可以离线预计算、检索快；
代价是 query 与 document 之间**没有任何交互**，细粒度的语义匹配会丢失。
RRF 融合只是把两路排名合并，并没有补上这个缺陷。

Rerank 用的是 **Cross-Encoder**：把 (query, document) 拼成一个序列
送进同一个模型，token 级注意力可以在 query 和文档之间自由流动，
因此能判断「这段文字到底回没回答这个问题」，而不只是「看起来像不像」。
代价是无法离线预计算，只能对少量候选（本项目取 Top-20）在线打分。

工程上的标准做法就是本项目这条链路：
    粗排（多路召回，追求高召回率） → 融合（RRF） → 精排（追求高准确率）

★ 可插拔后端

同一个 `RerankBackend` 接口下有三种实现，通过 `RERANK_BACKEND` 切换：

| 值      | 实现                      | 依赖                                 |
|---------|---------------------------|--------------------------------------|
| `api`   | `ApiReranker`             | 只要有网关密钥，零下载               |
| `local` | `LocalCrossEncoderReranker` | torch + sentence-transformers + 模型 |
| `none`  | `NoopReranker`            | 无                                   |

默认走 `api`：开箱可用，不必为了跑通演示先下载 2GB+ 模型。
想验证完整的本地 Cross-Encoder 链路时，装好依赖并把 `RERANK_BACKEND` 改成 `local` 即可，
**业务代码零改动** —— 与 LLM/Embedding 走 OpenAI 兼容接口的设计一致。

⚠️ 任何后端不可用都必须降级为「按 RRF 顺序返回」，绝不能让精排环节
   把整条检索链路打挂（精排是增益项，不是必需项）。

★ 送进精排的候选必须先截断（`RERANK_CANDIDATES`）

Cross-Encoder 的代价是每一对 (query, doc) 都要跑一次完整前向，无法预计算。
而 RRF 融合后的候选最多有 `HYBRID_VECTOR_TOP_K + HYBRID_BM25_TOP_K +
GRAPH_TOP_K` 条（默认 20+20+20=60）。全部送进精排有两个后果：

- `api` 后端：白花带宽与计费额度；
- `local` 后端：`bge-reranker-v2-m3` 是 568M 参数（XLM-R large 底座）的模型，
  60 对在 CPU 上要跑几十秒，比不精排还慢一个数量级。

所以「粗排多召回、精排只重排 Top-N」这条标准做法在本模块是**硬约束**：
`rerank_hits()` 内部会先按 `RERANK_CANDIDATES` 截断再做逐对打分。
调用点不必自己截 —— 放这里是为了让两个调用点（hybrid / 单路）都覆盖到。

★ 本地后端的关键工程点（与 `core/embedding_factory` 完全同一套约定）

1. **零成本预检**：只查目录与文件是否存在（不导入 torch），
   目录有问题的**在 `get_reranker()` 就拿不到 local 后端**，直接带着原因降级。
   不做这一步的话，「模型目录写错」要等到用户第一次提问才暴露，
   而且失败信息淹在每次检索的 warning 里 —— 属于典型的静默失效。
2. **惰性加载 + 双检锁**：权重在首次调用时才载入，不拖慢应用启动；
   应用启动另有后台预热（`main.py` Phase 3），首问不必付这笔钱。
3. **不阻塞事件循环**：`CrossEncoder.predict` 同步且 CPU 密集，
   必须走 `asyncio.to_thread`，否则表现为「精排期间所有请求都不响应」。
4. **批大小与线程数可配**：`RERANK_LOCAL_BATCH_SIZE` / `RERANK_LOCAL_THREADS`。
   注意 reranker 的输入是拼好的长序列，内存占用按 batch × 序列长 的平方级增长，
   默认批大小（8）刻意比 embedding 的（32）小。
5. **单例跟随配置变化重建**：模型名或设备一改就换新实例，
   让「改配置后重新加载」不需要重启进程，测试里也就能直接 monkeypatch。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# 本地模型目录里必须有这些东西才算「备好了」：
# - config.json          transformers 判断模型结构的入口
# - 权重文件             safetensors 或 pytorch 二选一
# 缺 config.json 时 transformers 会自以为在按远程 id 下载，报错指向网络，
# 而真实原因是「挂载目录没挂上」——所以这里提前分开报。
_LOCAL_REQUIRED = ("config.json",)
_LOCAL_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin", "model.safetensors.index.json")


def backend_name() -> str:
    """当前配置的精排后端（小写；未知值原样返回）"""
    return (settings.RERANK_BACKEND or "none").strip().lower()


def effective_rerank_model() -> str:
    """
    当前后端**实际在用**的模型名。

    与 `embedding_factory.effective_model_name()` 同一约定：
    健康检查报 `RERANK_MODEL` 会在本地后端下显示一个根本没被调用过的模型名
    （「报告与事实不符」正是这个项目一直在防的一类问题）。
    """
    if backend_name() == "local":
        return settings.RERANK_LOCAL_MODEL
    return settings.RERANK_MODEL


def local_model_problem(ref: str | None = None) -> str | None:
    """
    零成本预检本地模型目录，返回问题描述（None = 没问题）。

    **不导入 torch、不加载权重**，只做 `Path` 检查，所以可以放心放在启动路径上。

    ⚠️ 只对**绝对路径**下结论。`BAAI/bge-reranker-v2-m3` 这种 HuggingFace 远程 id
    在磁盘上当然找不到对应目录，但它可能是「有缓存」或「打算现下」，
    不是配置错误 —— 对不确定的事实不下断言，交给 transformers 自己处理。
    """
    target = ref if ref is not None else settings.RERANK_LOCAL_MODEL
    path = Path(target)
    if not path.is_absolute():
        # 相对路径同样无法判断意图（可能是 HF id，也可能是相对目录）
        return None
    if not path.is_dir():
        return f"本地精排模型目录不存在：{target}"
    missing = [name for name in _LOCAL_REQUIRED if not (path / name).is_file()]
    if missing:
        return f"本地精排模型目录 {target} 缺少 {'、'.join(missing)}"
    if not any((path / name).is_file() for name in _LOCAL_WEIGHT_FILES):
        return (
            f"本地精排模型目录 {target} 里找不到权重文件"
            f"（{' 或 '.join(_LOCAL_WEIGHT_FILES)}）"
        )
    return None


# ---------- 抽象接口 ----------
class RerankBackend(abc.ABC):
    """精排后端接口：输入 query 与候选文档，输出 (下标, 相关分)"""

    #: 后端标识（日志 / /api/health 展示用）
    name: str = "unknown"

    @abc.abstractmethod
    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """
        对候选文档重新打分。

        Args:
            query: 用户查询
            documents: 候选文档文本（顺序即输入顺序，返回值用 index 指回）
            top_k: 需要返回的条数

        Returns:
            [{"index": int, "score": float}, ...]，按 score 降序
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        """释放资源（默认无操作）"""
        return None


# ---------- 实现 1：不精排（降级 / 对照组） ----------
class NoopReranker(RerankBackend):
    """
    透传后端：保持输入顺序，分数置 0。

    两个用途：
    1. 配置 `RERANK_BACKEND=none` 时做 A/B 对照，直观看到精排带来的提升；
    2. 其他后端初始化失败时的兜底，保证链路不中断。
    """

    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    async def rerank(self, query, documents, top_k):  # noqa: ANN001, ANN201
        return [
            {"index": i, "score": 0.0}
            for i in range(min(top_k, len(documents)))
        ]


# ---------- 实现 2：远程 Rerank API（DashScope 原生接口） ----------
class ApiReranker(RerankBackend):
    """
    调用远程 Rerank 接口。

    DashScope 的重排能力不在 OpenAI 兼容路径下，接口为：
        POST {host}/api/v1/services/rerank/text-rerank/text-rerank
        {"model": ..., "input": {"query":..., "documents":[...]},
         "parameters": {"top_n": N, "return_documents": false}}
        → {"output": {"results": [{"index": 0, "relevance_score": 0.76}, ...]}}

    这里同时兼容 OpenAI 风格的 `{"results":[{"relevance_score":...}]}` 返回，
    换成 Jina / Cohere / 自建 rerank 服务时通常只需改 `RERANK_API_URL`。
    """

    name = "api"

    def __init__(self) -> None:
        self._url = settings.rerank_api_url
        self._key = settings.rerank_api_key
        self._model = settings.RERANK_MODEL
        if not self._url:
            raise RuntimeError("无法确定 Rerank 接口地址（请配置 RERANK_API_URL 或 LLM_BASE_URL）")

    async def rerank(self, query, documents, top_k):  # noqa: ANN001, ANN201
        payload = {
            "model": self._model,
            "input": {
                "query": query,
                "documents": list(documents),
            },
            "parameters": {
                "top_n": int(top_k),
                # 只要分数，不要回传原文（省带宽）
                "return_documents": False,
            },
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=settings.RERANK_TIMEOUT_SECONDS) as client:
            resp = await client.post(self._url, headers=headers, json=payload)

        if resp.status_code != 200:
            raise RuntimeError(
                f"Rerank 接口返回 HTTP {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        raw_results = (
            (data.get("output") or {}).get("results")
            or data.get("results")
            or []
        )

        results: List[Dict[str, Any]] = []
        for item in raw_results:
            score = item.get("relevance_score", item.get("score"))
            if score is None:
                continue
            results.append({"index": int(item.get("index", 0)), "score": float(score)})

        if not results:
            raise RuntimeError(f"Rerank 返回结果为空: {str(data)[:200]}")

        results.sort(key=lambda x: -x["score"])
        return results


# ---------- 实现 3：本地 BGE Cross-Encoder ----------
class LocalCrossEncoderReranker(RerankBackend):
    """
    本地 Cross-Encoder（默认 `BAAI/bge-reranker-base`；本项目实机跑
    `bge-reranker-v2-m3`）。

    与 `core/embedding_factory._LocalEmbedder` 完全同一套生命周期：
    惰性加载 → 双检锁 → 线程池执行，外加「配置变了就换新实例」。

    分数语义：`bge-reranker-*` 系列的 `num_labels` 为 1，sentence-transformers
    的 `get_default_activation_fn()` 会据此套上 `nn.Sigmoid()`，
    所以 `predict` 回来就是 (0, 1) 的相关性分，**不需要再手动归一化**。
    不要给它传 `activation_fn=nn.Identity()` —— 那会退化成原始 logit
    （可以是负数），而 `rerank_score` 会一路透传到前端。
    """

    name = "local"

    def __init__(self) -> None:
        self._model: Any = None
        self._lock = asyncio.Lock()
        self.ref = settings.RERANK_LOCAL_MODEL
        self.device = settings.RERANK_LOCAL_DEVICE

    def _load_sync(self) -> Any:
        """同步加载模型（在线程里执行）"""
        try:
            from sentence_transformers import CrossEncoder  # 惰性导入，缺依赖时在此处报错
        except ImportError as e:  # pragma: no cover - 取决于是否装了可选依赖
            raise RuntimeError(
                "本地精排后端需要 sentence-transformers（含 torch）：\n"
                '  pip install torch --index-url https://download.pytorch.org/whl/cpu\n'
                '  pip install "sentence-transformers>=2.7.0"\n'
                f"原始错误: {e}"
            ) from e

        # torch 线程数与 FastAPI 的线程池会互相抢核；只在显式配置时才动它
        threads = int(getattr(settings, "RERANK_LOCAL_THREADS", 0) or 0)
        if threads > 0:
            try:
                import torch

                torch.set_num_threads(threads)
                logger.info("已把 torch 推理线程数限制为 %d", threads)
            except Exception as e:  # noqa: BLE001
                logger.warning("设置 torch 线程数失败（忽略）: %s", e)

        logger.info("正在加载本地精排模型 %s（device=%s）…", self.ref, self.device)
        started = time.perf_counter()
        try:
            model = CrossEncoder(
                self.ref,
                device=self.device,
                max_length=settings.RERANK_LOCAL_MAX_LENGTH,
            )
        except Exception as e:  # noqa: BLE001
            # 到这一步说明目录预检已通过（或用的是远程 id），失败原因通常是
            # 权重损坏 / 远程不可达 —— 把路径带上，别让人猜是哪个模型。
            raise RuntimeError(
                f"本地精排模型加载失败：'{self.ref}'\n"
                f"原始错误: {type(e).__name__}: {e}"
            ) from e

        logger.info(
            "✓ 本地精排模型已加载: %s（max_len=%d，耗时 %.1fs）",
            self.ref, settings.RERANK_LOCAL_MAX_LENGTH, time.perf_counter() - started,
        )
        return model

    async def ensure_model(self) -> Any:
        """拿到模型实例（首次调用会加载权重）"""
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    self._model = await asyncio.to_thread(self._load_sync)
        return self._model

    async def rerank(self, query, documents, top_k):  # noqa: ANN001, ANN201
        model = await self.ensure_model()
        pairs = [(query, doc) for doc in documents]
        batch_size = max(1, int(getattr(settings, "RERANK_LOCAL_BATCH_SIZE", 8) or 8))

        # CrossEncoder.predict 同步且吃 CPU，必须放线程池
        scores = await asyncio.to_thread(
            model.predict, pairs, batch_size=batch_size
        )

        ranked = sorted(
            ({"index": i, "score": float(s)} for i, s in enumerate(scores)),
            key=lambda x: -x["score"],
        )
        return ranked[:top_k]


# ---------- 后端工厂（含降级） ----------
_CONSTRUCTORS = {
    "api": ApiReranker,
    "local": LocalCrossEncoderReranker,
    "none": NoopReranker,
}

_backend: RerankBackend | None = None
_backend_error: str | None = None
# 本地后端的「可复用的模型实例」跨 `reset_reranker()` 保留。
# 分开存的原因：`_backend` 是**后端对象**（含降级结果），`_local` 是**权重实例**。
# 权重加载要几十秒，测试里频繁 reset 后端不该把它一起丢掉。
_local_backend: LocalCrossEncoderReranker | None = None


def _get_local_backend() -> LocalCrossEncoderReranker:
    """
    本地后端单例（**不加载权重**，只持有实例）。

    模型名或设备一变就换新实例 —— 与 `embedding_factory._get_local_embedder()`
    同一约定：让「改配置后重新加载」不需要重启进程，测试里也就能直接 monkeypatch。
    """
    global _local_backend
    ref, device = settings.RERANK_LOCAL_MODEL, settings.RERANK_LOCAL_DEVICE
    if (
        _local_backend is None
        or _local_backend.ref != ref
        or _local_backend.device != device
    ):
        _local_backend = LocalCrossEncoderReranker()
    return _local_backend


def local_reranker_loaded() -> bool:
    """本地精排模型的权重是否已经载入内存（真机验证 / 健康检查用）"""
    return _local_backend is not None and _local_backend._model is not None


def get_reranker(force_reload: bool = False) -> RerankBackend:
    """
    获取精排后端单例。

    构造失败（配置错误 / 依赖缺失 / 模型目录不存在）不抛异常，而是降级为
    `NoopReranker` 并记录原因 —— 精排是增益项，不能成为链路的单点故障。

    但「降级」必须**看得见**：模型目录这类可以在毫秒内查清的问题，
    在这里就查清并写进 `reranker_status()["degraded_reason"]`，
    而不是拖到用户第一次提问时才在检索日志里冒一行 warning。
    """
    global _backend, _backend_error

    if _backend is not None and not force_reload:
        return _backend

    name = backend_name()

    if not settings.RERANK_ENABLED or name == "none":
        _backend = NoopReranker("未启用精排")
        _backend_error = None
        logger.info("精排未启用（RERANK_BACKEND=%s）", name)
        return _backend

    if name not in _CONSTRUCTORS:
        _backend_error = f"未知的 RERANK_BACKEND: {name}"
        _backend = NoopReranker(_backend_error)
        logger.warning("%s，已降级为不精排", _backend_error)
        return _backend

    # 本地后端：先做零成本目录预检（缺目录 / 缺权重都在这里拦下来）
    if name == "local":
        problem = local_model_problem()
        if problem:
            _backend_error = problem
            _backend = NoopReranker(problem)
            logger.warning(
                "✗ %s —— 已降级为不精排（按 RRF 顺序返回）。"
                "请检查 RERANK_LOCAL_MODEL 指向的目录，或改用 RERANK_BACKEND=api。",
                problem,
            )
            return _backend
        _backend = _get_local_backend()
        _backend_error = None
        logger.info("✓ 精排后端就绪: local (%s, device=%s)",
                    settings.RERANK_LOCAL_MODEL, settings.RERANK_LOCAL_DEVICE)
        return _backend

    try:
        _backend = _CONSTRUCTORS[name]()
        _backend_error = None
        logger.info("✓ 精排后端就绪: %s (%s)", _backend.name, effective_rerank_model())
    except Exception as e:  # noqa: BLE001
        _backend_error = f"{type(e).__name__}: {e}"
        _backend = NoopReranker(_backend_error)
        logger.warning("精排后端 '%s' 初始化失败，降级为不精排: %s", name, e)

    return _backend


def reset_reranker() -> None:
    """
    清空后端单例与本地模型实例（测试 / 切配置用）。

    与 `embedding_factory.reset_embedding_backend()` 同一语义：**连权重一起丢**。
    留一个「后端没了但权重还在」的中间态只会让测试之间互相串味
    （上一条用例的假模型被下一条用例捡回去用），得不偿失。
    """
    global _backend, _backend_error, _local_backend
    _backend = None
    _backend_error = None
    _local_backend = None


def reranker_status() -> Dict[str, Any]:
    """精排状态摘要（供 /api/health 展示）"""
    backend = _backend
    name = backend_name()
    info: Dict[str, Any] = {
        "enabled": bool(settings.RERANK_ENABLED),
        "configured": settings.RERANK_BACKEND,
        "active": backend.name if backend else "not_loaded",
        "model": effective_rerank_model(),
        "candidates": int(getattr(settings, "RERANK_CANDIDATES", 20) or 20),
        "top_n": settings.RERANK_TOP_N,
        "degraded_reason": _backend_error,
    }
    if name == "local":
        # 与 embedding 的 info 同一分工：本地后端额外报路径/设备/是否已载入内存，
        # 排查「到底加载的是哪个模型、加载成功没有」时不必去翻启动日志。
        info["path"] = settings.RERANK_LOCAL_MODEL
        info["device"] = settings.RERANK_LOCAL_DEVICE
        info["loaded"] = local_reranker_loaded()
    return info


async def warmup_local_reranker() -> str | None:
    """
    预热本地精排模型（本地后端专用）。

    为什么值得单独做：`bge-reranker-v2-m3` 的权重是 2.27GB（fp32），
    冷载（含 import torch）实测要好几秒到十几秒。压在哪都不合适：

    - 压在启动路径上 → 后端晚十几秒才能服务；
    - 压在第一次提问上 → 用户等十几秒，而这正是本项目最在意的首字延迟。

    所以投成后台任务：启动不受影响，首问时模型通常已经就绪。

    Returns:
        None 表示成功（或当前不是 local 后端 / 已降级，无需预热）；
        否则返回失败原因字符串 —— 与 `spawn_background` 的约定一致。
    """
    if backend_name() != "local" or not settings.RERANK_ENABLED:
        return None
    backend = get_reranker()
    if not isinstance(backend, LocalCrossEncoderReranker):
        # 目录预检没过 → 已降级为 Noop，预热没有意义（原因已由 status 上报）
        return None
    try:
        await backend.ensure_model()
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


# ---------- 高层封装：对「检索结果列表」做精排 ----------
async def rerank_hits(
    query: str,
    hits: List[Dict[str, Any]],
    *,
    top_k: int | None = None,
    backend: RerankBackend | None = None,
) -> List[Dict[str, Any]]:
    """
    对融合后的候选做精排，返回重排后的 Top-K。

    ★ 候选截断在这里做，调用点不必自己截

    融合结果最多有 VECTOR_TOP_K + BM25_TOP_K + GRAPH_TOP_K 条（默认 60），
    全送进 Cross-Encoder 既慢（本地是 568M 参数的模型）又费钱（api 是按量计费）。
    所以先按 `RERANK_CANDIDATES`（默认 20）截断再打分 —— 详见模块 docstring。

    Args:
        query: 用户查询
        hits: 融合后的候选（需含 `text` 字段）
        top_k: 返回条数，默认 `settings.RERANK_TOP_N`
        backend: 显式指定后端（测试用），缺省取全局单例

    Returns:
        重排后的结果列表；每条新增 `rerank_score` 与 `rerank_rank`。
        精排不可用 / 调用失败时，原样返回（截断到 top_k），不抛异常。
    """
    limit = top_k or settings.RERANK_TOP_N
    if not hits:
        return []

    backend = backend or get_reranker()

    if isinstance(backend, NoopReranker):
        return hits[:limit]

    # 截断下限用 limit：RERANK_CANDIDATES 配得比 RERANK_TOP_N 还小时，
    # 不能反而让「要 5 条」变成「拿不到 5 条」。
    candidate_limit = max(limit, int(getattr(settings, "RERANK_CANDIDATES", 20) or 20))
    candidates = hits[:candidate_limit]
    if len(candidates) < len(hits):
        logger.debug(
            "精排候选截断：%d → %d 条（RERANK_CANDIDATES=%d）",
            len(hits), len(candidates), candidate_limit,
        )

    documents = [str(h.get("text", "")) for h in candidates]
    try:
        ranked = await backend.rerank(query, documents, limit)
    except Exception as e:  # noqa: BLE001
        # 精排失败只降级本次调用，不影响检索结果本身
        logger.warning("精排调用失败，按融合顺序返回: %s", e)
        return candidates[:limit]

    out: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for rank, item in enumerate(ranked, start=1):
        idx = int(item.get("index", -1))
        if idx < 0 or idx >= len(candidates) or idx in seen:
            continue
        seen.add(idx)
        enriched = {**candidates[idx]}
        enriched["rerank_score"] = round(float(item.get("score", 0.0)), 6)
        enriched["rerank_rank"] = rank
        out.append(enriched)

    # 兜底：后端返回条数少于请求条数时，用剩余候选按融合顺序补齐，
    # 保证「要 5 条就一定有 5 条」（知识库条数不足时除外）
    if len(out) < limit:
        for idx, hit in enumerate(candidates):
            if len(out) >= limit:
                break
            if idx in seen:
                continue
            out.append({**hit, "rerank_score": None, "rerank_rank": None})

    return out[:limit]


__all__ = [
    "RerankBackend",
    "NoopReranker",
    "ApiReranker",
    "LocalCrossEncoderReranker",
    "backend_name",
    "effective_rerank_model",
    "local_model_problem",
    "local_reranker_loaded",
    "get_reranker",
    "reset_reranker",
    "reranker_status",
    "warmup_local_reranker",
    "rerank_hits",
]
