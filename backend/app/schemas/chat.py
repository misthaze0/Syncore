"""
聊天相关数据模型

⚠️ 关于 SSE 事件协议（重要）

SSE 事件是前后端之间**唯一的契约**，它的**单一事实来源**是前端的类型定义：

    frontend/src/types/index.ts   →  SSEEvent / SourceDoc / RetrievalLane ...

后端不维护一份并行的 Pydantic 镜像 —— 曾经有过（`SourceDoc` / `SSEEvent`），
但它在 Phase 2/3 扩容（intent / agent_switch / tool_call / tool_result /
interrupt / retrieval / rerank_score ...）时没有同步，字段停留在 Phase 1，
比没有更危险。事件由 `app/agents/graph.py` 以普通 dict 产出，
新增事件类型时必须**同时**改前端类型与 `graph.py` 的产出点。

本模块只保留请求侧模型。
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """聊天请求"""
    message: str
    thread_id: str = "default"
    top_k: int = 5
    #: 对话附件（`POST /api/files/attach` 返回的 id 列表，按顺序注入本轮上下文）。
    #: 与知识库上传是两条路径 —— 附件只进本轮，不入 Milvus/ES，24h 过期。
    attachments: List[str] = Field(default_factory=list, max_length=5)

    # ---------- 本轮选项（Phase 8：把「怎么答」的决定权交回用户） ----------
    #: 是否允许联网搜索。默认**开** —— 「能自动的不要问」：用户只有在明确
    #: 不想让助手查网上资料时才关。关闭时 `browser_agent` 拿不到 `web_search` 工具。
    web: bool = True
    #: 是否走知识图谱召回（三路融合里的图路）。默认开，**UI 不暴露** ——
    #: 它属于底层检索策略，普通用户没有可判断的依据；留给 API 调用者与开发者。
    graph: bool = True
    #: 本轮使用的模型名。`None` 表示跟随全局配置（`.env` 的 `LLM_MODEL`）。
    #: 只有配置了 `LLM_AVAILABLE_MODELS` 才有多选，否则前端根本不显示选择器。
    model: Optional[str] = None
    #: 本轮是否开深度思考。`None` = 跟随全局配置（`.env` 的 `LLM_ENABLE_THINKING`）。
    #: 思考内容经 `reasoning` 事件实时流式返回（前端折叠展示）。
    thinking: Optional[bool] = None
    #: 本轮权限档（Phase 5）。`None` = 跟随设置里的默认档。
    #: ⚠️ 只允许**收紧**：传进来的值比默认档松时，后端按默认档跑
    #: （见 `permissions/policy.effective_mode`）。这不是防呆，是防注入 ——
    #: 会被写进这一位的文本，同样可能来自邮件正文或网页内容。
    #: 放宽只有一个入口：设置页改默认档，那里有界面、有留痕。
    permission_mode: Optional[str] = None
