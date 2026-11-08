"""
配置中心 - 从 .env 读取所有配置
Pydantic Settings 自动管理环境变量加载与类型转换

★ 两级配置：`.env`（部署配置） + `data/runtime_config.json`（运行时覆写）

`.env` 回答「这台机器怎么部署」，由人手工维护；运行时覆写回答「界面上点了哪个模型」，
由程序写。两个前端入口写的就是后者：

- 「AI → 语言模型」—— 换 `LLM_MODEL`（默认回答模型，影响所有走默认的调用）；
- 「高级 → 检索模型」—— 换向量召回 / 精排模型（改 embedding 会让已入库向量作废）。

两者分开之后，「这个值到底从哪来的」永远有答案（`RUNTIME_OVERRIDES` 就是清单）。
详见文件末尾的「运行时覆写层」。
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _parse_model_list(raw: str) -> List[str]:
    """
    解析 `LLM_AVAILABLE_MODELS`（逗号 / 分号分隔，去重且保持书写顺序）。

    抽成模块级函数是因为它有两个消费者：`declared_models`（原样）与
    `available_models`（补上当前默认模型）—— 分隔符与去重规则只该有一处，
    否则「界面里少了一个候选」会变成一次口味不同的分词引发的悬案。
    """
    if not raw or not raw.strip():
        return []
    names: List[str] = []
    for item in raw.replace(";", ",").split(","):
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return names


class Settings(BaseSettings):
    """全局配置，从环境变量 / .env 文件加载"""

    # ---------- LLM 配置 (OpenAI 兼容接口) ----------
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = "sk-placeholder"
    #: 默认回答模型。**可以在前端「AI → 语言模型」里切换**（写运行时覆写，不动 `.env`）。
    #:
    #: 注意它与顶栏「模型」下拉的区别，两者不是一回事：
    #: - 顶栏那个是**本轮覆盖**（`POST /api/chat` 的 `model` 字段，只影响这一次问答）；
    #: - 这里是**全局默认** —— 标题生成、记忆抽取、图谱抽取、评估裁判等
    #:   所有没显式指定模型的调用都读它。
    #: 换掉它 = 换掉整台机器的“脑子”，所以界面上要写清影响面。
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = 0.7
    # 单次请求超时与重试次数：调小可让「模型网关不可用」快速失败并向前端报错，
    # 而不是让用户对着空白界面等几十秒
    LLM_TIMEOUT_SECONDS: float = 30.0
    LLM_MAX_RETRIES: int = 1
    #: 是否让模型进入「深度思考」（会先产出一大段内部推理再作答）。
    #:
    #: ⚠️ 这是本项目**最影响体感速度**的一个开关，实测（qwen3.7-flash，同一问题）：
    #:     思考开：总 13.8~20.2s，**首字 12.9~19.6s**，completion 1080~1924 tokens；
    #:     思考关：总 1.9~2.1s，  **首字 0.4s**，       completion 97~108 tokens。
    #: 也就是说用户等待的时间里九成以上花在生成**他看不到**的推理上（答案本身长度几乎不变）。
    #: 对本项目的主场景（基于知识库作答）这笔开销换不回什么：实测推理正文里
    #: 大量篇幅是模型在纠结「该不该拒答」，与答案质量无关。
    #:
    #: `None` = **不发这个字段**，交给网关的默认行为 —— 这是刻意的默认值：
    #: `enable_thinking` 是 Qwen 系网关的扩展参数，对 OpenAI 官方端点等
    #: 不认它的网关发过去可能直接 400。要提速就显式设为 False（见 `.env`）。
    LLM_ENABLE_THINKING: Optional[bool] = None
    #: `reasoning_effort` 档位（low / medium / high）—— 只有 OpenAI o 系列 /
    #: gpt-5 这类「始终思考、靠强度调节」的模型会用它（见 app/llm/thinking.py）。
    #: 本项目默认走 Qwen 系网关，这个键平时不参与请求。
    LLM_THINKING_EFFORT: str = "high"
    #: 顶栏「模型」下拉的候选（逗号分隔）。**留空 = 不启用选择器** ——
    #: 「能自动的不要问」：只有一个模型时，给用户一个只有一项的下拉框
    #: 只会让人以为哪里还能选。配了两个以上才出现。
    #: 例：qwen3.7-flash,qwen3.7-plus,qwen3.7-max
    LLM_AVAILABLE_MODELS: str = ""

    # ---------- 上下文管理（长对话不爆窗、也不突然失忆） ----------
    #
    # 旧实现只给模型最近 20 条消息、工具结果硬切 2000/3000 字 —— 短对话够用，
    # 长对话会同时出现两个相反的毛病：窗口明明还空着却已经开始忘，以及一条
    # 超长附件就能把窗口顶爆。这里改成按 **token 预算** 取上下文：
    # 先折叠旧工具输出（观察屏蔽），不够再对更早的对话做结构化摘要压缩。
    #
    #: 模型上下文窗口（tokens）。网关的 `/v1/models` 不返回窗口大小，只能声明。
    #: 常见模型有内置估算（见 `app/agents/context.py::_BUILTIN_WINDOWS`），
    #: 未识别的模型回落到这个值。**宁可估小**：估大 = 真的 400，估小只是多压缩一次。
    CONTEXT_WINDOW_TOKENS: int = 32768
    #: 按模型名覆盖窗口大小。JSON 对象或 `模型=tokens,模型=tokens` 都接受，
    #: 匹配规则是「覆盖键是模型名的子串，取最长匹配」——网关的模型 id 常带日期后缀。
    CONTEXT_WINDOW_OVERRIDES: str = ""
    #: 给模型输出预留的 token（不参与输入预算）
    CONTEXT_OUTPUT_RESERVE_TOKENS: int = 4096
    #: 走到可用窗口的多少比例时触发压缩
    CONTEXT_COMPACT_RATIO: float = Field(0.85, gt=0.3, le=0.95)
    #: 压缩后保留最近多少比例的原文（其余进入摘要）
    CONTEXT_RECENT_RATIO: float = Field(0.38, gt=0.1, le=0.8)
    #: 为「这一轮还会继续长」预留的余量；触发点不会晚于「可用窗口减它」
    CONTEXT_HEADROOM_TOKENS: int = 8192
    #: 摘要用的模型；留空 = 跟随本轮回答模型（必须与当前供应商同一条连接）
    CONTEXT_SUMMARY_MODEL: str = ""
    #: 摘要调用的超时（秒）。超时/失败时退化为确定性摘要，不阻塞本轮回答
    CONTEXT_SUMMARY_TIMEOUT_SECONDS: float = 60.0
    #: 摘要记录落盘目录（按会话一个 JSON；删会话/删轮次时同步失效）
    CONTEXT_SUMMARY_DIR: str = "./data/context_summaries"
    #: 摘要记录保留期（天）。过期只会丢掉摘要缓存（原文仍在会话上下文里，
    #: 下次使用时会自动重建）；删会话 / 删轮次时则**立即**清理，不等 TTL。
    CONTEXT_SUMMARY_TTL_DAYS: int = 90
    #: 最近多少条工具结果保持完整；更早的只留占位与要点（观察屏蔽）
    CONTEXT_KEEP_RECENT_TOOL_RESULTS: int = 6
    #: 旧工具调用的参数（写文件正文等）超过这个长度就折叠成摘要式参数
    CONTEXT_TOOL_ARGS_MAX_CHARS: int = 2000
    #: 完整工具输出落盘目录：超长结果写到这里，模型拿到 path 后可分段 read
    TOOL_RESULTS_DIR: str = "./data/tool_results"
    #: 单条工具结果内联进消息的最大字符数；超出则落盘 + 头尾 + 引用
    TOOL_RESULT_INLINE_CHARS: int = 12000
    TOOL_RESULT_HEAD_CHARS: int = 8000
    TOOL_RESULT_TAIL_CHARS: int = 2000
    #: 工具输出缓存保留期（天）
    TOOL_RESULT_TTL_DAYS: int = 7

    # ---------- Embedding 配置 ----------
    #: 用哪个后端做向量化：`api`（外部 OpenAI 兼容接口）| `local`（进程内本地模型）。
    #:
    #: 为什么把「后端」做成开关而不是直接换掉 `EMBEDDING_MODEL`：
    #: 两者的**调用形态完全不同**（一个是网络请求 + 分批 + 折半重试，
    #: 一个是进程内推理 + 线程池 + 惰性加载），塞进同一个字段只会让两边都变形。
    #: 默认值保持 `api`，是为了让老配置与测试基线一行都不用改。
    #: 换后端**必须同时改 `EMBEDDING_DIMENSION`**（维度随模型变），
    #: 且 Milvus Collection 需要重建 —— 代码会在维度不一致时直接报错并给出该填的值。
    EMBEDDING_BACKEND: str = "api"

    # --- 后端 = api 时生效 ---
    EMBEDDING_BASE_URL: str = "https://api.openai.com/v1"
    EMBEDDING_API_KEY: str = "sk-placeholder"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    # 单次 embedding 请求的最大条数。
    # ⚠️ 各家网关上限不同：本项目实测 qwen 网关为 20，超过会直接 400。
    # 代码在收到「批大小超限」错误时会自动折半重试，此值只是首选的起始批大小。
    EMBEDDING_MAX_BATCH: int = 20

    # --- 后端 = local 时生效 ---
    #: 本地模型：**目录路径**（如 `C:/models/bge-small-zh-v1.5`）或 HuggingFace id。
    #: 用目录路径的好处是完全离线、不受网络与镜像影响。
    #: 中文短文本检索的性价比之选：4 层 BERT，512 维，CPU 单条约几毫秒。
    EMBEDDING_LOCAL_MODEL: str = "BAAI/bge-small-zh-v1.5"
    #: `cpu` / `cuda`。CPU 上这个小模型已经够快，换 GPU 只对**批量入库**有意义。
    EMBEDDING_LOCAL_DEVICE: str = "cpu"
    #: 截断长度。bge-small-zh-v1.5 的 `max_position_embeddings` 就是 512，
    #: 调大没有意义（位置编码用完就没了）；调小能省算力但会切掉长块。
    EMBEDDING_LOCAL_MAX_LENGTH: int = 512
    #: 本地推理的批大小。本地没有网关的批上限，这个值只影响内存峰值与吞吐。
    EMBEDDING_LOCAL_BATCH_SIZE: int = 32
    #: 查询侧指令前缀（**只作用于查询，不作用于入库的段落**）。
    #:
    #: 官方说明：bge-*-v1.5 相对上一代已「改进为不加指令也能检索」，
    #: 加了只是略有提升，所以**默认为空**。留空意味着入库向量与查询向量
    #: 落在同一个空间里，不存在「忘了加前缀导致两侧偏移」的风险。
    #: 若要启用（短查询检索长文档场景），中文模型填：
    #: `为这个句子生成表示以用于检索相关文章：`
    EMBEDDING_LOCAL_QUERY_PREFIX: str = ""

    #: 向量维度。两个后端共用同一个值。
    #: `api` 后端无法自动探测（要调用一次才知道），只能靠这里声明；
    #: `local` 后端会读模型的 `config.json` 交叉校验，不一致直接报错。
    EMBEDDING_DIMENSION: int = 1536

    # ---------- Milvus ----------
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_COLLECTION: str = "knowledge_base"
    #: 认证用户名。**留空 = 不发送凭据**，与启用认证前的行为逐字一致 ——
    #: 默认留空是有意的：Milvus 的认证是服务端开关（`COMMON_SECURITY_DEFAULTROOTPASSWORD`
    #: / `authorizationEnabled`），客户端多带一个用户名字段只会让「本地没开认证」
    #: 的环境平白多一个可能对不上的参数。
    #:
    #: ⚠️ 开了认证却不填这里，症状是 `ensure_collection` 抛连接/鉴权错误
    #: → 启动日志说「Milvus 未就绪」→ RAG 静默退化成纯 LLM 直答（无来源标注）。
    #: 它看起来像「Milvus 没起来」，其实进程活得好好的 —— 所以这里不留默认密码，
    #: 逼着配置的人明确写一次。
    MILVUS_USER: str = ""
    MILVUS_PASSWORD: str = ""

    # ---------- Redis ----------
    REDIS_URL: str = "redis://localhost:6379/0"

    # ---------- Elasticsearch (Phase 3 关键词召回 / BM25) ----------
    ES_ENABLED: bool = True                # 关闭则退化为纯向量检索
    ES_URL: str = "http://localhost:9200"
    ES_USERNAME: str = ""                  # 本地关闭 xpack.security 时留空即可
    ES_PASSWORD: str = ""
    ES_INDEX: str = "xjms_knowledge"
    ES_TIMEOUT_SECONDS: float = 10.0
    #: 「探测类」请求（`ensure_index` 的 `exists`、`ping`）的超时。
    #:
    #: 探测只要一个是非题（现在通不通），不需要 elastic_transport 那套
    #: 「重试 3 次 + 退避」（实测约 9 秒）—— 那只是把同一个「不通」排练三遍，
    #: 而排练的这段时间是用户在等。配合 `max_retries=0` 后，
    #: 「连接被拒」这类失败立刻返回，探测不再是延迟来源。
    ES_PROBE_TIMEOUT_SECONDS: float = 2.0
    #: ES 被判不可用后的冷却期（秒）。期间**不再发起连接尝试**，直接按空结果降级。
    #:
    #: 为什么需要：ES 客户端的连接失败自带 3 次重试 + 退避（实测约 9 秒），
    #: 而 `ensure_index()` 每次都会重发这个握手 —— 没有冷却期的话，
    #: **用户每问一句都要陪我们白等**（实测被检索层的单路预算切在 5 秒）。
    #: 降温后这 5 秒变成约 0.1 秒。取 30s 是折中：ES 中途被拉起来时，
    #: 最多 30 秒后自动恢复，不需要重启后端。
    ES_UNAVAILABLE_COOLDOWN_SECONDS: float = 30.0

    # ---------- 混合检索 (Phase 3) ----------
    # hybrid: 向量召回 + BM25 召回 → RRF 融合 → 精排
    # vector: 只走向量（等价 Phase 1 行为，便于做对照实验）
    # bm25:   只走关键词（用于验证精确匹配场景）
    RETRIEVAL_MODE: str = "hybrid"
    HYBRID_VECTOR_TOP_K: int = 20          # 向量这一路的召回条数
    HYBRID_BM25_TOP_K: int = 20            # BM25 这一路的召回条数
    HYBRID_RRF_K: int = 60                 # RRF 平滑常数（经典取值 60）
    HYBRID_VECTOR_WEIGHT: float = 1.0      # RRF 中向量路的权重
    HYBRID_BM25_WEIGHT: float = 1.0        # RRF 中 BM25 路的权重（同时作用于 ES 查询 boost）
    # 单路召回的**时间上限**（三路并发跑，各自独立计时）。
    #
    # 为什么需要它：依赖不可用时客户端会一直等到自己的连接超时才报错
    # （ES 实测 9.3s、Milvus 2.8s），而这条路本来是「可选增益」——
    # 一路连不上，正确表现是迅速退化成少一路召回，而不是让用户为它等满超时。
    # 给一个上限，超过就按空结果处理（与「该路挂了」同语义）。
    #
    # 取值依据：健康状态下三路都在百毫秒级（图路可能到秒级），5s 足够宽松；
    # 它只用来砍掉「连不上 / 卡死」这类异常，不是用来限制正常查询的。
    RETRIEVAL_LANE_TIMEOUT_SECONDS: float = 5.0

    # ---------- Rerank 精排 (Phase 3) ----------
    RERANK_ENABLED: bool = True
    # api:   调用 DashScope 原生重排接口（零下载，开箱可用）
    # local: 进程内 BGE Cross-Encoder（需装 sentence-transformers 并备好模型目录）
    # none:  只输出 RRF 融合结果，不精排
    RERANK_BACKEND: str = "api"
    RERANK_API_URL: str = ""               # 留空则从 LLM_BASE_URL 推导出 DashScope 原生路径
    RERANK_API_KEY: str = ""               # 留空则复用 LLM_API_KEY
    RERANK_MODEL: str = "qwen3.7-text-rerank"
    RERANK_TOP_N: int = 5                  # 精排后保留的条数（注入 Prompt）
    # 送进精排的候选上限（在 RRF 融合结果之上取前 N 条）。
    #
    # 为什么必须截断：Cross-Encoder 的代价是 O(候选数 × 序列长) 的逐对前向，
    # 无法像双塔那样预计算。而融合结果最多有 VECTOR_TOP_K + BM25_TOP_K +
    # GRAPH_TOP_K 条（默认 20+20+20=60），全送进去：
    #   - api 后端：白花带宽与计费额度；
    #   - local 后端：568M 参数的模型在 CPU 上跑 60 对，耗时直接翻三倍。
    # 业界标准做法就是「粗排多召回、精排只重排 Top-20~50」，这里取 20。
    # 取上限而非写死在调用点，是为了让它和三个 *_TOP_K 能分别调。
    RERANK_CANDIDATES: int = 20
    RERANK_TIMEOUT_SECONDS: float = 20.0
    RERANK_LOCAL_MODEL: str = "BAAI/bge-reranker-base"
    RERANK_LOCAL_DEVICE: str = "cpu"
    RERANK_LOCAL_MAX_LENGTH: int = 512
    # 本地 Cross-Encoder 的推理批大小。默认比 embedding 小：
    # reranker 的输入是 (query, 文档) 拼成的长序列，显存/内存占用按
    # batch × max_length 平方级增长（注意力矩阵），调大要谨慎。
    RERANK_LOCAL_BATCH_SIZE: int = 8
    # torch 推理线程数，0 = 交给 torch 自己决定（通常 = 物理核数）。
    # 留这个开关是因为它和 FastAPI 的线程池会互相抢核：
    # 机器同时跑着 Milvus / ES 时，把 torch 限到一半核往往整体更快。
    RERANK_LOCAL_THREADS: int = 0

    # ---------- MCP 工具接入 ----------
    MCP_ENABLED: bool = True
    #: MCP Server 的清单文件（**唯一事实来源**，设置页「MCP」写的就是它）。
    #: 里面可能有 token，与 `llm_profiles.json` 同一风险面：本机单用户，明文存，
    #: 由 `.gitignore` 覆盖，出参只给键名与掩码（见 `app/mcp_store.py`）。
    MCP_STORE: str = "./data/mcp/servers.json"
    #: ⚠️ 现在只是**首次种子**：`MCP_STORE` 文件不存在时读一次并落盘，之后不再回读。
    #: 留在这里是为了"拷一份 .env 就能带出这台机器的接入配置"，
    #: 而不是"改了它就生效" —— 后者已经被文件取代（改完要重启，界面也看不见状态）。
    #: 见 `app/mcp_store.py` 模块注释。
    MCP_SERVERS: str = "[]"
    MCP_TIMEOUT_SECONDS: float = 30.0
    #: 建立连接（握手 + 拉工具清单）的总时限（秒）。
    #: 和 `MCP_TIMEOUT_SECONDS` 分开是因为它们卡在不同地方：后者是"一次工具调用太久"，
    #: 前者是"这个 server 到底起不起来"。没有它，一个 `npx` 冷启动卡死的 server 会让
    #: 设置页保存请求**一直挂着转圈**（stdio 子进程还可能变成没人收的孤儿）——
    #: 界面能写配置，就必须保证写配置这个动作会结束。
    MCP_CONNECT_TIMEOUT_SECONDS: float = 60.0
    #: 连不上的 server 下一次自动重连的间隔（秒）。
    #: 0 = 不做后台重试，只在用户点「连接测试」或调用工具时再试。
    #: 默认给一个不小的值：远端服务重启几分钟是常态，
    #: 每 3 秒打一次 connection refused 会把日志刷满，也会让 CPU 空转。
    MCP_RETRY_SECONDS: float = 120.0

    # ---------- FastAPI ----------
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # ---------- 文件上传 ----------
    UPLOAD_DIR: str = "./data/uploads"
    MAX_FILE_SIZE_MB: int = 50

    # 本地办公转换引擎（Filee）：留空时从 PATH 探测，不自动下载或安装。
    OFFICE_FILEE_PATH: str = ""
    OFFICE_CONVERT_TIMEOUT_SECONDS: int = Field(default=120, ge=1, le=1800)

    # ---------- 对话附件（临时上下文，不入知识库） ----------
    # 与「知识库上传」是两条路径：附件只进本轮上下文，过期自动清理，
    # 不会把一次性的临时文件永久污染知识库。
    ATTACHMENTS_DIR: str = "./data/attachments"
    # 单个附件注入提示词的最大字符数（超出截断并标注）
    ATTACHMENT_MAX_CHARS: int = 24000
    # 附件保留时长（小时）：过期文件在下次有人上传时顺带清理
    ATTACHMENT_TTL_HOURS: int = 24

    # ---------- 产物版本（每次成功写入留一份快照） ----------
    # 只服务沙箱内的产出文件：用户自己的授权目录不进这套版本库。
    ARTIFACT_REVISIONS_DIR: str = "./data/artifact_revisions"
    # 单个文件超过这个体积就只记「写入过」，不复制内容（避免一张大附件撑爆数据盘）
    ARTIFACT_REVISION_MAX_MB: int = 20
    # 同一路径最多保留多少个版本；超出丢弃最旧的
    ARTIFACT_REVISION_KEEP: int = 50

    # ---------- Agent 文件工具沙箱 (Phase 2) ----------
    # file_agent / pc_agent / software_agent 的读写删除操作被限制在该目录内，防止越权访问
    FILE_SANDBOX_DIR: str = "./data/workspace"
    # 沙箱之外、用户显式授权给 Agent 的目录（逗号或分号分隔，可含 ~ 与相对路径）。
    # 办公类产品要碰真实文件（桌面/下载/文档）才有用，但默认**为空** = 只允许沙箱，
    # 越界访问必须由用户在这里明确列出，不能由模型或工具自己决定。
    AGENT_ALLOWED_DIRS: str = ""

    # ---------- 工作区（界面上的「工作区」= 一个用户授权的本地目录） ----------
    # 与 `AGENT_ALLOWED_DIRS` 是同一件事的两个入口：那份写在 `.env` 里、要重启，
    # 这一份由用户在输入框旁边点出来的目录选择器维护，随时增删。
    # 落盘分两张表：清单本身 + 「哪个会话正在用哪个工作区」。
    WORKDIR_STORE: str = "./data/workdirs/workdirs.json"
    WORKDIR_BINDINGS_STORE: str = "./data/workdirs/bindings.json"

    # ---------- Phase 2 Agent 运行时 ----------
    AGENT_MAX_TOOL_STEPS: int = 6          # 单轮最多工具调用步数（防死循环）
    APPROVAL_TIMEOUT_SECONDS: int = 300    # HITL 审批等待超时（超时视为拒绝）
    ENABLE_GRAPH: bool = True              # 是否启用 LangGraph 多 Agent 模式
    # ---------- 联网搜索（百炼 Responses API 内置 web_search） ----------
    # `web_search` 的整次调用预算（连接阶段另收紧到 3s，见该工具模块注释）。
    #
    # 搜索是**可选增益**：搜不到时正确表现是尽快告诉模型「搜不了」，让它用自己的知识答。
    # 但这条路径是「服务端模型带工具真实检索 + 生成摘要」的完整子调用，预算必须盖住它。
    # ⚠️ 预算跟着搜索子调用模型走：qwen3.7-flash ~6s 时 20s 够用；换 qwen3.8-max 后
    # 实测 15~20s（2026-10-04，flash 额度耗尽被迫换档）—— 40s 是新档位的取舍，
    # 网关真死了仍靠 3s 的连接预算快速失败。
    WEB_SEARCH_TIMEOUT_SECONDS: float = 40.0
    # 「网页抓取」路（query 含网址时自动启用 web_extractor）的预算。
    # ⚠️ 网关硬约束：web_extractor 必须开思考（非思考模式 400，实测），
    # 开思考的「抓取+摘要」实测：qwen3.7-flash ~23s，qwen3.8-max 28~60s+
    # （思考方差大）—— 120s 盖住慢档，抓不到时反正会走诚实降级。
    WEB_FETCH_TIMEOUT_SECONDS: float = 120.0
    # 以下三项留空 = 复用 LLM_* 同名配置（同一网关、同一把钥匙，实测可用）。
    WEB_SEARCH_MODEL: str = ""
    WEB_SEARCH_BASE_URL: str = ""
    WEB_SEARCH_API_KEY: str = ""
    # 关思考：实测同一查询开思考 74s / 关思考 6.3s，思考全烧在没人看的内心独白上。
    # ⚠️ 该参数是 Qwen 系网关扩展；换成非 Qwen 后端若报 400，把这项设为 false。
    WEB_SEARCH_DISABLE_THINKING: bool = True

    # ---------- Phase 4: 链路追踪（可观测层） ----------
    # 关闭后不采集 trace，但没有任何功能依赖它（纯观测，不影响问答）
    OBSERVABILITY_ENABLED: bool = True
    TRACE_DIR: str = "./data/traces"       # JSONL 落盘目录（按天分文件）
    # 内存环形缓冲条数。它服务「看最近发生了什么」；更早的历史走文件 grep，
    # 所以不必把上限调太大 —— 追踪数据的价值随时间衰减得很快。
    TRACE_MAX_MEMORY: int = 500
    # 启动时从磁盘回填多少条历史 trace 进内存。
    # 设为 0 会导致「重启后追踪面板空白、但文件里明明有数据」的困惑现象。
    TRACE_LOAD_ON_START: int = 100
    # 保留期（天）：启动时删除早于该天数的 traces-YYYY-MM-DD.jsonl 分片。
    # 0 = 永不清理（磁盘数据是排查证据，需要主动决策才删；没有 HTTP 删除接口是刻意的）。
    TRACE_RETENTION_DAYS: int = 30

    # ---------- 用量账本（token 消耗流水） ----------
    # 与 trace 是两本账：trace 是**诊断数据**，30 天一滚、内存只留 500 条；
    # 账本是**消耗事实**，只增不删、不设保留期（见 app/usage/ledger.py 的理由）。
    USAGE_LEDGER_ENABLED: bool = True
    USAGE_LEDGER_DIR: str = "./data/usage"     # JSONL 落盘目录（按天分文件）
    # 单个分片读取进内存聚合时的行数上限。正常一天几百行，触不到；
    # 它只防「手工往账本里塞了个巨型文件」这一种情况。
    USAGE_LEDGER_MAX_ROWS_PER_FILE: int = 200000

    # ---------- Phase 4b: 语音（STT 输入 + TTS 播报） ----------
    # 语音是入口/出口增强，不在 Agent 主链路上：挂了只是按钮不可用，问答照常。
    VOICE_ENABLED: bool = True
    # STT 双后端：local（sherpa-onnx + SenseVoice，离线、默认）/ dashscope（云端 qwen3-asr-flash）。
    # 云端走 DashScope 原生接口。
    # ⚠️ OpenAI 兼容层的 ASR 只收公网音频 URL、不吃 base64（实测 400），必须走原生。
    VOICE_STT_BACKEND: str = "local"
    # 云端模型名（仅 dashscope 后端使用）
    VOICE_STT_MODEL: str = "qwen3-asr-flash"
    # 本地模型目录（留空 = 本地模型根目录下的 sherpa-onnx-sense-voice-…，
    # 根目录的约定同 LOCAL_MODEL_DIR）
    VOICE_STT_LOCAL_MODEL: str = ""
    VOICE_STT_TIMEOUT_SECONDS: float = 60.0
    # 桌面助手唤醒（sherpa-onnx KWS）模型目录；留空 = 模型根目录下的
    # sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01
    VOICE_WAKE_MODEL: str = ""
    # 上传音频大小上限（MB）。手机录音一分钟 wav ≈ 2MB，10MB 已远超单轮提问所需
    VOICE_MAX_AUDIO_MB: float = 10.0
    # TTS 双后端：dashscope（qwen3-tts，效果好，默认）/ edge（免费兜底）
    VOICE_TTS_BACKEND: str = "dashscope"
    VOICE_TTS_MODEL: str = "qwen3-tts-flash"
    VOICE_TTS_VOICE: str = "Cherry"             # dashscope 音色（Cherry / Ethan / …）
    VOICE_TTS_VOICE_EDGE: str = "zh-CN-XiaoxiaoNeural"
    VOICE_TTS_RATE: str = "+0%"                # edge 后端语速（如 -10% 略慢）
    VOICE_TTS_FORMAT: str = "mp3"
    VOICE_TTS_TIMEOUT_SECONDS: float = 60.0

    # ---------- Phase 4b: GraphRAG（实体关系图 + 多跳检索） ----------
    # 图是**第三路可选召回**：关掉或 Neo4j 不可用时，混合检索自然退化为
    # 向量 + BM25 两路，问答完全不受影响（与 ES / 精排同样的降级哲学）。
    #
    # ⚠️ 注意与「构建图」的成本区分：
    #   读（检索）是免费的，所以默认 True —— 建好了图就能立刻受益；
    #   写（构建）每块要打一次模型，所以**不做成自动任务**，只有
    #   `scripts/build_graph.py` 与 `POST /api/graph/build` 两个显式入口。
    #   这跟 INDEX_WATCH_ENABLED 是同一套取舍：主动花钱的开关一律 opt-in。
    GRAPH_ENABLED: bool = True
    GRAPH_URI: str = "bolt://localhost:7687"
    GRAPH_USER: str = "neo4j"
    GRAPH_PASSWORD: str = "xjms-graph-2026"
    GRAPH_DATABASE: str = "neo4j"
    GRAPH_TIMEOUT_SECONDS: float = 10.0
    #: Neo4j 被判不可用后的冷却期（秒）。期间不再尝试建连，图路直接按空结果降级。
    #:
    #: 为什么需要（与 `ES_UNAVAILABLE_COOLDOWN_SECONDS` 同一类问题）：
    #: `_ensure_driver` 失败后会把驱动句柄丢掉，于是**下一个请求又会重建并重连**，
    #: 一次完整尝试要等满 `GRAPH_TIMEOUT_SECONDS`。Neo4j 没起来时，
    #: 用户每问一句都要在图这一路白等 5 秒（实测端到端 `sources` 被推到 5.05s，
    #: 本应约 2.7s）—— 而图路只是三路召回里的一路可选增益。
    #: 取 30s 是折中：Neo4j 中途被拉起来时最多 30 秒后自动恢复。
    GRAPH_UNAVAILABLE_COOLDOWN_SECONDS: float = 30.0
    # 多跳展开的上限。1 = 只看直接邻居；2 = 「查询实体 → 中间实体 → 答案块」。
    # 再往上收益急剧衰减而噪声线性增长（一个 3 跳外的块与查询的相关性
    # 已经弱到无法解释），所以默认 2，不要为了“看起来更强”往上调。
    GRAPH_MAX_HOPS: int = 2
    # 图路自己的候选条数。与 HYBRID_VECTOR_TOP_K / HYBRID_BM25_TOP_K 同量级 ——
    # RRF 只看名次，三路的池子大小应当可比，否则某一路会因池子大而占便宜。
    GRAPH_TOP_K: int = 20
    # RRF 中图路的权重。默认 1.0 与另外两路平权：多跳是**补充**不是**覆盖**，
    # 调高会让远端块压过直接相关的块（症状：简单问题的答案排到后面去了）。
    GRAPH_WEIGHT: float = 1.0
    # 参与查询匹配的实体最小长度。太短的实体（单字）几乎必然误匹配。
    GRAPH_MIN_ENTITY_CHARS: int = 2
    # ★ 多跳证据保底槽（本组配置里最重要的一条）
    #
    # RRF 按「被几路命中」投票，只被图路召回的块天然只有一票，必然排在
    # 「两路共识」的块之后 —— 无论它的图路径多么可信。也就是说，
    # **纯 RRF 排序下多跳证据几乎不可能进入最终结果**，
    # 图检索会变成“建了但看不出效果”。
    #
    # 这个参数为「仅被图路召回、且跳数 ≥ 1」的候选保留几个最终名额：
    # 它们被追加在结果末尾（最低优先级），占用的是上下文的一条额度。
    # 设 0 可关闭（回到纯 RRF 行为，用于对照实验）。
    GRAPH_MIN_MULTIHOP: int = 1
    # 构建时的并发上限与批大小（打模型 + 写图）
    GRAPH_BUILD_CONCURRENCY: int = 4
    GRAPH_BUILD_BATCH_SIZE: int = 200
    # 抽取用模型；留空则复用 LLM_MODEL
    GRAPH_EXTRACT_MODEL: str = ""
    GRAPH_EXTRACT_TIMEOUT_SECONDS: float = 60.0
    # 单块送入抽取的字符上限（chunk 本身约 500 字，留足余量即可）
    GRAPH_EXTRACT_MAX_CHARS: int = 2000

    # ---------- Phase 6: 审批留痕与批量审批 ----------
    # 审批记录是**证据**，必须落盘：进程崩了、容器重启了，“谁在什么时候批了什么”
    # 这件事不能因为一次重启就消失（HITL 的审计价值全在这份记录上）。
    # 与长期记忆同样的取舍：文件是唯一事实来源，不依赖 Redis。
    APPROVAL_RECORD_ENABLED: bool = True
    APPROVAL_RECORD_PATH: str = "./data/approvals/records.jsonl"
    # 内存里保留多少条供接口查询（更早的历史走文件 grep）
    APPROVAL_RECORD_MEMORY: int = 500
    # 留痕保留期（天）。超过则从 records.jsonl **归档**到 archive/records-YYYY-MM.jsonl
    # 而非直接删除 —— 审批记录是审计证据，宁占磁盘不可丢失。
    # 0 = 永不归档（默认 90 天，与 trace 的 30 天不同：审批是低频高价值事件）
    APPROVAL_RETENTION_DAYS: int = 90
    # 预授权（批量放行）的默认有效期与使用次数上限。
    # 预授权是“把一次点击授权给 N 次同类操作”，因此必须有硬边界 ——
    # 无上限的预授权等于把 HITL 关掉，而用户不会意识到自己关掉了它。
    APPROVAL_GRANT_TTL_SECONDS: int = 900
    APPROVAL_GRANT_MAX_USES: int = 10

    # ---------- 模型主动提问（结构化问答，与审批是两条路） ----------
    # 审批由宿主判定「这一步危险」，提问由模型声明「信息不够」。
    # 两者共用挂起/恢复机制，但事件类型、审计落点、前端卡片都刻意分开 ——
    # 混成一种事件会让「用户点了批准」与「用户回答了问题」变成同一份数据，
    # 事后无法回答「这次到底是授权还是澄清」。
    #
    # 等待时长比审批更长：回答一段业务问题比点「允许/拒绝」需要更多阅读时间。
    QUESTION_TIMEOUT_SECONDS: int = 600
    QUESTION_RECORD_ENABLED: bool = True
    QUESTION_RECORD_PATH: str = "./data/questions/records.jsonl"
    QUESTION_RECORD_MEMORY: int = 500
    # 与审批留痕同口径：问答记录是审计证据，过期归档而不是删除。
    QUESTION_RETENTION_DAYS: int = 90

    # ---------- Phase 5: 权限模式 / 规则 / Hooks ----------
    # 权限模式与规则是**用户对本机做的决定**，必须由界面写、由界面读，
    # 不能留在 `.env` 里让人重启改文件（同 `MCP_STORE` 的取舍，见 mcp_store 注释）。
    # `.env` 的 `PERMISSION_MODE` 只提供**出厂默认**：文件里没有记录时用它一次。
    PERMISSION_MODE: str = "manual"
    PERMISSION_MODE_STORE: str = "./data/permissions/mode.json"
    PERMISSION_RULES_STORE: str = "./data/permissions/rules.json"
    #: 电脑操控的总开关（仅对助理会话生效）。默认关闭：这是一项「助理可以直接
    #: 操作你正在用的桌面」的授权，必须由用户在设置页点一次，不能装完就生效。
    COMPUTER_CONTROL_STORE: str = "./data/computer_control.json"
    #: 规则条数上限。规则是「一条命中就改变行为」的短句柄，
    #: 几百条之后没人能读出「这次为什么被拦」，所以宁可实现。
    PERMISSION_MAX_RULES: int = 200

    # Hooks = 用户写的命令行脚本，在工具调用前后自动执行。
    #: ★ 出厂**关闭**。它是「用户授权的一段本机代码」，
    #: 而一次没打招呼的代码执行，比一百个 danger 工具都危险。
    HOOKS_ENABLED: bool = False
    HOOKS_STORE: str = "./data/hooks/hooks.json"
    #: 单个钩子的超时（秒）。钩子卡住 = 整条对话卡住，所以它必须有上限。
    HOOKS_TIMEOUT_SECONDS: int = 20
    #: 钩子执行留痕（谁在什么时候跑了什么、退出码多少）
    HOOKS_RUN_LOG: str = "./data/hooks/runs.jsonl"
    HOOKS_RUN_MEMORY: int = 200

    # ---------- Phase 7: 会话历史（保存 / 浏览 / 恢复） ----------
    # 为什么落盘而不是复用 Redis Checkpointer：
    #   ① Checkpointer 存的是**图状态**（LangChain Message 对象），只够喂给模型，
    #      拿不回前端要的富信息（引用来源 / 工具调用卡片 / 审批留痕 / 记忆回执）；
    #   ② Redis 在本项目是**可降级依赖**（挂掉退化成 InMemorySaver），
    #      把它当会话历史的唯一归宿，等于「重启一次聊天记录就没了」——
    #      这与长期记忆的选择同源（见 decisions/0010）。
    # 会话历史与长期记忆的差别在**写入频率**：记忆是低频整文件写，
    # 历史是每轮对话一条，所以走 append-only 事件日志（同审批留痕，见 decisions/0012）。
    CONVERSATION_ENABLED: bool = True
    CONVERSATION_DIR: str = "./data/conversations"
    # 索引里保留的会话数上限。超出后按「最后活跃时间」淘汰最旧的（连同它的轮次文件）。
    # 单用户桌面场景下几百个会话是很久才会到达的量级；设 0 = 不做上限。
    CONVERSATION_MAX_ITEMS: int = 200
    # 列表接口单次返回条数上限（防止一次把几百个会话都推给前端）
    CONVERSATION_LIST_LIMIT: int = 100
    # 会话标题（取首轮用户输入）截断长度
    CONVERSATION_TITLE_CHARS: int = 40
    # 首轮结束后**异步**用模型生成一个更短的标题（把「在吗」变成「报销流程咨询」）。
    # 关掉它就退回「首轮输入截断」这一种标题，功能不受影响 ——
    # 所以它可以在没有模型网关的环境里安全地置 false。
    CONVERSATION_AUTO_TITLE: bool = True
    # 自动标题的单次超时。它是**后台旁路**任务，不占用户的等待，所以给得宽松：
    # 本机实测该网关（`qwen3.7-flash`）单次要 15~20 秒 ——
    # 与 `api/chat.py` 注释里那条「首字延迟可达十几秒」同源。
    # 但也必须有上限：一个标题不值得长时间占着一个连接。
    CONVERSATION_TITLE_TIMEOUT_SECONDS: float = 30.0
    # 恢复上下文：打开历史会话时，若 Checkpointer 里已经没有这个 thread 的状态
    # （Redis 重启/换数据卷/降级成 InMemorySaver），就从历史轮次**播种**回去，
    # 否则「界面显示着 10 轮历史，模型却完全不记得」这种不一致会让人以为是 bug。
    CONVERSATION_REHYDRATE: bool = True

    # ---------- 工作空间模块（群组 + 数字员工）----------
    #
    # 这些实体之间有真实的外键关系与级联（删工作空间要连带删群组/成员/员工/消息），
    # 文件型要手工维护引用完整性，而 SQLite 的 `FOREIGN KEY ... ON DELETE CASCADE`
    # 一行闭环。因此本模块单独走关系库，其余栈（对话历史/子任务/记忆）保持文件型不动。
    WORKSPACE_ENABLED: bool = True
    # ★ 复数 `workspaces`：与既有文件沙箱 `data/workspace`（FILE_SANDBOX_DIR）区分，
    # 避免「工作空间模块的库」与「Agent 文件工具沙箱」撞同一个目录名。
    WORKSPACE_DB_PATH: str = "./data/workspaces/workspace.db"
    # 项目 ↔ 知识来源的边界映射（ADR 0022 阶段 5b）。
    # ★ 为什么不进 workspace.db：它描述的是**索引侧**的归属（source 前缀），
    # 与员工/群组这些实体没有外键关系；放进关系库就要为一张两列的表加迁移，
    # 而它坏了的正确退路是「当成没有边界」—— 一份空 JSON 就能表达。
    KNOWLEDGE_SCOPE_STORE: str = "./data/workspaces/knowledge_scope.json"

    # ---------- 任务板与认领（施工图 §5）----------
    #
    # ★ 这几个上限都进 CAS/循环的**判定条件**，不是展示用的数字：
    # 改它们会改变并发行为，所以集中在配置里给一处，不散在代码常量里。
    #: 一名员工同时最多几张卡在飞。认领那条 UPDATE 的 WHERE 里带它
    #: （`§5.2`），所以它是**原子**的限流 —— 在 Python 里先查再写就不成立，
    #: 认领轮会并发问同一个人两次。
    BOARD_MAX_INFLIGHT_PER_EMPLOYEE: int = 1
    #: 认领轮最多跑几轮。每轮 = 每名被邀请成员一次短上下文调用，
    #: 所以这个数直接是 token 乘法（ADR 0020 的「后果」里记着它约 +30~60%）。
    WORKSPACE_MAX_LLM_CALLS: int = 80
    WORKSPACE_MAX_TOKENS: int = 160000
    WORKSPACE_MAX_SECONDS: int = 3600
    WORKSPACE_MAX_RETRIES: int = 6
    WORKSPACE_DELIVERY_MAX_ATTEMPTS: int = 3
    WORKSPACE_DELIVERY_TIMEOUT_SECONDS: int = 1800
    CLAIM_ROUNDS_MAX: int = 2
    #: 一轮认领最多摊给一名员工几张卡（只给摘要，不给全文）。
    OFFER_BATCH_MAX: int = 4
    #: 单张卡在认领轮输入里的摘要上限。超了不是砍尾，是这张卡不参与本轮
    #: （砍尾会让人以为模型读过了整张卡）。
    CLAIM_CARD_BRIEF_CHARS: int = 200
    #: 连续几轮「一个新人也没接上活」就算推不动了（护栏 3）。
    #: ★ 它数的是**连续**空轮、落在 `runs.stall_count` 上，与停滞检测共用同一个计数器；
    #: `CLAIM_ROUNDS_MAX` 是「一次循环最多问几遍」的上限，两者口径不同：
    #: 一个 run 里认领循环会被不同事件触发多次，空轮跨次累计，有进展则归零。
    BOARD_STALL_LIMIT: int = 2

    # ---------- 同事互聊与交付摘要（施工图 §4.4 / §5.7，2026-10-16 提到本期）----------
    #
    # ★ 这三样是「互聊」的上界，不是调优旋钮
    # 定向消息一旦放开，模型调用数就从可算的 `轮数 × 成员数` 变成没有上界。
    # 护栏 1（预算）、4（环）、5（深度）各自掐掉一种发散形状，缺一条就是聊天室。
    #: 护栏 1：一次 run 里四类定向消息（request/commitment/deliverable/blocker）的总条数上限。
    NEGOTIATION_MAX_MESSAGES_PER_RUN: int = 24
    #: 护栏 5：一张卡最多被转手几次（`run_tasks.handoff_depth`）。
    HANDOFF_MAX_DEPTH: int = 3
    #: 一次认领轮里「指名我的话」的条数上限（只给摘要，与卡片同一口径）。
    INBOX_MAX: int = 3
    #: 群聊单条消息的正文上限（禁令 2）。超长处置是「摘要 + 指向私聊」，不是砍尾。
    GROUP_SUMMARY_CHARS: int = 800
    #: 交付摘要喂给模型的结果全文上限（§4.4：截断必须明说）。
    REPORT_INPUT_CHARS: int = 6000

    # ---------- Phase 9: Supervisor 后台子任务 ----------
    #
    # ★ 为什么任务状态落**文件**而不复用 Redis Checkpointer
    #
    # 与会话历史同源（见上面 CONVERSATION_DIR 的注释，decisions/0016）：
    # Redis 在本项目是**可降级依赖**（挂掉退化成 InMemorySaver），
    # 而后台任务状态恰恰是「重启最不该丢」的东西 ——
    # 丢了不会让任务停跑（子 Agent 照跑、结果照落盘），
    # 但用户和主 Agent 都**查不到**它，界面上就成了一批「看不见的孤儿」。
    # 所以这里走与 ConversationStore 同构的「索引 + 每任务一个 append-only 文件」。
    SUBTASK_ENABLED: bool = True
    SUBTASK_DIR: str = "./data/tasks"
    # 索引里保留的任务数上限，超出后按最后活跃时间淘汰最旧的。
    # 比会话数小得多：一个子任务的 jsonl 远大于一轮对话。
    SUBTASK_MAX_ITEMS: int = 200
    # 全局同时运行的子任务数。超出则**排队**（不报错，见不变量 14）。
    #
    # ★ 为什么不设更大
    # 瓶颈在模型网关而不是本地：本机实测单次 LLM 调用 2~20 秒，
    # 再往上加并发只是让预算与限流一起爆掉，而单用户桌面场景
    # 一次也拆不出五六个互不依赖的子任务。
    SUBTASK_MAX_CONCURRENCY: int = 3
    # 单轮最多派发几个子任务。超出时截断计划并**告知用户** ——
    # 静默丢弃第 5 个任务会让人以为它已经派出去了（不变量 52 的同类）。
    SUBTASK_MAX_PER_TURN: int = 4
    # 服务启动时把上次留下的非终态任务结算掉。
    # ⚠️ 建议保持开启：不开启的话，进程被杀时留下的 running 任务
    # 会永远停在那里，界面上是一个转不停的 spinner。
    SUBTASK_SWEEP_ON_START: bool = True

    # ---------- 后台任务完成汇报（总 Agent 向用户汇报结果） ----------
    TASK_REPORT_ENABLED: bool = True
    # 任务终态到达后等多久再汇报：把几乎同时结束的几个任务并成一次汇报。
    TASK_REPORT_DEBOUNCE_SECONDS: float = 2.5
    # 单条任务结果正文进汇报提示词的最大字符数（太长挤爆上下文）。
    TASK_REPORT_RESULT_CHARS: int = 1500
    # 汇报生成失败 / 中途被杀时最多重试几次（每次任务终态触发与启动补扫都算一次机会）。
    TASK_REPORT_MAX_ATTEMPTS: int = 2
    # 汇报生成的超时与长度。汇报是**旁路增益**：失败退回固定文案兜底，绝不重试等待。
    TASK_REPORT_TIMEOUT_SECONDS: float = 90.0
    TASK_REPORT_MAX_TOKENS: int = 1200

    # ---------- 定时任务（cron 式周期触发，跑的是同一套后台子任务执行器） ----------
    SCHEDULED_ENABLED: bool = True
    SCHEDULED_DIR: str = "./data/schedules"
    # 调度循环的扫描间隔。到点判定是 `next_run_at <= now`，
    # 所以触发精度 ≈ 这个值；30 秒对「每天 09:00」这类语义绰绰有余，
    # 再小只是空转（单用户桌面没有秒级触发的场景）。
    SCHEDULED_TICK_SECONDS: float = 30.0
    # 进程停机后允许**补跑**的错过窗口（分钟）。超过它的不补跑、只推进 ——
    # 「今早 8 点的日报」在下午 3 点重放只会产出过期内容 + 白烧 token。
    SCHEDULED_CATCHUP_MINUTES: int = 30
    # 每个任务在磁盘上保留的运行记录条数（只淘汰已结算的）。
    SCHEDULE_MAX_RUNS_PER_SCHEDULE: int = 200

    # ---------- 可视化工作流 ----------
    WORKFLOW_ENABLED: bool = True
    WORKFLOW_DIR: str = "./data/workflows"
    # 每个工作流在磁盘上保留的运行记录条数（只淘汰已结算的）。
    WORKFLOW_MAX_RUNS_PER_WORKFLOW: int = 100

    # ---------- 外部渠道（微信这类 IM 入口） ----------
    # 微信 iLink（ClawBot）扫码绑定后的账号落盘位置。
    # ⚠️ `bot_token` 是密钥：只进这个目录，不进 .env（那是要人手改的文件），
    # 也不进会话数据（那是要导出、要回放的东西）。见 api/channels.py 的读取侧。
    CHANNELS_WECHAT_DIR: str = "./data/wechat"

    # ---------- Phase 4: LangFuse 旁路导出（可选） ----------
    # 走官方 ingestion REST API，**不依赖 langfuse SDK**（见 observability/exporters.py）
    LANGFUSE_ENABLED: bool = False
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_TIMEOUT_SECONDS: float = 10.0

    # ---------- Phase 4: 评估体系 ----------
    EVAL_GOLDEN_SET: str = "./data/eval/golden_set.json"
    EVAL_REPORT_DIR: str = "./data/eval/reports"
    # 答案质量评估后端：
    #   builtin —— 内置 LLM-as-judge（零新增依赖，默认）
    #   ragas   —— 官方 Ragas 库（需 pip install -e ".[eval]"，会降级 langchain 版本）
    EVAL_JUDGE_BACKEND: str = "builtin"
    EVAL_JUDGE_MODEL: str = ""             # 留空则复用 LLM_MODEL
    EVAL_JUDGE_TIMEOUT_SECONDS: float = 60.0
    # 单次评估最多并发判定多少条样本（评估要打真实模型，串行会非常慢）
    EVAL_JUDGE_CONCURRENCY: int = 4

    # ---------- Phase 5: 增量索引（内容指纹驱动的变更检测） ----------
    # ⚠️ 默认关闭。开启后后台会周期性扫描目录，发现变更就会调 Embedding 重新入库
    #    —— 也就是**会花钱**。与 LANGFUSE_ENABLED / 答案层评估同样的默认策略：
    #    主动产生成本的开关一律 opt-in，而不是替用户做决定。
    #    不开也能用：POST /api/index/scan 可随时手动触发一次。
    INDEX_WATCH_ENABLED: bool = False
    INDEX_WATCH_DIRS: str = ""   # 逗号分隔，支持多个目录；留空 = 不监听任何目录
    INDEX_WATCH_INTERVAL_SECONDS: float = 60.0      # 两轮扫描的间隔
    INDEX_MANIFEST: str = "./data/index_manifest.json"   # 指纹清单（增量判断的依据）
    #: 重建向量库时的**中转转储**（每行一个块的 JSON）。
    #:
    #: 为什么需要它：换 embedding 模型必须重建 Collection，而重建要先清空再重灌。
    #: 清空之后如果向量化中途失败（网关抖动、模型目录被移走），库里就是半份数据 ——
    #: 而且**看起来是好的**（标识对、能检索、只是少了一部分块）。
    #: 先把块正文落到这里，失败时重跑一次就能幂等续建，不必让用户重新上传。
    VECTOR_DUMP_PATH: str = "./data/vector_dump.jsonl"
    # 单轮扫描的文件数上限：防止误把一个大目录（如 node_modules）配进来后
    # 一次性把几千个文件推进 Embedding 网关
    INDEX_WATCH_MAX_FILES: int = 2000

    # ---------- Phase 5: 长期记忆 ----------
    # 记忆层是**增益**不是依赖：关掉或存储损坏时，Agent 只是「不认识你」，
    # 问答链路完全不受影响。文件是唯一事实来源（不依赖 Redis，重启不丢）。
    MEMORY_ENABLED: bool = True
    MEMORY_STORE: str = "./data/memory/profile.json"
    MEMORY_USER_ID: str = "default"           # 暂无多用户体系，留出扩展位
    MEMORY_MAX_ITEMS: int = 50                # 注入 Prompt 的偏好条数上限
    MEMORY_INJECT_MAX_CHARS: int = 1200       # 注入 Prompt 的字符上限（防撑爆上下文）
    # 偏好的「未确认即过期」天数：超过该时间未再出现/确认的偏好不再注入 Prompt
    # （0 = 关闭）。数据仍保留在文件中（非破坏性），identity 类别豁免 ——
    # 姓名这类事实不会因为久未提起就失效。过期记忆由用户重新表达即可复活。
    MEMORY_TTL_DAYS: int = 365
    # 默认使用连续衰减；关闭后才使用原 TTL 规则。遗忘只影响召回，不删除数据。
    MEMORY_DECAY_ENABLED: bool = True
    MEMORY_INITIAL_STABILITY_DAYS: float = Field(30.0, gt=0, le=3650)
    MEMORY_FOCUS_STABILITY_DAYS: float = Field(3.0, gt=0, le=3650)
    MEMORY_MAX_STABILITY_DAYS: float = Field(3650.0, gt=0, le=36500)
    MEMORY_RECALL_THRESHOLD: float = Field(0.2, gt=0, lt=0.6)
    MEMORY_REBOUND_GAIN: float = Field(1.5, gt=0, le=10)
    MEMORY_REINFORCE_INTERVAL_HOURS: float = Field(6.0, ge=0, le=8760)
    # 被动抽取：一轮问答结束后后台调一次模型，从对话里推断偏好。
    # 它**不阻塞** SSE 流关闭（fire-and-forget），所以超时可以给得宽松些。
    MEMORY_EXTRACT_ENABLED: bool = True
    MEMORY_EXTRACT_TIMEOUT_SECONDS: float = 30.0
    MEMORY_EXTRACT_MODEL: str = ""            # 留空则复用 LLM_MODEL
    # 关系记忆（「关于用户」）：称呼/生日/作息/喜好等，单独成块注入。
    # 硬上限比通用偏好小得多 —— 它的作用是让助理「记得住人」，不是塞满上下文。
    MEMORY_OWNER_MAX_ITEMS: int = 8
    MEMORY_OWNER_MAX_CHARS: int = 400
    # 旧 TTL 模式的「最近在忙」时效；默认曲线模式使用 MEMORY_FOCUS_STABILITY_DAYS。
    MEMORY_FOCUS_TTL_DAYS: int = 30

    # ---------- 助手人设（前端可编辑） ----------
    # 与记忆同一套取舍：增益不是依赖（文件损坏 → 默认人设继续跑），
    # 文件是唯一事实来源（可 git diff、可备份）。注入点只有 runner 一处；
    # 主 Agent 的意图识别**不吃人设**（路由精度不该被人设影响）。
    PERSONA_STORE: str = "./data/persona/persona.json"

    # ---------- 助理主动问候 ----------
    # 只在用户打开助理会话时尝试一次（前端触发）；没有推送、没有后台轮询。
    ASSISTANT_PROACTIVE_ENABLED: bool = True
    # 同一会话两次自动尝试之间的冷却：前端重连/切页不该反复触发模型调用
    ASSISTANT_PROACTIVE_COOLDOWN_SECONDS: int = 300
    ASSISTANT_PROACTIVE_TIMEOUT_SECONDS: float = 8.0
    ASSISTANT_PROACTIVE_MAX_TOKENS: int = 120

    # ---------- 快捷指令（可复用的问法） ----------
    #: 只落**用户自建**的指令；内置那几条是产品内容，写在 `app/quick_commands.py` 里。
    #: 与模板时代的取舍相同：文件损坏 → 回退到纯内置（增益不是依赖）。
    QUICK_COMMANDS_STORE: str = "./data/quick_commands/commands.json"

    # ---------- 技能（Skills，照 Agent Skills 规范） ----------
    #: 用户自己的技能目录（每个技能一个子目录，里面有 `SKILL.md`）。
    #: 与 memory 同一套取舍：**运行时状态，不入库**，可备份可 diff。
    SKILLS_DIR: str = "./data/skills"
    #: 随仓库打包的内置技能目录（`backend/skills/`，只读）。
    #: 分开两份是因为**改内置技能等于改产品**：它应该走 git，
    #: 而用户随手放一个文件夹进来不应该把出厂技能一起覆盖掉。
    SKILLS_BUNDLED_DIR: str = "./skills"
    #: 界面上的启停状态（`{"技能名": bool}`）。只有覆写项，没有全量清单 ——
    #: 目录里删掉一个技能，不该在状态文件里留下一条永远清不掉的孤儿。
    SKILLS_STATE_STORE: str = "./data/skills/state.json"
    SKILLS_ENABLED: bool = True
    #: 注入 system prompt 的字符上限。技能多到装不下时，块里会**明说**
    #: 「还有 N 个未列出」，模型可以用 `skills_list` 看到全部 ——
    #: 静默截断的症状是「她有技能但不知道」，比多花点 token 贵。
    SKILLS_INJECT_MAX_CHARS: int = 3000

    # ---------- 插件市场（发现 / 安装 / 卸载） ----------
    #: 市场根目录（随仓库走）：`index.json` 是内置目录清单，`packs/` 是可直接安装的技能包。
    #: 与市场状态（装了什么）分开：清单是产品内容，状态是运行时数据（见 MARKET_INSTALLED_STORE）。
    MARKET_DIR: str = "./market"
    #: 远程目录源（可选）。留空 = 只用内置清单；配置后每次刷新去拉一次，
    #: 拉失败不影响内置清单，失败原因单独返回给界面（不做静默降级）。
    MARKET_INDEX_URL: str = ""
    #: 已安装注册表（版本 / 来源 / 组合包成员关系）。
    MARKET_INSTALLED_STORE: str = "./data/market/installed.json"
    #: 「通过 Syncore 创建」的落点：她写好的插件目录与 zip 放这里，市场弹窗再选它装进去。
    #: 单独一个目录是为了让人在文件选择器里只看到候选，不用翻整块硬盘。
    MARKET_STAGING_DIR: str = "./data/market/staging"
    #: 单个技能包下载上限。zip 炸弹的第一道防线是"根本不读完它"。
    MARKET_MAX_DOWNLOAD_BYTES: int = 25 * 1024 * 1024

    # ---------- 智能体注册表（设置页「智能体」写的那份） ----------
    # 专员的职责 / 能力路由词 / 工具白名单 / 模型 / 启停，原先散在四处
    # （`core/prompts.AGENT_DESCRIPTIONS`、`supervisor.CAPABILITY_TO_AGENT`、
    #  工具表各自的 `allowed_agents`、`runner` 里 `== "rag_agent"` 的检索硬门），
    # 加一个专员要改四个地方且必然漂移。这里收成一份文件，代码常量退化为**种子默认值**。
    AGENTS_STORE: str = "./data/agents.json"

    # ---------- 运行时覆写（前端「检索模型」面板写的那份） ----------
    #: 放置运行时覆写的文件。
    #:
    #: ⚠️ **不要**让它和 `.env` 合并成一个文件。`.env` 是**部署配置**：
    #: 人手工写、带密钥、可能进版本控制、回答「这台机器怎么搭」；
    #: 覆写层是**运行时状态**：程序写、是界面上的选择、不入库、回答「现在用哪个模型」。
    #: 合成一份的后果是「界面上改一下就把部署配置改了」，而且下次 `git checkout`
    #: 或者别人拷走 `.env` 时，会连你临时试的模型一起带走。
    #: 分开之后，「这个值从哪来」永远能查 —— 见 `RUNTIME_OVERRIDES`。
    RUNTIME_CONFIG_STORE: str = "./data/runtime_config.json"

    #: LLM profile 目录（ADR 0021 切片 1）：各供应商各自的 Base URL 与 API Key。
    #:
    #: ⚠️ 明文密钥，与 `.env` / 覆写层同一风险面（本机单用户桌面应用），
    #: 必须由 `.gitignore` 覆盖；出参只给掩码（`mask_secret`）。
    #: 与覆写层分开：覆写层记「当前生效的一套」，目录记「每家的凭据」。
    LLM_PROFILES_STORE: str = "./data/llm_profiles.json"

    #: 本地模型目录扫描的根（前端「检索模型」面板列出本机已下载的模型用）。
    #: 留空 = 自动推断：取当前两个本地模型路径的**公共父目录** ——
    #: 比如把 bge-small-zh-v1.5 与 bge-reranker-v2-m3 放在同一本地目录下，
    #: 所以不配也能列出候选，不必为了一个候选清单多记一个路径。
    LOCAL_MODEL_DIR: str = ""

    @field_validator("CORS_ORIGINS")
    @classmethod
    def parse_cors_origins(cls, v: str) -> str:
        """保持字符串形式，在使用时 split"""
        return v

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def milvus_uri(self) -> str:
        return f"http://{self.MILVUS_HOST}:{self.MILVUS_PORT}"

    @property
    def milvus_auth_kwargs(self) -> dict:
        """
        构造 pymilvus 的连接凭据参数。

        ★ 为什么单独抽出来，而不是在 `MilvusManager.client` 里 if 一下

        凭据的**有无**是服务端决定的事实，客户端只能照抄配置。
        抽成一个纯函数式的属性之后，「没配就一个字段都不发」这条规则
        可以被单测直接钉住 —— 它正是「本地不开认证的环境不能因为这次改动受影响」
        的保证。写在客户端里就只能靠读代码确认了。

        留空时返回空字典：pymilvus 的 `user=''` 会被当成"未提供"，
        但那依赖于 SDK 的默认值语义，不如自己不发。
        """
        if not self.MILVUS_USER:
            return {}
        return {"user": self.MILVUS_USER, "password": self.MILVUS_PASSWORD}

    @property
    def upload_path(self) -> Path:
        p = Path(self.UPLOAD_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def sandbox_path(self) -> Path:
        """Agent 文件工具沙箱根目录（自动创建）"""
        p = Path(self.FILE_SANDBOX_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def workdir_store_path(self) -> Path:
        """工作区登记表的落盘路径（父目录自动创建，文件可以不存在）"""
        p = self._resolve(self.WORKDIR_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def workdir_bindings_path(self) -> Path:
        """会话 ↔ 工作区绑定的落盘路径（同上）"""
        p = self._resolve(self.WORKDIR_BINDINGS_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def agent_allowed_dirs_list(self) -> list[Path]:
        """
        沙箱之外授权给 Agent 的目录（已展开 `~`、已解析为绝对路径）。

        不存在的条目被保留（用户可能先配目录再建），由调用方在访问时报错 ——
        静默丢掉一条授权配置，症状会是「明明配了却说没有权限」，很难排查。
        """
        raw = (self.AGENT_ALLOWED_DIRS or "").replace(";", ",")
        out: list[Path] = []
        for item in raw.split(","):
            text = item.strip().strip('"').strip("'")
            if not text:
                continue
            p = Path(text).expanduser()
            out.append(p if p.is_absolute() else (self.backend_root / p).resolve())
        return out

    @property
    def backend_root(self) -> Path:
        """后端项目根目录（backend/），用于解析 MCP Server 脚本等相对路径"""
        return Path(__file__).resolve().parents[1]

    # ---------- Phase 4: 路径解析 ----------
    # 这几个路径的语义是「相对 backend/」，但 uvicorn 的 CWD 未必是 backend/。
    # 直接 Path(relative) 会跟着 CWD 漂移 —— 表现为「评估报告跑到别的地方去了」，
    # 而且不报错。所以一律显式锚定到 backend_root。

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else self.backend_root / p

    @property
    def trace_path(self) -> Path:
        p = self._resolve(self.TRACE_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def usage_ledger_path(self) -> Path:
        """用量账本目录（自动创建）。刻意与 trace 分目录 —— 清理 trace 不该碰到账本。"""
        p = self._resolve(self.USAGE_LEDGER_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def golden_set_path(self) -> Path:
        """Golden Set 文件路径（评估的输入）"""
        return self._resolve(self.EVAL_GOLDEN_SET)

    @property
    def eval_report_path(self) -> Path:
        """评估报告输出目录（自动创建）"""
        p = self._resolve(self.EVAL_REPORT_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---------- Phase 5: 路径解析 ----------

    @property
    def index_watch_dirs_list(self) -> List[Path]:
        """
        增量索引要扫描的目录列表（绝对路径）。

        相对路径同样锚定 `backend_root` —— 与 TRACE_/EVAL_ 一致，
        否则「换个 CWD 启动就扫不到目录」而且不报错。
        """
        return [self._resolve(d.strip()) for d in self.INDEX_WATCH_DIRS.split(",") if d.strip()]

    @property
    def index_manifest_path(self) -> Path:
        """增量索引的指纹清单路径（父目录自动创建）"""
        p = self._resolve(self.INDEX_MANIFEST)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def vector_dump_path(self) -> Path:
        """重建向量库时的中转转储路径（父目录自动创建）"""
        p = self._resolve(self.VECTOR_DUMP_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def memory_store_path(self) -> Path:
        """长期记忆的存储路径（父目录自动创建）"""
        p = self._resolve(self.MEMORY_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def persona_store_path(self) -> Path:
        """助手人设的存储路径（父目录自动创建）"""
        p = self._resolve(self.PERSONA_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def quick_commands_store_path(self) -> Path:
        """快捷指令（用户自建部分）的存储路径（父目录自动创建）"""
        p = self._resolve(self.QUICK_COMMANDS_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def attachments_path(self) -> Path:
        """对话附件的临时目录（自动创建）"""
        p = self._resolve(self.ATTACHMENTS_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def artifact_revisions_path(self) -> Path:
        """产物版本库（自动创建）"""
        p = self._resolve(self.ARTIFACT_REVISIONS_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def context_summaries_path(self) -> Path:
        """长对话摘要记录目录（自动创建）"""
        p = self._resolve(self.CONTEXT_SUMMARY_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tool_results_path(self) -> Path:
        """超长工具输出的落盘目录（自动创建；对 Agent 只读放行）"""
        p = self._resolve(self.TOOL_RESULTS_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def skills_dir(self) -> Path:
        """用户技能目录（自动创建；扫描时不存在不算错误，只是「还没有技能」）"""
        p = self._resolve(self.SKILLS_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def skills_bundled_dir(self) -> Path:
        """内置技能目录（随仓库走，只读）。不存在时返回路径本身，由扫描侧跳过。"""
        return self._resolve(self.SKILLS_BUNDLED_DIR)

    @property
    def skills_state_path(self) -> Path:
        """技能启停状态的落盘路径（父目录自动创建）"""
        p = self._resolve(self.SKILLS_STATE_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def mcp_store_path(self) -> Path:
        """MCP Server 清单的落盘路径（父目录自动创建）"""
        p = self._resolve(self.MCP_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def market_index_path(self) -> Path:
        return self._resolve(self.MARKET_DIR) / "index.json"

    @property
    def market_packs_dir(self) -> Path:
        return self._resolve(self.MARKET_DIR) / "packs"

    @property
    def market_installed_path(self) -> Path:
        """已安装注册表落盘路径（父目录自动创建）"""
        p = self._resolve(self.MARKET_INSTALLED_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def market_staging_dir(self) -> Path:
        """创建向导的产物目录（自动创建）"""
        p = self._resolve(self.MARKET_STAGING_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def agents_store_path(self) -> Path:
        """智能体注册表的存储路径（父目录自动创建）"""
        p = self._resolve(self.AGENTS_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def runtime_config_path(self) -> Path:
        """运行时覆写的落盘路径（父目录自动创建）"""
        p = self._resolve(self.RUNTIME_CONFIG_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def profiles_path(self) -> Path:
        """LLM profile 目录的落盘路径（父目录自动创建）"""
        p = self._resolve(self.LLM_PROFILES_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ---------- Phase 6: 路径解析 ----------

    @property
    def approval_record_path(self) -> Path:
        """审批留痕的 JSONL 路径（父目录自动创建）"""
        p = self._resolve(self.APPROVAL_RECORD_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def question_record_path(self) -> Path:
        """模型提问留痕的 JSONL 路径（父目录自动创建）"""
        p = self._resolve(self.QUESTION_RECORD_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ---------- Phase 5: 路径解析 ----------

    @property
    def permission_mode_path(self) -> Path:
        """默认权限模式的落盘路径（父目录自动创建，文件可不存在）"""
        p = self._resolve(self.PERMISSION_MODE_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def permission_rules_path(self) -> Path:
        """allow / ask / deny 规则的落盘路径"""
        p = self._resolve(self.PERMISSION_RULES_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def computer_control_path(self) -> Path:
        """电脑操控总开关的落盘路径（父目录自动创建，文件可不存在）"""
        p = self._resolve(self.COMPUTER_CONTROL_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def hooks_store_path(self) -> Path:
        """Hooks 定义的落盘路径（父目录自动创建，文件可不存在）"""
        p = self._resolve(self.HOOKS_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def hooks_run_log_path(self) -> Path:
        """钩子执行留痕（append-only，同审批留痕一个套路）"""
        p = self._resolve(self.HOOKS_RUN_LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ---------- Phase 7: 路径解析 ----------

    @property
    def conversation_dir(self) -> Path:
        """会话历史的根目录（会话索引 + 每会话一个轮次文件；自动创建）"""
        p = self._resolve(self.CONVERSATION_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def subtask_dir(self) -> Path:
        """后台子任务的根目录（任务索引 + 每任务一个 append-only 事件文件；自动创建）"""
        p = self._resolve(self.SUBTASK_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def schedule_dir(self) -> Path:
        """定时任务的根目录（定义索引 + 每任务一个 append-only 运行文件；自动创建）"""
        p = self._resolve(self.SCHEDULED_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def workflow_dir(self) -> Path:
        """工作流的根目录（定义索引 + 每流一个 append-only 运行文件；自动创建）"""
        p = self._resolve(self.WORKFLOW_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def channels_wechat_dir(self) -> Path:
        """微信渠道目录。故意不自动创建 —— 没绑定过就该是个不存在的目录。"""
        return self._resolve(self.CHANNELS_WECHAT_DIR)

    @property
    def wechat_account_path(self) -> Path:
        """微信 iLink 账号凭据文件（扫码登录后由通道侧写入）"""
        return self.channels_wechat_dir / "account.json"

    @property
    def workspace_db_path(self) -> Path:
        """工作空间模块的 SQLite 库路径（父目录自动创建）"""
        p = self._resolve(self.WORKSPACE_DB_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def knowledge_scope_path(self) -> Path:
        """项目 ↔ 知识来源映射表的路径（父目录自动创建，文件可以不存在）"""
        p = self._resolve(self.KNOWLEDGE_SCOPE_STORE)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def rerank_api_url(self) -> str:
        """
        Rerank 接口地址。

        DashScope 的重排能力**不在** OpenAI 兼容路径下，而是原生路径：
            {scheme}://{host}/api/v1/services/rerank/text-rerank/text-rerank
        所以这里从 LLM_BASE_URL 只取 scheme+host 再拼原生路径
        （LLM_BASE_URL 形如 https://xxx/compatible-mode/v1，多出来的路径段要去掉）。
        需要接别家重排服务时，直接配 RERANK_API_URL 覆盖。
        """
        if self.RERANK_API_URL:
            return self.RERANK_API_URL
        from urllib.parse import urlparse

        parsed = urlparse(self.LLM_BASE_URL)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}/api/v1/services/rerank/text-rerank/text-rerank"

    @property
    def rerank_api_key(self) -> str:
        """Rerank 密钥；未单独配置时复用 LLM_API_KEY（同一网关）"""
        return self.RERANK_API_KEY or self.LLM_API_KEY

    @property
    def declared_models(self) -> List[str]:
        """
        `.env` 的 `LLM_AVAILABLE_MODELS` 里**原样声明**的候选名。

        ★ 与 `available_models` 的差别只有一处，但很要紧

        `available_models` 会保证「当前 `LLM_MODEL` 一定在清单里」（否则用户看不到
        自己默认用的是哪个），于是它**永远包含**当前模型 —— 想判断
        「这个名字是用户手填的，还是 `.env` 里本来就声明过的」时，
        看它只会永远得到「是」。这个 property 保留原始事实，供「语言模型」面板
        判断「当前模型不在候选清单里」并据此提醒。
        """
        return _parse_model_list(self.LLM_AVAILABLE_MODELS)

    @property
    def available_models(self) -> list[dict]:
        """
        解析 `LLM_AVAILABLE_MODELS`，返回模型选择器的候选清单。

        规则：
        - **未配置就返回空列表** —— 前端据此不显示选择器，而不是显示一个
          只有一项、点了也没变化的控件。
        - 去重并保持书写顺序；`LLM_MODEL` 一定在清单里（否则用户看不到
          「默认用的是哪个」），且带 `default: true`。
        """
        names = _parse_model_list(self.LLM_AVAILABLE_MODELS)
        if not names:
            return []
        if self.LLM_MODEL and self.LLM_MODEL not in names:
            names.insert(0, self.LLM_MODEL)
        return [
            {"id": name, "label": name, "default": name == self.LLM_MODEL}
            for name in names
        ]

    @property
    def mcp_servers_list(self) -> list[dict]:
        """
        解析 MCP_SERVERS 配置。

        配置写在 .env 里的 JSON 字符串，解析失败时返回空列表并打印告警，
        而不是让应用起不来 —— 与项目其他依赖的容错策略保持一致。
        """
        raw = (self.MCP_SERVERS or "").strip()
        if not raw or raw in {"[]", "null", "none"}:
            return []
        try:
            import json

            data = json.loads(raw)
        except json.JSONDecodeError as e:
            import logging

            logging.getLogger(__name__).warning("MCP_SERVERS 不是合法 JSON，已忽略: %s", e)
            return []
        if isinstance(data, dict):
            data = [data]
        return [item for item in data if isinstance(item, dict)]

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


# 全局单例
settings = Settings()


# ══════════════════════════════════════════════════════════════════════════════
# 运行时覆写层
#
# ★ 它解决什么
#
# 检索用哪个 embedding、精排用哪个模型，原本只有 `.env` 一处可改 —— 换一个模型
# 试试效果，要编辑文件 + 重启进程。这个动作太重了，于是做成界面上可切。
# 但界面**不能直接改 `.env`**（那是人的地盘，见 `RUNTIME_CONFIG_STORE` 的注释），
# 所以中间加这一层：界面写 `data/runtime_config.json`，这里读它、覆盖默认值。
#
# ★ 为什么在 **import 时**应用，而不是在 `main.py` 的启动流程里应用
#
# 读 `settings` 的不止 Web 进程：入库脚本、评估脚本都直接 import 它。
# 如果只有 Web 进程应用覆写，就会出现最难查的一种错配：
#
#     界面把 embedding 换成 B 模型 → 检索侧用 B、`index_samples.py` 用 A
#
# 两侧落在不同向量空间，而且**不报错**（不变量 53 的典型症状：效果悄悄变差）。
# 放在这里，所有入口拿到的都是同一份生效配置 —— 这是本层唯一不可让步的约束。
#
# ★ 只认识白名单里的键
#
# 覆写文件是可读可写的普通 JSON，不能让它改掉任意 settings 字段
# （比如一份手改的文件把 `LLM_API_KEY` 覆盖成别人的）。白名单之外的键：忽略 + 告警。
# ══════════════════════════════════════════════════════════════════════════════

_OVERRIDE_FILE_VERSION = 1

#: 允许被运行时覆写的键 → 期望类型。**语义校验**（哪些后端合法、维度范围）
#: 不在这里做 —— 那属于各自领域（检索是 `app/rag/model_settings.py`，
#: 语言模型是 `app/llm/model_settings.py`）。
#: 这里只负责「这个键能不能被覆盖」和「值是不是这个类型」。
RUNTIME_OVERRIDE_SPEC: Dict[str, type] = {
    "LLM_MODEL": str,
    "LLM_BASE_URL": str,
    "LLM_API_KEY": str,
    "LLM_AVAILABLE_MODELS": str,
    "EMBEDDING_BACKEND": str,
    "EMBEDDING_MODEL": str,
    "EMBEDDING_BASE_URL": str,
    "EMBEDDING_API_KEY": str,
    "EMBEDDING_LOCAL_MODEL": str,
    "EMBEDDING_LOCAL_DEVICE": str,
    "EMBEDDING_DIMENSION": int,
    "RERANK_ENABLED": bool,
    "RERANK_BACKEND": str,
    "RERANK_MODEL": str,
    "RERANK_API_URL": str,
    "RERANK_API_KEY": str,
    "RERANK_LOCAL_MODEL": str,
    "RERANK_LOCAL_DEVICE": str,
    "RERANK_TOP_N": int,
    "RERANK_CANDIDATES": int,
    "VOICE_STT_BACKEND": str,
    "VOICE_STT_MODEL": str,
    "VOICE_WAKE_MODEL": str,
    "VOICE_TTS_BACKEND": str,
    "VOICE_TTS_MODEL": str,
    "VOICE_TTS_VOICE": str,
    "VOICE_TTS_VOICE_EDGE": str,
    "VOICE_TTS_RATE": str,
}

# 字段名 → `.env` 里的原始值。界面上要能回答「这个是覆写来的，还是 .env 的」，
# 所以覆写生效时把被顶掉的默认值原样留一份。
_ENV_BASELINE: Dict[str, Any] = {}


def mask_secret(value: Any) -> str:
    """
    密钥的展示掩码：看得出来配过、后四位能对上，但抄不走。

    全项目唯一一份掩码实现（ADR 0021 保证 3）：日志、`state()` 出参、
    连通性测试回显都走它 —— 掩码口径分叉会出现「两处显示对不上」的假故障。
    """
    text = str(value or "")
    if not text:
        return ""
    tail = text[-4:] if len(text) > 8 else ""
    return f"••••••••{tail}" if tail else "••••••••"


def _coerce(value: Any, want: type) -> Any:
    """
    把 JSON 里的值转成期望类型；转不了就抛 `ValueError`。

    ⚠️ `bool` 必须单独处理：`bool("false")` 是 `True` ——
    手写一个 `"RERANK_ENABLED": "false"` 就会被读成「开启」，
    而且完全静默。这类「字面量看起来对、语义反了」的坑不值得省几行。
    """
    if want is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "on"}:
            return True
        if isinstance(value, str) and value.strip().lower() in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"期望布尔值，得到 {value!r}")
    if want is int:
        if isinstance(value, bool):  # bool 是 int 的子类，别让它蒙混过关
            raise ValueError(f"期望整数，得到布尔值 {value!r}")
        return int(value)
    return str(value)


def load_runtime_overrides() -> Dict[str, Any]:
    """
    读出运行时覆写（不应用，只读）。

    任何异常都降级为「空覆写」而不是抛出去 —— 与指纹清单同一条取舍：
    一份损坏的覆写文件，正确的后果是「回到 .env 的值」（能跑，只是不是你要的那一档），
    而不是「应用起不来」。无法识别的键逐条告警，不静默丢弃。
    """
    path = settings.runtime_config_path
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("运行时覆写文件不可读，已忽略（按 .env 配置运行）: %s → %s", path, e)
        return {}

    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict):
        logger.warning("运行时覆写文件结构异常，已忽略: %s", path)
        return {}

    clean: Dict[str, Any] = {}
    for key, raw in values.items():
        want = RUNTIME_OVERRIDE_SPEC.get(key)
        if want is None:
            logger.warning("运行时覆写里含不受支持的键，已忽略: %s", key)
            continue
        try:
            clean[key] = _coerce(raw, want)
        except (TypeError, ValueError) as e:
            logger.warning("运行时覆写的 %s 值不合法，已忽略该键: %s", key, e)
    return clean


def apply_persisted_overrides() -> Dict[str, Any]:
    """
    把覆写文件里的值盖到全局 `settings` 上，返回**实际生效**的那部分。

    在模块末尾调用一次。重复调用是安全的（幂等）。
    """
    values = load_runtime_overrides()
    if not _ENV_BASELINE:
        # 第一次调用时记下 .env 的原始值 —— 之后 `settings` 就被盖过了，
        # 再也读不回来「.env 里原本写的是什么」。
        _ENV_BASELINE.update({k: getattr(settings, k, None) for k in RUNTIME_OVERRIDE_SPEC})

    # ⚠️ 先**整体回到 .env 的值**再盖覆写，而不是只 setattr 覆写里有的键。
    # 差别在于「删掉一条覆写」：只盖有的键的话，被删掉的那条会永远停在
    # 上一次的值上 —— 表现为「点了恢复默认，界面还是旧模型」，而且不报错。
    for key, base in _ENV_BASELINE.items():
        setattr(settings, key, base)
    for key, value in values.items():
        setattr(settings, key, value)

    if values:
        # ⚠️ 键名含 KEY 的值只打掩码：这条日志曾经把 LLM_API_KEY 明文写进日志，
        # 违反 ADR 0021 保证 2（密钥不进日志）。
        logger.info(
            "已应用 %d 项运行时覆写（来自 %s）: %s",
            len(values), settings.runtime_config_path,
            ", ".join(
                f"{k}={mask_secret(values[k]) if 'KEY' in k else values[k]}"
                for k in sorted(values)
            ),
        )
    return values


def env_baseline() -> Dict[str, Any]:
    """`.env` 里的原始值（被覆写顶掉之前）—— 界面上的「恢复默认」用"""
    return dict(_ENV_BASELINE)


def override_status(keys: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """
    运行时覆写的现状（给界面看）：文件路径、已生效的键、`.env` 原始值、可改清单。

    `keys` 只用来**收窄报告的键集**，不改变读取范围 —— 每个前端面板只列自己那一组
    （语言模型面板只关心 `LLM_MODEL`，不该把 `RERANK_TOP_N` 也铺出来），
    而 `active` 始终只含真正写在文件里的键。

    放在 `config.py` 而不是各域自己拼：覆写文件是这一层的东西，
    两个域各写一份「现状」迟早会漂移成两种形状（前端就得记住两套字段）。
    """
    path = settings.runtime_config_path
    updated_at = ""
    if path.is_file():
        try:
            updated_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(
                timespec="seconds"
            )
        except OSError:
            updated_at = ""
    scope = set(keys) if keys is not None else set(RUNTIME_OVERRIDE_SPEC)
    active = {k: v for k, v in load_runtime_overrides().items() if k in scope}
    baseline = env_baseline()

    # ⚠️ Key 类值只给掩码：`active` / `defaults` 会把覆写明文与 `.env` 基线明文
    # 原样送进 GET 响应（ADR 0021 保证 3）。前端只用这里的**键名**
    # （`Object.keys(store.active)` 判断「有没有覆写」），掩码不掉任何功能。
    def _view(k: str, v: Any) -> Any:
        return mask_secret(v) if "KEY" in k else v

    return {
        "path": str(path),
        "active": {k: _view(k, v) for k, v in active.items()},
        "keys": sorted(active),
        "defaults": {k: _view(k, v) for k, v in baseline.items() if k in scope},
        "updatable": sorted(scope),
        "updated_at": updated_at,
    }


def save_runtime_overrides(values: Dict[str, Any]) -> Path:
    """
    原子写入覆写文件（先写临时文件再 `os.replace`）。

    与指纹清单同一套做法，理由也一样：进程写到一半被杀会留下半截 JSON，
    虽然下次能靠「解析失败 → 忽略」自愈，但那意味着**你设的模型悄悄失效了**。

    传空字典 = 删除覆写文件（回到纯 .env 行为），而不是留一个空壳文件 ——
    「文件在但里面什么都没有」会让人以为覆写层坏了。
    """
    path = settings.runtime_config_path
    if not values:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:  # pragma: no cover - 权限异常
            logger.warning("删除运行时覆写文件失败: %s", e)
        return path

    payload = {
        "version": _OVERRIDE_FILE_VERSION,
        "note": (
            "由界面「AI → 语言模型」与「高级 → 检索模型」写入，不要手工编辑；"
            "删掉本文件即回到 backend/.env 的配置。"
        ),
        "values": {k: values[k] for k in sorted(values)},
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def remove_runtime_overrides(keys: Iterable[str]) -> Dict[str, Any]:
    """
    只移除指定的覆写键，其余原样保留（并重新应用到 `settings`），返回生效的覆写。

    ★ 为什么需要「按域恢复」而不是一律清空

    覆写文件是两个前端入口共用的。如果任一面板的「恢复默认」都清空整个文件，
    那么在语言模型面板点一下就会**顺手把用户调好的精排候选数也带回 `.env` 的值** ——
    它不报错，只是安静地改掉了他没在看的设置。

    与 `save_runtime_overrides({})` 的分工：那个是「全部回到 `.env`」（面板上的
    「恢复 .env」），这个是「这一组回到 `.env`」（单个面板里的「恢复默认」）。
    """
    dropped = set(keys)
    kept = {k: v for k, v in load_runtime_overrides().items() if k not in dropped}
    save_runtime_overrides(kept)
    return apply_persisted_overrides()


# ★ 应用时机是契约：
#   必须在任何模块 import 到 `settings` 并**读**它之前完成。
#   放在文件末尾（`settings` 已构造）正好满足 —— 任何 `from app.config import settings`
#   都会先把本模块执行完。
RUNTIME_OVERRIDES: Dict[str, Any] = apply_persisted_overrides()

