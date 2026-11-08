"""
Syncore - FastAPI 应用入口

启动流程:
1. Phase 1: 确保 Milvus Collection 就绪（失败不阻塞启动）
2. Phase 2: 注册内置工具 → 编译 LangGraph（含 Checkpointer）→ 切换 chat 接口为多 Agent 事件流

Phase 2 初始化失败时会自动降级为 Phase 1 的纯 RAG 模式，保证服务可用。

启动命令:
    uvicorn app.main:app --reload --port 8000
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Coroutine, Optional, Set

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import SERVICE_VERSION
from app.api.router import api_router
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Neo4j 驱动会把服务端的「索引已存在」通知按 INFO 打出来，**每次启动一大段多行文本**。
# 这类噪音最麻烦的地方不是它自己，而是它会淹没日志里真正的问题
# （与不变量 28 里那类 RuntimeWarning 是同一个道理）。
# 它没有信息量：`IF NOT EXISTS` 本来就该命中已存在的索引。
logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)

logger = logging.getLogger("app.main")

# 启动期后台任务（预热等）的强引用集合。
# `asyncio.create_task` 只持弱引用，任务对象随时可能被 GC 掉 ——
# 表现是「预热跑到一半日志就没了，而且不报错」。放进集合、结束时移除。
_bg_tasks: Set["asyncio.Task[Any]"] = set()


def spawn_background(coro: "Coroutine[Any, Any, Optional[str]]", label: str) -> "asyncio.Task[Any]":
    """
    起一个「不阻塞启动」的后台任务并持强引用。

    任务的返回值约定为 `None`（成功）或字符串（失败原因）——
    与 `warmup_local_embedding()` / `warmup_local_reranker()` 的约定一致，
    这样调用方不需要写 try/except。

    Returns:
        任务对象。需要**串行**两段预热时（例如两个 GB 级模型不该同时抢磁盘），
        后一段可以 `await` 前一个任务。
    """
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)

    def _done(t: "asyncio.Task[Any]") -> None:
        _bg_tasks.discard(t)
        if t.cancelled():
            return
        err = t.exception()
        if err is not None:
            logger.warning("%s 失败（不影响启动）: %s", label, err)
            return
        reason = t.result()
        if reason:
            logger.warning("✗ %s 未完成: %s", label, reason)
        else:
            logger.info("✓ %s 完成", label)

    task.add_done_callback(_done)
    return task


def _report_milvus_failure(e: BaseException) -> None:
    """
    把 Milvus 初始化失败翻译成「能直接动手」的信息。

    ★ 为什么要分两类

    「维度不一致」与「连不上」都会走到这里，但处置方式完全不同：

    - 连不上：Milvus 进程没起 —— 现象与原因都直白；
    - 维度不一致：Milvus 好好的，只是库还是**上一版 embedding 建的**。
      它的现象是**向量召回恒返回空**：检索不报错、界面照常出答案
      （BM25 / 图路还在兜底），只是效果差一截 —— 最容易被误判成「模型不行」。
      所以这里用 ERROR 级别点名说出「重建 Collection」。

    用 `isinstance` 而不是按错误文本匹配：文本会随措辞变化，
    而类型不会 —— 按文本匹配的判据在下次改文案时就静默失效了。
    """
    try:
        from app.core.milvus_client import CollectionDimensionMismatch

        mismatch = isinstance(e, CollectionDimensionMismatch)
    except Exception:  # noqa: BLE001  # pragma: no cover - 依赖缺失时才走到
        mismatch = False

    if mismatch:
        logger.error(
            "✗ Milvus Collection 与当前 Embedding 后端维度不一致 —— "
            "向量召回会**恒返回空**（不报错，只是检索效果变差）。\n%s", e,
        )
        return
    logger.warning("✗ Milvus 未就绪，RAG 检索/入库将不可用: %s", e)


def _report_vector_space(milvus_manager: Any) -> None:
    """
    启动时核对「库里这批向量」与「当前 embedding 配置」是不是同一个空间。

    ★ 为什么这件事必须有人主动查

    维度不一致会在 `ensure_collection()` 直接抛错（上一节），**好发现**。
    但把 embedding 换成**另一个同维度的模型**（比如两个 512 维的中文模型）
    完全不会报错：写入成功、检索有结果，只是库里的旧向量与新查询向量
    根本不在一个空间里，相似度变成噪声 —— 症状是「换了模型之后效果反而差了」，
    几乎一定会被归因成「这个模型不行」（不变量 53）。

    判定依据是 Collection 自己登记的产出模型（见 `core/milvus_client.py`）。
    这里只告警、不阻断：库里可能还有别的路兜底（BM25 / 图），
    重建是一个要花钱的显式动作（`POST /api/rag/vectors/rebuild`）。
    """
    try:
        state = milvus_manager.vector_space_state()
    except Exception as e:  # noqa: BLE001
        logger.debug("向量空间核对失败（忽略）: %s", e)
        return

    if state.get("state") == "mismatch":
        logger.warning(
            "✗ 向量库与当前 Embedding 配置不是同一个向量空间 —— "
            "检索**不会报错**，但相似度已失去意义（旧向量是别的模型产的）。\n"
            "  库里登记: %s\n  当前配置: %s\n"
            "  处理方式：界面「高级 → 检索模型 → 重建向量库」，或 "
            "POST /api/rag/vectors/rebuild",
            state.get("registered"), state.get("expected"),
        )
    elif state.get("state") == "unknown" and state.get("error"):
        logger.info("向量空间未核对: %s", state["error"])


async def _warmup_reranker_after(
    prereq: "asyncio.Task[Any] | None",
    warmup: "Callable[[], Coroutine[Any, Any, Optional[str]]]",
) -> Optional[str]:
    """
    等前一段预热结束后再预热精排模型。

    为什么需要这个「排队」：本机是本地 Embedding（95MB）+ 本地精排（2.27GB）
    两个模型，同时加载会互相抢磁盘与 CPU，两边都变慢，而**先要用到的是
    Embedding**（融合之前就要算查询向量）。串行化让 Embedding 尽早可用，
    代价只是精排晚几秒就绪 —— 那几秒通常还轮不到它上场。

    `prereq` 为 None（外部 API 后端 / 预热没起）时直接开始，不做任何等待。
    """
    if prereq is not None:
        # return_exceptions=True：前置任务失败（返回的是失败原因字符串，不抛异常）
        # 或抛异常都不该阻断精排预热 —— 两者本来就不互相依赖。
        await asyncio.gather(prereq, return_exceptions=True)
    return await warmup()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化依赖，关闭时释放资源"""

    # ---------- Phase 1: Milvus ----------
    try:
        from app.core.milvus_client import milvus_manager

        milvus_manager.ensure_collection()
        logger.info("✓ Milvus Collection 就绪: %s", settings.MILVUS_COLLECTION)
        _report_vector_space(milvus_manager)
    except Exception as e:  # noqa: BLE001
        _report_milvus_failure(e)

    # ---------- Phase 3: Elasticsearch（关键词召回） ----------
    if settings.ES_ENABLED:
        try:
            from app.rag.es_client import es_manager

            if await es_manager.ensure_index():
                doc_count = await es_manager.count()
                logger.info("✓ Elasticsearch 索引就绪: %s (%s, %d 条文档)",
                            es_manager.index_name, settings.ES_URL, doc_count)
                # 「连得上、但索引是空的」是最隐蔽的一种降级：不抛异常、不影响启动，
                # 只是 BM25 这一路恒返回空，混合检索静默退化为纯向量。
                # 换 ES 容器 / 换数据卷 / 忘记重建索引后必然出现，所以显式告警。
                if doc_count == 0 and settings.RETRIEVAL_MODE != "vector":
                    logger.warning(
                        "⚠ ES 索引 '%s' 为空，但 RETRIEVAL_MODE=%s："
                        "关键词召回恒为空，混合检索实际已退化为纯向量。"
                        "请通过 POST /api/index/scan 重新扫描知识目录入库。",
                        es_manager.index_name, settings.RETRIEVAL_MODE,
                    )
            else:
                logger.warning(
                    "✗ Elasticsearch 未就绪，检索降级为纯向量召回: %s", settings.ES_URL
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("✗ Elasticsearch 初始化失败，检索降级为纯向量召回: %s", e)

    # ---------- Phase 3: 精排后端 ----------
    # 本地精排（bge-reranker-v2-m3，2.27GB）与本地 Embedding 不该同时加载：
    # 两个 GB 级模型并发读盘 + 反序列化会互相拖慢，而 Embedding 是检索链路上
    # **先**要用到的那个（融合之前就要算查询向量），所以让它先跑。
    # 串行化的做法见本段之后的「本地精排预热」。
    if settings.RERANK_ENABLED:
        try:
            from app.rag.reranker import (
                LocalCrossEncoderReranker,
                get_reranker,
                reranker_status,
            )

            backend = get_reranker()
            status = reranker_status()
            if status.get("degraded_reason"):
                logger.warning("✗ 精排已降级为不精排: %s", status["degraded_reason"])
            else:
                logger.info("✓ 精排后端就绪: %s (%s)", backend.name, status.get("model"))
            if isinstance(backend, LocalCrossEncoderReranker):
                logger.info("精排运行在本地模型上：%s（device=%s，max_len=%s，批大小=%s）",
                            settings.RERANK_LOCAL_MODEL, settings.RERANK_LOCAL_DEVICE,
                            settings.RERANK_LOCAL_MAX_LENGTH, settings.RERANK_LOCAL_BATCH_SIZE)

            # 上报当前检索模式，方便一眼看出跑的是哪条链路
            logger.info("检索模式: %s | 向量TopK=%s | BM25TopK=%s | RRF k=%s | 精排候选=%s → TopN=%s",
                        settings.RETRIEVAL_MODE, settings.HYBRID_VECTOR_TOP_K,
                        settings.HYBRID_BM25_TOP_K, settings.HYBRID_RRF_K,
                        settings.RERANK_CANDIDATES, settings.RERANK_TOP_N)
        except Exception as e:  # noqa: BLE001
            logger.warning("✗ 精排初始化失败，将按融合顺序返回: %s", e)

    # ---------- Phase 3: Embedding 后端 ----------
    # 本地模型首载实测 **9.3 秒**（torch import + 权重反序列化）。
    # 放在启动路径上 → 后端晚 9 秒才能服务；放在第一次提问上 → 用户等 9 秒。
    # 两个都不行，所以：先做**零成本**的配置校验（只读 config.json），
    # 再把真正的加载投成后台任务 —— 启动不受影响，首问时模型通常已经就绪。
    emb_warmup: "asyncio.Task[Any] | None" = None
    try:
        from app.core.embedding_factory import backend_name, embedding_info, warmup_local_embedding

        info = embedding_info()
        where = ""
        if backend_name() == "local":
            where = f" ← {settings.EMBEDDING_LOCAL_MODEL} ({settings.EMBEDDING_LOCAL_DEVICE})"
        logger.info("Embedding 后端: %s | %s | %s 维%s",
                    info["backend"], info["model"], info["dimension"], where)

        if info.get("degraded_reason"):
            # 维度对不上 / 模型目录不存在都会走到这里。这是**必须**让用户看见的错误：
            # 它不会让启动失败，但会让每一次向量化都失败。
            logger.warning("✗ Embedding 后端配置有问题: %s", info["degraded_reason"])

        if backend_name() == "local":
            emb_warmup = spawn_background(warmup_local_embedding(), "本地 Embedding 模型预热")
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ Embedding 后端初始化失败: %s", e)

    # ---------- Phase 3: 本地精排预热（排在 Embedding 之后） ----------
    # 见上面 Phase 3 精排段的说明：两个 GB 级模型串行加载。
    try:
        from app.rag.reranker import warmup_local_reranker

        spawn_background(
            _warmup_reranker_after(emb_warmup, warmup_local_reranker),
            "本地精排模型预热",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 本地精排预热调度失败: %s", e)

    # ---------- Phase 2: 工具注册中心 + LangGraph ----------
    graph_ready = False
    if settings.ENABLE_GRAPH:
        try:
            from app.agents.graph import init_graph
            from app.agents.tools.builtin import register_builtin_tools
            from app.api.chat import enable_graph

            register_builtin_tools()

            # 技能扫描（只在启动时打一行日志，不参与启动流程）：
            # 症状「我装了技能她不用」的第一步排查就是"进程到底看见了几个"，
            # 而界面是另一个进程写的目录 —— 没有这行日志就只能靠重启去猜有没有生效。
            try:
                from app.skills import get_skill_store

                _skills = get_skill_store().scan(force=True)
                _bad = [s.name for s in _skills if not s.usable]
                logger.info(
                    "→ 技能: %s 个可注入（共扫描 %s 个%s）",
                    len(get_skill_store().active_skills()),
                    len(_skills),
                    f"，读不出来的：{_bad}" if _bad else "",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("✗ 技能扫描失败（不影响其它功能）: %s", e)

            # Phase 3: 接入配置声明的 MCP Server（失败只告警，不阻塞启动）
            await _register_mcp_servers()

            graph = await init_graph()
            enable_graph(graph)
            graph_ready = True
            logger.info("✓ Supervisor 模式已启用（含工具治理 + HITL 审批 + 后台子任务）")
        except Exception as e:  # noqa: BLE001
            logger.warning("✗ Supervisor 初始化失败，降级为 Phase 1 RAG 模式: %s", e, exc_info=True)

    # ---------- Phase 9: 后台子任务（Supervisor 架构，见 ADR 0019）----------
    # ★ 放在图初始化**之后**
    # 结算上次进程留下的任务时要能判断「这个任务本该有 checkpointer」，
    # 而 checkpointer 是 `init_graph` 建的。
    #
    # ⚠️ 早于用户第一次对话执行 —— 那些任务是上一个进程派出去的，
    # 用户这次打开界面就该看到它们的结局（而不是一堆永远转圈的进度条）。
    try:
        from app.tasks.executor import executor
        from app.tasks.store import task_store

        await executor.reset()
        if settings.SUBTASK_SWEEP_ON_START:
            counts = task_store.sweep_orphans()
            # ★ pending 任务没有副作用，重新派发是安全的（不变量 57）。
            # store 只负责把它们挑出来，这里真正经 executor 重新 create_task。
            redispatched = 0
            for task_id in counts["redispatched"]:
                if executor.redispatch(task_id):
                    redispatched += 1
            # ★ 中断的任务不自动续（可能重放最后一步），但要当场评估「能不能续」：
            # 能续的挂「继续」按钮、要核对的写清最后一步、没状态的才判死。
            # 这一步放在这里而不是懒到用户点开时，是因为界面上「有没有继续按钮」
            # 必须来自事实，而不是一次点击之后才知道。
            from app.tasks.recovery import reconcile_interrupted

            await reconcile_interrupted()
            # ★ 补扫「成功了但还没汇报」的任务（进程在终态与汇报之间被杀时留下）。
            # 终态事件只发生一次，不补扫的话这批任务永远没人向用户汇报。
            try:
                from app.tasks.report import deliver_pending_reports

                await deliver_pending_reports()
            except Exception as e:  # noqa: BLE001
                logger.warning("汇报补扫失败（不影响启动）: %s", e)
            if redispatched or counts["interrupted"]:
                logger.info(
                    "后台任务结算完成: 重新派发 %s 个 / 待评估续跑 %s 个",
                    redispatched,
                    len(counts["interrupted"]),
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 后台子任务初始化失败（不影响问答）: %s", e)

    # ---------- 工作空间恢复：先对账任务，再恢复派发与消息 ----------
    # 只重新派发未启动的持久化意图。中断任务保留检查点，可能已有副作用时先核对。
    try:
        from app.workspace.recovery import reconcile_workspaces
        from app.workspace.orchestrator import orchestrator
        from app.workspace.messaging import messenger

        orchestrator._closing = False
        messenger.stopping = False
        recovered = await reconcile_workspaces()
        if recovered:
            logger.info("✓ 协作 run 对账完成: %s 次协作已恢复运行账与消息投递", recovered)
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 协作 run 对账失败（不影响问答）: %s", e)

    # ---------- Phase 9c: 定时任务（对账 + 调度循环） ----------
    # ★ 排在 Phase 9 之后：运行记录的对账要读子任务的终态
    # （`sweep_on_startup` 靠 task_store 判断「那次运行其实跑完了没有」），
    # 而子任务自己那批孤儿必须先结算干净，否则这里读到的是假的 running。
    # 调度循环放在对账之后启动：先让磁盘状态收敛成事实，再开始比对触发点。
    try:
        from app.schedules.runner import sweep_on_startup
        from app.schedules.scheduler import start_scheduler

        sweep_on_startup()
        await start_scheduler()
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 定时任务初始化失败（不影响问答）: %s", e)

    # ---------- Phase 9d: 微信通道（外部 IM 入口） ----------
    # 只接「收消息 → 助理 → 回信」这一条；扫码登录在 api/channels.py。
    # ★ 放在图初始化之后：worker 回答走的是与桌面端同一个已编译的图（assistant_main），
    # 自己再编译一张等于两套 checkpointer / 两套工具注册，迟早漂移。
    try:
        from app.channels.wechat_worker import wechat_channel

        if await wechat_channel.start():
            logger.info("✓ 微信通道已接入助理（收到消息会由 assistant_main 回答并回信）")
        else:
            logger.info("→ 微信通道未启动（还没有绑定凭据）")
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 微信通道初始化失败（不影响其它功能）: %s", e)

    if not graph_ready:
        logger.info("→ 当前运行模式: Phase 1 纯 RAG")

    # ---------- Phase 4: 链路追踪（可观测层） ----------
    # 放在最后启动：它要回填历史 trace，属于「锦上添花」的初始化，
    # 不该让任何一项基础设施初始化等它。
    try:
        from app.observability import start_observability

        await start_observability()
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 链路追踪初始化失败（不影响问答）: %s", e)

    # ---------- Phase 4: 评估能力探测 ----------
    # 只探测不执行 —— 评估是脚本级的跑批（要打几十次真实模型），
    # 不该在启动时触发。这里上报状态是为了让「能不能评估」一眼可见。
    try:
        from app.eval import ensure_default_golden_set, judge_status

        golden = ensure_default_golden_set(settings.golden_set_path)
        logger.info("✓ Golden Set 就绪: %s 条查询（%s）",
                    len(golden.items),
                    ", ".join(f"{k} {v}" for k, v in sorted(golden.counts_by_kind().items())))

        judge = judge_status()
        if judge.get("available"):
            logger.info("✓ 答案质量判定后端就绪: %s (%s)", judge["active"], judge["model"])
        else:
            logger.warning("✗ 答案质量判定不可用，答案层指标将被跳过: %s",
                           judge.get("degraded_reason"))
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 评估能力初始化失败: %s", e)

    # ---------- Phase 5: 增量索引 ----------
    # 先报清单现状再决定要不要起监听：监听默认是关的（它会花钱调 Embedding），
    # 但「清单里有几个文件、上次扫描是什么时候」应当永远可见。
    try:
        from app.rag import incremental
        from app.rag.watcher import start_watcher

        idx = incremental.status()
        logger.info(
            "增量索引：已跟踪 %s 个文件 / %s 块 | 监听目录=%s",
            idx["tracked_files"], idx["tracked_chunks"], ", ".join(idx["watch_dirs"]),
        )
        last = idx.get("last_scan") or {}
        if last.get("finished_at"):
            logger.info(
                "增量索引：上次扫描 %s（新增 %s / 重建 %s / 删除 %s）",
                last.get("finished_at"),
                len(last.get("added") or []),
                len(last.get("modified") or []),
                len(last.get("deleted") or []),
            )
        await start_watcher()
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 增量索引初始化失败（不影响问答）: %s", e)

    # ---------- Phase 5: 长期记忆 ----------
    try:
        from app.memory import get_manager

        mem = get_manager().stats()
        if mem["total"]:
            logger.info("✓ 长期记忆: %s 条偏好（%s）", mem["total"], mem["store_path"])
        else:
            logger.info("长期记忆为空 —— 用户说出「记住：…」后会自动积累")
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 长期记忆初始化失败（问答不受影响）: %s", e)

    # ---------- Phase 6: 审批留痕 + 预授权 ----------
    # 回填最近若干条到内存，否则「重启后审批面板空白、但文件里明明有数据」
    # 会成为一个反复被问的问题（与 TRACE_LOAD_ON_START 是同一个坑）。
    try:
        from app.approval import approval_store, grant_registry

        # 先归档超期记录再回填：保留期收紧后重启立即生效，且回填不会读
        # 马上要归档的行（与 trace cleanup_old_files → load_recent 同一原则）
        if approval_store.enabled:
            try:
                cleaned = approval_store.cleanup_old_records()
                if cleaned.get("archived"):
                    logger.info(
                        "审批留痕归档：%s 条进 archive/（保留 %s 条）",
                        cleaned["archived"],
                        cleaned["kept"],
                    )
            except Exception as e:  # noqa: BLE001
                # 归档失败不影响回填，留痕仍是旁路能力
                logger.warning("审批留痕归档失败（不影响回填）: %s", e)

        loaded = approval_store.load_recent()
        stats = approval_store.stats()
        # 措辞说明：`written` 是**本次进程**的追加条数（回填的历史不算），
        # 所以它启动时必然是 0。写成「已落盘 N 条」会被读成“磁盘上一共只有 N 条”，
        # 与旁边的 `loaded`（从磁盘回填进内存的条数）互相矛盾。
        logger.info(
            "审批留痕：内存 %s 条（本次进程新写入 %s 条）| 路径=%s",
            loaded, stats["written"], stats["path"],
        )
        by_decision = stats.get("by_decision") or {}
        if by_decision:
            logger.info(
                "审批留痕分布：%s",
                " / ".join(f"{k} {v}" for k, v in sorted(by_decision.items())),
            )
        pending_count = by_decision.get("pending", 0)
        if pending_count:
            logger.warning(
                "⚠ 有 %s 条审批记录停在「等待中」：多半是上次进程被杀或等待超时后未回填。"
                "它们不会被自动清理 —— 停在 pending 本身就是有价值的信号。",
                pending_count,
            )
        logger.info("预授权：当前生效 %s 条", grant_registry.stats()["active"])
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 审批留痕初始化失败（审批本身不受影响）: %s", e)

    # ---------- Phase 4b: GraphRAG（多跳图检索的第三路召回） ----------
    # 主动探一次连通性：图是软依赖，但它最危险的失效形态是「以为在跑、其实一路空」
    # —— 检索不报错，只是多跳证据永远进不来，指标看起来“只是低了一点”。
    # 所以宁可启动时多花一次握手，也要把这个状态打出来。
    try:
        from app.graph.store import graph_store

        g = await graph_store.status()
        if not g.get("enabled"):
            logger.info("→ 图检索未启用（GRAPH_ENABLED=false），混合检索为两路融合")
        elif g.get("available"):
            stats = g.get("stats") or {}
            logger.info(
                "✓ 图检索就绪: %s（命名空间=%s | 实体 %s / 关系 %s / 块 %s）",
                g.get("uri"), g.get("space"),
                stats.get("entities"), stats.get("relations"), stats.get("chunks"),
            )
            if not stats.get("relations"):
                logger.warning(
                    "⚠ 图谱为空，但 GRAPH_ENABLED=true：多跳这一路将恒返回空，"
                    "混合检索实际已退化为两路。首次使用请执行 "
                    "`python scripts/build_graph.py` 或 POST /api/graph/build。"
                )
        else:
            logger.warning(
                "✗ 图检索不可用，混合检索降级为两路融合（多跳证据拿不到）: %s",
                g.get("degraded_reason"),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 图检索初始化失败（问答不受影响）: %s", e)

    # ---------- Phase 7: 会话历史 ----------
    # 回填索引并报告规模。「有几个会话、最后一次聊是什么时候」应当开机就可见 ——
    # 否则「历史面板是空的」到底是没聊过、还是索引坏了，没人分得清。
    try:
        from app.conversations import conversation_store

        conv = conversation_store.stats()
        if conv["conversations"]:
            logger.info(
                "✓ 会话历史：%s 个会话 / %s 轮（%s）",
                conv["conversations"], conv["turns"], conv["dir"],
            )
        else:
            logger.info("会话历史为空 —— 首轮问答结束后会自动建档（%s）", conv["dir"])
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 会话历史初始化失败（问答不受影响）: %s", e)

    # ---------- 工作空间模块（群组 + 数字员工） ----------
    # 惰性建表并报告规模。「有几个工作空间 / 员工」开机就该可见，
    # 否则「工作空间页是空的」到底是没建过、还是库没初始化，没人分得清。
    try:
        from app.workspace import workspace_store

        ws = workspace_store.stats()
        logger.info(
            "✓ 工作空间：%s 个工作空间 / %s 个群组 / %s 名员工（%s）",
            ws.get("workspaces"), ws.get("groups"), ws.get("employees"), ws.get("path"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 工作空间初始化失败（问答不受影响）: %s", e)

    # ---------- Phase 4b: 语音能力 ----------
    # 只报状态不打网关：语音是增益不是依赖，挂掉只是按钮不可用。
    try:
        from app.voice.stt import stt_status
        from app.voice.tts import tts_status

        stt = stt_status()
        tts = tts_status()
        if settings.VOICE_ENABLED:
            logger.info(
                "✓ 语音: STT %s（%s / %s）| TTS %s%s",
                "就绪" if stt.get("available") else "不可用",
                stt.get("backend"),
                stt.get("model"),
                tts.get("backend"),
                f"（{tts.get('model', tts.get('voice', ''))}）" if tts.get("available") else "",
            )
            if not stt.get("available"):
                logger.warning("✗ 语音 STT 不可用: %s", stt.get("reason"))
            if not tts.get("available"):
                logger.warning("✗ 语音 TTS 不可用: %s", tts.get("reason"))
            if settings.VOICE_STT_BACKEND == "local" and stt.get("available"):
                # 后台预热：模型加载要 1–3 秒，别让第一次按住麦克风承担它
                from app.voice.local_stt import warmup_async

                spawn_background(warmup_async(), "本地语音识别模型预热")
        else:
            logger.info("→ 语音功能未启用（VOICE_ENABLED=false）")
    except Exception as e:  # noqa: BLE001
        logger.warning("✗ 语音能力初始化失败（问答不受影响）: %s", e)

    yield

    # ---------- 关闭 ----------
    from app.workspace.orchestrator import orchestrator
    await orchestrator.shutdown()
    from app.workspace.messaging import messenger
    await messenger.shutdown()
    # 先把启动期后台任务收干净：预热可能在进程退出时还挂在 `asyncio.to_thread` 上，
    # 不管它的话解释器会打出 “Task was destroyed but it is pending” —— 那是**噪音**，
    # 会让人以为关停出了问题（与不变量 28 里那类警告是同一个道理）。
    for task in list(_bg_tasks):
        task.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
        _bg_tasks.clear()

    # 先停微信长轮询：它可能正挂在一轮回答上，晚停会让关停多等一次模型调用。
    try:
        from app.channels.wechat_worker import wechat_channel

        await wechat_channel.stop()
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.rag.watcher import stop_watcher

        await stop_watcher()
    except Exception:  # noqa: BLE001
        pass

    # 调度循环要先停：晚停一个 tick 就会在进程收尾阶段派发出一批没人结算的子任务，
    # 那些运行记录又得等下次启动的对账来收尸。
    try:
        from app.schedules.scheduler import stop_scheduler

        await stop_scheduler()
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.memory import drain_background_tasks

        await drain_background_tasks()
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.observability import shutdown_observability

        await shutdown_observability()
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.agents.graph import shutdown_graph

        await shutdown_graph()
    except Exception:  # noqa: BLE001
        pass

    # 关闭 MCP 连接与 ES 连接
    try:
        from app.agents.tools.mcp_adapter import mcp_manager

        await mcp_manager.close_all()
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.rag.es_client import es_manager

        await es_manager.close()
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.graph.store import graph_store

        await graph_store.close()
    except Exception:  # noqa: BLE001
        pass

    logger.info("Syncore 服务已关闭")


async def _register_mcp_servers() -> None:
    """
    按配置文件接入 MCP Server，把远端工具注册进工具治理中心。

    完全容错：单个 server 连不上只影响它自己的工具，
    其余 server 与全部内置工具照常可用。

    ★ 清单来自 `data/mcp/servers.json`（设置页写的那份），不再读 `.env` 的 JSON
    `.env` 的 `MCP_SERVERS` 只在文件不存在时种子一次（见 `app/mcp_store.py`）——
    两边都读会出现「改了 .env 为什么没生效」这种两头都对不上的状态。
    """
    from app.agents.tools.mcp_adapter import mcp_manager
    from app.mcp_store import get_mcp_store

    declared = get_mcp_store().load()
    if not declared:
        logger.info("→ 未配置 MCP Server（%s 里没有条目），跳过", settings.mcp_store_path)
        return

    result = await mcp_manager.apply()
    if result.get("connected"):
        logger.info(
            "✓ MCP 工具接入完成: %s 个工具来自 %s 个 server（%s）",
            result["tools"], len(result["connected"]), ", ".join(result["connected"]),
        )
    if result.get("failed"):
        logger.warning("✗ 部分 MCP Server 未接入: %s", result["failed"])


def create_app() -> FastAPI:
    """构建 FastAPI 应用"""
    app = FastAPI(
        title="Syncore API",
        description="企业级 AI 工作台 - RAG 知识库 + 多 Agent 编排 + 工具治理",
        version=SERVICE_VERSION,
        lifespan=lifespan,
    )

    # CORS：允许本地前端开发服务器访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 统一挂载 /api 前缀下的所有路由
    app.include_router(api_router)

    _install_task_not_found_normalizer(app)

    @app.get("/", tags=["root"])
    async def root():
        """根路径，返回服务概览"""
        return {
            "service": "Syncore",
            "version": SERVICE_VERSION,
            "docs": "/docs",
            "api_prefix": "/api",
        }

    return app


def _install_task_not_found_normalizer(app: FastAPI) -> None:
    """
    ★ 把 `/api/tasks/...` 的「路由未匹配」404 归一化成「任务不存在」

    背景（不变量 47 的完整实现）：
    `GET /api/tasks/..%2F..%2Fetc` 这类输入**根本进不到** `/{task_id}` 处理器
    —— Starlette 在 URL 解码阶段发现它不是合法路径，直接返回它自己的 404
    （`{"detail": "Not Found"}`）。

    于是响应体不一致：
        非法 id（路由匹配得上）    → `{"detail": "任务不存在"}`
        路径穿越（路由匹配不上）  → `{"detail": "Not Found"}`

    ★ 响应体不同**等于泄露信息**
    攻击者能靠文案差异判断「这个是格式问题」还是「这个任务真的存在」，
    于是「哪些路径存在」变成可探测的 —— 而这正是不变量 47 要防的。

    ⚠️ 只作用于 `/api/tasks` 前缀，其它路由的 404 一律不动 ——
    那个 app 里还有别的东西（`/docs`、`/`、静态资源），
    统一改它们的 404 只会让真正的「页面不存在」也变成「任务不存在」。
    """
    from fastapi.exception_handlers import http_exception_handler
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _normalize_task_404(request, exc: StarletteHTTPException):  # noqa: ANN001, ANN202
        if exc.status_code == 404 and request.url.path.startswith("/api/tasks"):
            return JSONResponse(
                status_code=404,
                content={"detail": "任务不存在"},
            )
        # 其它路径：交回 FastAPI 默认处理（它是 `http_exception_handler`，
        # 直接 import 而不 `app.exception_handlers[404]` ——
        # 后者在这台 FastAPI 版本上是 None，取不到）
        return await http_exception_handler(request, exc)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=True,
    )
