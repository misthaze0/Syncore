"""
检索/精排模型的运行时设置 —— 查看、切换、自检、重建向量库

★ 为什么单独一层，而不是让接口直接改 `settings`

改模型的**动作**只有一行（`setattr`），但改完必须同时回答三个问题，缺一个就会出事：

1. **这次改动影响谁？** —— 单例缓存（`_local_embedder` / `_local_reranker`）还抱着旧模型，
   不重置就「改了配置没生效」；而且还抱着旧**权重**，两个模型同时在内存里。
2. **向量库还能用吗？** —— 这是本项目最危险的一处：embedding 一换，向量空间就换了。
   维度不同 → Milvus 会报错（好发现）；**维度相同** → 完全不报错，
   只是库里的旧向量与新查询向量不在一个空间，相似度变成噪声（不变量 53）。
   判定要落到「库自己登记的产出模型」上（见 `core/milvus_client.py::stored_identity`）。
3. **配置真的能跑吗？** —— 目录写错、网关不认这个模型名，都要**在切换的那一刻**
   说清楚，而不是等下一次提问在检索日志里冒一行 warning。

★ 「重建」为什么是显式动作，而不是切换的副作用

重建 = 把库里所有块用新模型重新向量化一遍。它要花钱（api 后端）、要几十秒到几分钟
（本地后端更久），而且**先清空再重灌**。自动做等于「点一下下拉框，静默花掉一轮 token
并且中途失败可能只剩半份数据」。项目里所有主动产生成本的动作都是显式入口
（索引监听、图构建），这里保持一致。

★ 重建为什么先转储再清空

清空之后、灌完之前，库里是**半份**数据 —— 而它看起来是好的（标识对、检索有结果、
只是少了一部分块），这是最难发现的一类损坏。所以先把块正文落一份 JSONL，
失败时重跑就是幂等续建（主键是确定性的，upstert 覆盖同一批 chunk_id）。
转储只在中途存在，成功即删。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from app.config import (
    RUNTIME_OVERRIDE_SPEC,
    apply_persisted_overrides,
    load_runtime_overrides,
    override_status,
    remove_runtime_overrides,
    save_runtime_overrides,
    settings,
)
from app.core.embedding_factory import (
    KIND_PASSAGE,
    KIND_QUERY,
    effective_model_name,
    embedding_identity,
    embedding_info,
    get_embeddings,
    local_embedder_loaded,
    local_model_dimension,
    reset_embedding_backend,
    warmup_local_embedding,
)
from app.core.milvus_client import milvus_manager
from app.rag.reranker import (
    effective_rerank_model,
    local_model_problem,
    reset_reranker,
    reranker_status,
    rerank_hits,
    warmup_local_reranker,
)

logger = logging.getLogger(__name__)

VALID_EMBEDDING_BACKENDS = ("api", "local")
VALID_RERANK_BACKENDS = ("api", "local", "none")

#: 改动这些键要重算 embedding 侧（单例失效 / 预热 / 一致性判定）
#: `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` 是 api 后端的供应商接入 ——
#: 客户端缓存里捏着这两样，改了就必须重建客户端。
_EMBEDDING_KEYS = {
    "EMBEDDING_BACKEND", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY",
    "EMBEDDING_LOCAL_MODEL", "EMBEDDING_LOCAL_DEVICE", "EMBEDDING_DIMENSION",
}
#: 改动这些键要重算精排侧（`RERANK_API_URL` / `RERANK_API_KEY` 同上）
_RERANK_KEYS = {
    "RERANK_ENABLED", "RERANK_BACKEND", "RERANK_MODEL", "RERANK_API_URL", "RERANK_API_KEY",
    "RERANK_LOCAL_MODEL", "RERANK_LOCAL_DEVICE", "RERANK_TOP_N", "RERANK_CANDIDATES",
}
#: 检索域的键集 —— 面板上报「覆写了几项」、以及「恢复默认」都只看这一组。
#:
#: 覆写文件是两个前端入口共用的（语言模型 / 检索模型），按域收窄之后，
#: 在这一页点「恢复默认」不会顺手把用户换的语言模型也带回 `.env`。
_RAG_KEYS = frozenset(_EMBEDDING_KEYS | _RERANK_KEYS)

# 重新向量化时的分批大小。它只影响进度粒度与内存峰值 ——
# 真正的网络批大小由 embedding 后端自己管（api 会按网关上限折半重试）。
_REBUILD_BATCH = 64
# 转储/回灌时单次向 Milvus 取的条数
_DUMP_BATCH = 1000

# 后台预热任务的强引用（`asyncio.create_task` 只持弱引用，会被 GC 掉）
_warmup_tasks: set["asyncio.Task[Any]"] = set()


class RagModelSettingsError(ValueError):
    """切换参数不合法（接口层转成 400，消息直接给人看）"""


# ---------------------------------------------------------------- 本地模型目录


def _local_model_roots() -> List[Path]:
    """
    扫描本地模型的根目录。

    优先用显式配置的 `LOCAL_MODEL_DIR`；没配就从当前两个本地模型路径**推断公共父目录**
    （本机两个模型都在 `B:/model` 下，于是不必为了一个候选清单多记一个路径）。
    推断出来的目录必须真实存在才返回 —— 猜错了不如不猜。
    """
    roots: List[Path] = []
    if settings.LOCAL_MODEL_DIR:
        p = Path(settings.LOCAL_MODEL_DIR)
        if p.is_dir():
            roots.append(p)
        return roots

    for ref in (settings.EMBEDDING_LOCAL_MODEL, settings.RERANK_LOCAL_MODEL):
        parent = Path(ref).parent
        if str(parent) in ("", "."):
            continue
        if parent.is_dir() and parent not in roots:
            roots.append(parent)
    return roots


def _classify_local_model(config: Dict[str, Any]) -> str:
    """
    从一个模型的 `config.json` 判断它是「向量模型」还是「精排模型」。

    依据是 `architectures`：精排是一个**分类头**（`...ForSequenceClassification`），
    向量模型输出的是句向量（`BertModel` 这类裸骨架）。
    这个区分对界面很重要 —— 把 reranker 填到 `EMBEDDING_LOCAL_MODEL` 上，
    sentence-transformers 会加载成功然后在 pooling 处失败，错误信息完全不指向原因。
    """
    archs = config.get("architectures") or []
    if isinstance(archs, str):
        archs = [archs]
    joined = " ".join(str(a) for a in archs)
    if "ForSequenceClassification" in joined or "ForTokenClassification" in joined:
        return "rerank"
    if "Model" in joined:
        return "embedding"
    return "unknown"


def scan_local_models() -> List[Dict[str, Any]]:
    """
    列出本机已下载的本地模型（供界面直接挑选）。

    只读 `config.json`（毫秒级、不导入 torch），所以可以在每次打开面板时实时扫。
    只认「目录里有 config.json」的**一级子目录** —— 那正是
    `sentence-transformers` 能加载的形态，其余目录列出来只会误导。
    """
    found: List[Dict[str, Any]] = []
    for root in _local_model_roots():
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError as e:
            logger.warning("扫描本地模型目录失败: %s → %s", root, e)
            continue
        for child in children:
            cfg_path = child / "config.json"
            if not cfg_path.is_file():
                continue
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                logger.warning("读取 %s 失败，已跳过: %s", cfg_path, e)
                continue
            size_mb = None
            weights = child / "model.safetensors"
            if weights.is_file():
                try:
                    size_mb = round(weights.stat().st_size / 1e6)
                except OSError:
                    size_mb = None
            found.append({
                "path": str(child),
                "name": child.name,
                "kind": _classify_local_model(cfg),
                "dimension": int(cfg.get("hidden_size") or 0) or None,
                "model_type": cfg.get("model_type"),
                "weights_mb": size_mb,
                "root": str(root),
            })
    return found


# ---------------------------------------------------------------- 状态


def _override_block() -> Dict[str, Any]:
    """
    运行时覆写的现状（检索域视角）。

    实现移到 `config.override_status()`：覆写文件属于配置层，两个域的「现状」
    必须是同一种形状，否则前端要记住两套字段。这里只收窄键集。
    """
    return override_status(_RAG_KEYS)


def state() -> Dict[str, Any]:
    """
    面板需要的全部事实：当前生效配置 + 候选 + 向量库一致性。

    ★ `config` 与 `embedding` / `rerank` 是两种不同的东西，都需要

    - `config` 是**生效的配置项**（两个后端各自的模型名都在，不管当前用哪个）。
      表单要填的是它 —— 切到 local 时得知道 `EMBEDDING_LOCAL_MODEL` 配的是什么，
      而 `embedding_info()` 只在 backend=local 时才带 `path`。
    - `embedding` / `rerank` 是**运行状态**：实际在用哪个、维度多少、加载没有、降级没有。

    把两者混在一起，就会出现「界面上切到 local 时模型名一栏是空的」这类事。

    只读、不做任何加载 —— 与 `embedding_info()` 同一约定。
    唯一可能慢的是 `vector_space_state()`（要问一次 Milvus），所以它的失败
    被收敛成 `state: unknown` 而不是抛异常：Milvus 没起时面板**仍然要能打开**，
    否则「换个模型」这件事会在最需要它的时候（环境不全）用不了。
    """
    emb = embedding_info()
    emb["loaded"] = local_embedder_loaded()
    emb["local_model_problem"] = _embedding_local_problem(emb["backend"])

    rrk = reranker_status()
    if rrk.get("configured") == "local":
        rrk["local_model_problem"] = local_model_problem()

    return {
        "config": {k: getattr(settings, k, None) for k in sorted(_RAG_KEYS)},
        "embedding": emb,
        "rerank": rrk,
        "vector_store": milvus_manager.vector_space_state(),
        "local_models": scan_local_models(),
        "store": _override_block(),
    }


def _embedding_local_problem(backend: str, ref: str | None = None) -> str | None:
    """
    本地向量模型目录的零成本预检（与精排的 `local_model_problem` 同一职责）。

    `ref` 显式传入是为了让「即将切换到的那个目录」也能被检查 ——
    读 `settings` 只能看到**当前**值，而校验的恰恰是改完之后的样子。
    """
    if backend != "local":
        return None
    target = ref if ref is not None else settings.EMBEDDING_LOCAL_MODEL
    path = Path(target)
    # 只对本地路径下结论：`BAAI/xxx` 是 HF 远程 id，磁盘上没有属正常
    if not (path.is_absolute() or str(target).startswith(".")):
        return None
    if local_model_dimension(str(target)):
        return None
    if not path.is_dir():
        return f"本地向量模型目录不存在：{target}"
    return f"目录里没有 config.json（或缺少 hidden_size）：{target}"


# ---------------------------------------------------------------- 切换


def _coerce_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    按**检索域**白名单强转类型（`bool` 单独处理，见 `config._coerce` 的注释）。

    ⚠️ 只认 `_RAG_KEYS`，不是 `RUNTIME_OVERRIDE_SPEC` 全量：`LLM_MODEL` 也是
    合法的覆写键，但它属于「语言模型」面板 —— 这个接口接受它，就等于两个域
    各有一条通往同一把钥匙的路，改错了也不知道是哪一页干的。
    跨界键按「不支持」拒绝，错误消息里列的是本域可改项。
    """
    from app.config import _coerce

    out: Dict[str, Any] = {}
    for key, raw in patch.items():
        want = RUNTIME_OVERRIDE_SPEC.get(key) if key in _RAG_KEYS else None
        if want is None:
            raise RagModelSettingsError(
                f"不支持在运行时修改 '{key}'。可改的项：{', '.join(sorted(_RAG_KEYS))}"
            )
        try:
            out[key] = _coerce(raw, want)
        except (TypeError, ValueError) as e:
            raise RagModelSettingsError(f"'{key}' 的值不合法: {e}") from e
    return out


def _validate(effective: Dict[str, Any]) -> List[str]:
    """
    语义校验（不合法直接抛；可疑但合法只返回警告）。

    `effective` 是**当前生效值 + 本次改动**合并后的视图，不是「本次改了什么」——
    校验要看的是「改完之后这套配置合不合法」，比如把候选数从 20 改成 3
    而 `RERANK_TOP_N` 一直是 5，两者一起看才判得出问题。

    「警告」与「拒绝」的分界：**能不能判断对错**。
    「模型目录还不存在」是可疑但合法（用户可能正要下载）；「后端填了 'llm'」是确定错的。
    """
    warnings: List[str] = []

    backend = str(effective.get("EMBEDDING_BACKEND", "")).strip().lower()
    if backend not in VALID_EMBEDDING_BACKENDS:
        raise RagModelSettingsError(
            f"EMBEDDING_BACKEND 只能是 {' / '.join(VALID_EMBEDDING_BACKENDS)}，收到 '{backend}'"
        )
    rrk = str(effective.get("RERANK_BACKEND", "")).strip().lower()
    if rrk not in VALID_RERANK_BACKENDS:
        raise RagModelSettingsError(
            f"RERANK_BACKEND 只能是 {' / '.join(VALID_RERANK_BACKENDS)}，收到 '{rrk}'"
        )

    dim = int(effective.get("EMBEDDING_DIMENSION", 0) or 0)
    if not 1 <= dim <= 65536:
        raise RagModelSettingsError(f"EMBEDDING_DIMENSION 必须在 1~65536 之间，收到 {dim}")

    for key, lo, hi in (("RERANK_TOP_N", 1, 50), ("RERANK_CANDIDATES", 1, 200)):
        val = int(effective.get(key, 0) or 0)
        if not lo <= val <= hi:
            raise RagModelSettingsError(f"{key} 必须在 {lo}~{hi} 之间，收到 {val}")
    if int(effective.get("RERANK_CANDIDATES") or 0) < int(effective.get("RERANK_TOP_N") or 0):
        warnings.append(
            f"送进精排的候选数（{effective.get('RERANK_CANDIDATES')}）小于最终保留条数"
            f"（{effective.get('RERANK_TOP_N')}）—— 实际会按候选数上限返回。"
        )

    for key in ("EMBEDDING_MODEL", "RERANK_MODEL", "EMBEDDING_LOCAL_MODEL", "RERANK_LOCAL_MODEL"):
        value = str(effective.get(key, "")).strip()
        if not value:
            raise RagModelSettingsError(f"{key} 不能为空")
        if len(value) > 300:
            raise RagModelSettingsError(f"{key} 过长（>300 字符）")

    # api 供应商接入：允许留空（= 沿用 .env / 当前值），但填了就必须是合法形态
    for key in ("EMBEDDING_BASE_URL", "RERANK_API_URL"):
        value = str(effective.get(key, "") or "").strip()
        if value and not value.startswith(("http://", "https://")):
            raise RagModelSettingsError(f"{key} 必须以 http:// 或 https:// 开头：{value}")
    for key in ("EMBEDDING_API_KEY", "RERANK_API_KEY"):
        value = str(effective.get(key, "") or "").strip()
        if len(value) > 300:
            raise RagModelSettingsError(f"{key} 过长（>300 字符）")

    for key in ("EMBEDDING_LOCAL_DEVICE", "RERANK_LOCAL_DEVICE"):
        if not str(effective.get(key, "")).strip():
            raise RagModelSettingsError(f"{key} 不能为空")

    if backend == "local":
        problem = _embedding_local_problem(
            "local", str(effective.get("EMBEDDING_LOCAL_MODEL", ""))
        )
        if problem:
            warnings.append(
                f"{problem} —— 加载会失败，届时向量召回整条路不可用"
                f"（检索退化为 BM25 + 图路，不报错）。"
            )
        if not local_model_dimension(str(effective.get("EMBEDDING_LOCAL_MODEL", ""))):
            warnings.append(
                f"读不到 '{effective.get('EMBEDDING_LOCAL_MODEL')}' 的向量维度"
                f"（无 config.json 或用了 HF 远程 id），将沿用声明的 {dim} 维。"
            )

    if rrk == "local":
        problem = local_model_problem(str(effective.get("RERANK_LOCAL_MODEL", "")).strip())
        if problem:
            warnings.append(f"{problem} —— 精排会降级为按融合顺序返回。")

    return warnings


def _apply(values: Dict[str, Any]) -> None:
    """
    把覆写文件重新算成生效配置，并**让缓存失效**。

    ★ 单例失效这一步漏了会怎样

    `embedding_factory._local_embedder` / `reranker._local_backend` 都按
    「模型名 + 设备」缓存实例。不重置的话：切了模型，服务继续用旧实例 → 改了等于没改；
    而把设备从 cpu 换成 cuda 时，还会同时把两份权重都留在内存里。
    """
    before = (
        settings.EMBEDDING_BACKEND,
        effective_model_name(),
        settings.EMBEDDING_LOCAL_MODEL,
        settings.EMBEDDING_LOCAL_DEVICE,
    )
    before_rrk = (
        settings.RERANK_ENABLED,
        settings.RERANK_BACKEND,
        effective_rerank_model(),
        settings.RERANK_LOCAL_MODEL,
        settings.RERANK_LOCAL_DEVICE,
    )

    applied = apply_persisted_overrides()

    after = (
        settings.EMBEDDING_BACKEND,
        effective_model_name(),
        settings.EMBEDDING_LOCAL_MODEL,
        settings.EMBEDDING_LOCAL_DEVICE,
    )
    after_rrk = (
        settings.RERANK_ENABLED,
        settings.RERANK_BACKEND,
        effective_rerank_model(),
        settings.RERANK_LOCAL_MODEL,
        settings.RERANK_LOCAL_DEVICE,
    )

    if before != after:
        reset_embedding_backend()
        logger.info("Embedding 配置已切换: %s → %s", before, after)
    if before_rrk != after_rrk:
        reset_reranker()
        logger.info("精排配置已切换: %s → %s", before_rrk, after_rrk)

    logger.info("运行时覆写现有 %d 项: %s", len(applied), sorted(applied))


def _warm_up_after_switch(embedding_changed: bool, rerank_changed: bool) -> None:
    """
    切到本地后端后，把权重加载放到后台。

    不这么做的话，那笔「首次加载」会直接压在用户的下一次提问上 ——
    而它本来是可以提前付掉的（与启动预热同一取舍，不变量 52）。

    ⚠️ 没有事件循环时必须**安静地跳过**，而不是抛错：
    `update()` 是个同步函数，脚本与单测都可能直接调它。
    预热只是体验优化，不是配置生效的一部分 —— 权重在真正用到时会懒加载。
    """
    want_embedding = embedding_changed and settings.EMBEDDING_BACKEND == "local"
    want_rerank = (
        rerank_changed and settings.RERANK_ENABLED and settings.RERANK_BACKEND == "local"
    )
    if not (want_embedding or want_rerank):
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.info("当前没有事件循环，跳过切换后的预热（权重会在首次用到时懒加载）")
        return

    async def _run() -> None:
        # 协程对象在**循环内**才创建：先造出来再发现没循环，会留下
        # "coroutine was never awaited" 的 RuntimeWarning（噪音，且掩盖真问题）。
        jobs: List[tuple[str, Any]] = []
        if want_embedding:
            jobs.append(("本地 Embedding 模型", warmup_local_embedding))
        if want_rerank:
            jobs.append(("本地精排模型", warmup_local_reranker))
        # 串行：两个 GB 级模型同时读盘只会两边都慢（与启动预热同一理由）
        for label, fn in jobs:
            try:
                reason = await fn()
                if reason:
                    logger.warning("✗ %s预热未完成: %s", label, reason)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s预热异常（不影响配置生效）: %s", label, e)

    task = loop.create_task(_run())
    _warmup_tasks.add(task)
    task.add_done_callback(_warmup_tasks.discard)
    logger.info("已在后台预热切换后的本地模型（不阻塞本次请求）")


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    部分更新检索/精排配置：校验 → 落盘 → 生效 → 重新算状态。

    ★ 两个视图，别混用

    - `persisted`：写进覆写文件的**最小集合**（已有的覆写 + 本次改动）。
      只写用户显式改过的键，所以 `.env` 里没被动过的项仍然跟随 `.env` ——
      否则「我改了 .env 怎么没生效」会变成一个新的坑。
    - `effective`：当前生效值 + 本次改动。**校验看的必须是这个** ——
      校验要回答的是「改完之后这套配置合不合法」，而不是「你改了哪几个键」。
    """
    patch = {k: v for k, v in patch.items() if v is not None}
    if not patch:
        raise RagModelSettingsError("没有需要修改的配置项")

    coerced = _coerce_patch(patch)
    persisted = {**load_runtime_overrides(), **coerced}
    effective = {k: getattr(settings, k, None) for k in _RAG_KEYS}
    effective.update(coerced)

    # 切到本地后端时把维度**自动**填上：这个数能从模型的 config.json 直接读出来，
    # 让用户手填一个能自动算出来的数字，只会制造一个可能填错的输入框
    # （而填错的后果是建出一个维度不对的库 —— 不报错，只是向量检索永远返回空）。
    auto_filled: Dict[str, Any] = {}
    if (
        str(effective.get("EMBEDDING_BACKEND", "")).lower() == "local"
        and "EMBEDDING_DIMENSION" not in coerced
    ):
        detected = local_model_dimension(str(effective.get("EMBEDDING_LOCAL_MODEL", "")))
        if detected:
            effective["EMBEDDING_DIMENSION"] = detected
            persisted["EMBEDDING_DIMENSION"] = detected
            auto_filled["EMBEDDING_DIMENSION"] = detected

    warnings = _validate(effective)
    save_runtime_overrides(persisted)

    _apply(persisted)
    _warm_up_after_switch(
        embedding_changed=bool(set(coerced) & _EMBEDDING_KEYS),
        rerank_changed=bool(set(coerced) & _RERANK_KEYS),
    )

    result = state()
    # `applied` 的契约是「本次**实际写入**的键」，不是「你传了什么」——
    # 自动回填的维度也是一次真实写入，漏掉它会让界面少报一项改动。
    result["applied"] = sorted({*coerced, *auto_filled})
    result["warnings"] = warnings
    return result


def reset() -> Dict[str, Any]:
    """
    清空**检索域**的运行时覆写 → 回到 `backend/.env` 的配置（并让缓存失效）。

    ⚠️ 只清检索键，不动语言模型的覆写（`remove_runtime_overrides(_RAG_KEYS)`）——
    覆写文件是两个面板共用的，在这里点一下不该顺手改掉另一页的设置。
    要全部回到 `.env`，删掉 `data/runtime_config.json` 即可。
    """
    remove_runtime_overrides(_RAG_KEYS)
    _apply({})
    result = state()
    result["applied"] = []
    result["warnings"] = []
    return result


# ---------------------------------------------------------------- 自检


async def probe(timeout: float = 120.0) -> Dict[str, Any]:
    """
    用**当前配置**真打一次向量化与精排，回报实测事实。

    ★ 为什么值得做一个「自检」按钮

    「配置写进去了」和「配置能跑」是两件事，而后者只能靠真调一次来证明。
    面板上「已切换到 bge-small-zh-v1.5」是前者；「512 维，31ms」是后者。
    本项目在别处也有同样的出口（人设的 prompt-fragment、记忆的 prompt-block），
    只是这里的答案必须来自一次真实的调用。

    失败不抛异常：**失败本身就是最有用的信息**，原样返回给界面。
    """
    out: Dict[str, Any] = {}

    started = time.perf_counter()
    try:
        vectors = await asyncio.wait_for(
            get_embeddings(["Syncore：检索链路自检"], kind=KIND_QUERY), timeout=timeout
        )
        dim = len(vectors[0]) if vectors else 0
        declared = int(getattr(settings, "EMBEDDING_DIMENSION", 0) or 0)
        out["embedding"] = {
            "ok": True,
            "backend": settings.EMBEDDING_BACKEND,
            "model": effective_model_name(),
            "dimension": dim,
            "declared_dimension": declared,
            "dimension_matches": dim == declared,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as e:  # noqa: BLE001
        out["embedding"] = {
            "ok": False,
            "backend": settings.EMBEDDING_BACKEND,
            "model": effective_model_name(),
            "error": f"{type(e).__name__}: {e}",
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }

    started = time.perf_counter()
    try:
        hits = [
            {"text": "Syncore 是一个本地优先的桌面 AI 助手，支持混合检索。"},
            {"text": "今天中午食堂的番茄炒蛋有点咸。"},
        ]
        ranked = await asyncio.wait_for(
            rerank_hits("Syncore 支持什么检索方式？", hits, top_k=2), timeout=timeout
        )
        scored = [h for h in ranked if h.get("rerank_score") is not None]
        out["rerank"] = {
            "ok": bool(scored),
            "backend": settings.RERANK_BACKEND,
            "model": effective_rerank_model(),
            "scores": [h.get("rerank_score") for h in ranked],
            # 区分度：相关句与无关句的分数差。两者接近说明这个精排模型在这个
            # 场景上没在做事（很可能被降级成了 Noop，或模型与语料不匹配）。
            "gap": (
                round(float(scored[0]["rerank_score"]) - float(scored[-1]["rerank_score"]), 4)
                if len(scored) >= 2 else None
            ),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }
        if not scored:
            out["rerank"]["error"] = "未取到精排分数（已降级为按融合顺序返回？）"
    except Exception as e:  # noqa: BLE001
        out["rerank"] = {
            "ok": False,
            "backend": settings.RERANK_BACKEND,
            "model": effective_rerank_model(),
            "error": f"{type(e).__name__}: {e}",
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }

    return out


# ---------------------------------------------------------------- 重建向量库


def _dump_path() -> Path:
    return settings.vector_dump_path


def _dump_existing_chunks() -> List[Dict[str, Any]]:
    """
    把现有 Collection 里的块正文导出成列表（每行结构见 `_write_dump`）。

    用 `query_iterator` 分页而不是 `limit/offset`：Milvus 的 offset 有上限
    （与 limit 之和不能超过 16384），知识库一大就会静默只导出前一半 ——
    而这里少导出的部分会在下一步被清空，属于「静默丢数据」。
    """
    coll = settings.MILVUS_COLLECTION
    rows: List[Dict[str, Any]] = []
    if not milvus_manager.client.has_collection(coll):
        return rows
    iterator = milvus_manager.client.query_iterator(
        collection_name=coll,
        batch_size=_DUMP_BATCH,
        filter="",
        output_fields=["id", "text", "source", "page", "chunk_index"],
    )
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            rows.extend(batch)
    finally:
        try:
            iterator.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("关闭 query_iterator 失败（忽略）: %s", e)
    return rows


def _write_dump(rows: List[Dict[str, Any]]) -> Path:
    """把块正文落盘（原子替换）—— 失败后重跑的依据"""
    path = _dump_path()
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps({
                "id": row.get("id"),
                "text": row.get("text") or "",
                "source": row.get("source") or "",
                "page": row.get("page") or 0,
                "chunk_index": row.get("chunk_index") or 0,
            }, ensure_ascii=False) + "\n")
        fh.flush()
    tmp.replace(path)
    return path


def _read_dump() -> List[Dict[str, Any]]:
    """读回转储（文件不存在返回空列表）"""
    path = _dump_path()
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("向量转储里有无法解析的行，已跳过")
    return rows


def _clear_dump() -> None:
    try:
        _dump_path().unlink(missing_ok=True)
    except OSError as e:  # pragma: no cover
        logger.warning("删除向量转储失败: %s", e)


async def rebuild_vectors(force: bool = False) -> Dict[str, Any]:
    """
    按当前 embedding 配置**重灌**向量库（清空 → 用新模型重新向量化全部块）。

    步骤与每步的理由：

    1. **先自检**（一次单条向量化）。它必须在**删任何东西之前**跑：
       「新模型配置错了」要在数据还没动的时候失败，而不是清空之后才发现。
    2. **导出转储**。顺带做一次安全阀：`count()>0` 却导不出任何行 → 中止，
       不拿一个即将清空的库去赌。
    3. **删表重建**（新维度 + 新标识）→ 逐批重新向量化并 upsert。
    4. 成功则删转储；失败则**保留转储**并如实报告已经灌了多少，
       重跑本操作即幂等续建（chunk_id 是确定性的，upsert 覆盖）。

    Args:
        force: 标识一致时也强制重灌（很少需要，留给「怀疑数据被污染」的场景）

    Returns:
        报告字典。`ok=False` 时一定会带上 `error`，且**不会**留下一个空库 ——
        中止发生在删表之前，或者失败信息里会写明已灌入多少块。
    """
    started = time.perf_counter()
    store_before = milvus_manager.vector_space_state()

    if not force and store_before["state"] == "match":
        return {
            "ok": True,
            "skipped": True,
            "reason": "向量库与当前 embedding 配置一致（同一后端、同一模型、同一维度），无需重建",
            "vector_store": store_before,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    # ---------- 1. 自检：在动数据之前确认新配置真的能产出向量 ----------
    try:
        probe_vec = await asyncio.wait_for(
            get_embeddings(["重建自检"], kind=KIND_PASSAGE), timeout=180.0
        )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"新的 embedding 配置跑不通，未改动向量库：{type(e).__name__}: {e}",
            "vector_store": store_before,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    if not probe_vec:
        return {
            "ok": False,
            "error": "新的 embedding 配置没有返回向量，未改动向量库",
            "vector_store": store_before,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    new_dim = len(probe_vec[0])

    # ---------- 2. 导出（优先用上一次失败留下的转储，失败即续建） ----------
    rows = _read_dump()
    resumed = bool(rows)
    if not resumed:
        try:
            existing_count = milvus_manager.count()
        except Exception:  # noqa: BLE001
            existing_count = 0
        try:
            rows = await asyncio.to_thread(_dump_existing_chunks)
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"读取现有向量失败，未改动向量库：{type(e).__name__}: {e}",
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        if existing_count and not rows:
            return {
                "ok": False,
                "error": (
                    f"现有 Collection 报告有 {existing_count} 块，但一块也没读出来 —— "
                    f"为避免清空后无法恢复，已中止。请先确认 Milvus 正常。"
                ),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        if rows:
            _write_dump(rows)

    total = len(rows)

    # ---------- 3. 清空重建 + 重新向量化 ----------
    try:
        milvus_manager.delete_collection()
        milvus_manager.ensure_collection()
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": (
                f"重建 Collection 失败：{type(e).__name__}: {e}。"
                f"转储仍在 {_dump_path()}，修好后重跑本操作即可续建。"
            ),
            "dump_rows": total,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    written = 0
    failed: List[str] = []
    for start in range(0, total, _REBUILD_BATCH):
        batch = rows[start:start + _REBUILD_BATCH]
        try:
            vectors = await get_embeddings(
                [str(r.get("text") or "") for r in batch], kind=KIND_PASSAGE
            )
            metadatas = [
                {
                    "text": r.get("text") or "",
                    "source": r.get("source") or "",
                    "page": r.get("page") or 0,
                    "chunk_index": r.get("chunk_index") or 0,
                }
                for r in batch
            ]
            written += await milvus_manager.upsert(vectors, metadatas)
        except Exception as e:  # noqa: BLE001
            # 单批失败即中止：继续灌剩下的只会让「灌了多少」更难恢复。
            # 已灌入的部分不回滚 —— 转储在，重跑就是幂等续建。
            failed.append(f"第 {start + 1}~{start + len(batch)} 块: {type(e).__name__}: {e}")
            break

    ok = not failed
    if ok:
        _clear_dump()

    identity = ""
    try:
        identity = embedding_identity()
    except Exception:  # noqa: BLE001
        identity = store_before.get("expected") or ""

    return {
        "ok": ok,
        "skipped": False,
        "resumed_from_dump": resumed,
        "total_chunks": total,
        "written_chunks": written,
        "failed_batches": failed,
        "dimension": new_dim,
        "identity": identity,
        "vector_store": milvus_manager.vector_space_state(),
        "dump_path": None if ok else str(_dump_path()),
        "error": None if ok else (
            f"重新向量化中断：已灌入 {written}/{total} 块。"
            f"转储已保留，重跑本操作会幂等续建（不会重复写入）。"
        ),
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }


__all__ = [
    "RagModelSettingsError",
    "VALID_EMBEDDING_BACKENDS",
    "VALID_RERANK_BACKENDS",
    "scan_local_models",
    "state",
    "update",
    "reset",
    "probe",
    "rebuild_vectors",
]
