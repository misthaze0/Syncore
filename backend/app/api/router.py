"""
路由汇总 - 将所有 API Router 挂载到统一前缀 /api
"""

from fastapi import APIRouter

from app.api.agents import router as agents_router
from app.api.approval import router as approval_router
from app.api.assistant import router as assistant_router
from app.api.channels import router as channels_router
from app.api.chat import router as chat_router
from app.api.computer_control import router as computer_control_router
from app.api.conversations import router as conversations_router
from app.api.files import router as files_router
from app.api.graph import router as graph_router
from app.api.health import router as health_router
from app.api.hooks import router as hooks_router
from app.api.indexing import router as indexing_router
from app.api.llm import router as llm_router
from app.api.mcp import router as mcp_router
from app.api.market import router as market_router
from app.api.memory import router as memory_router
from app.api.models import router as models_router
from app.api.observability import router as observability_router
from app.api.office import router as office_router
from app.api.permissions import router as permissions_router
from app.api.persona import router as persona_router
from app.api.questions import router as questions_router
from app.api.quick_commands import router as quick_commands_router
from app.api.rag import router as rag_router
from app.api.skills import router as skills_router
from app.api.schedules import router as schedules_router
from app.api.tasks import router as tasks_router
from app.api.terminal import router as terminal_router
from app.api.tools import router as tools_router
from app.api.usage import router as usage_router
from app.api.voice import router as voice_router
from app.api.workdirs import router as workdirs_router
from app.api.workspace import router as workspace_router
from app.api.workflows import router as workflows_router

api_router = APIRouter(prefix="/api")

# ---------- Phase 1 ----------
api_router.include_router(health_router)
# 工作区（授权目录 + 会话绑定）：必须先于 chat 与 workspace 挂载。
# 本路由的静态段 `/roots`、`/browse` 会被 workspace 的 `/workspaces` 抢走，
# 而 `/binding/{thread_id}` 会被它当成一个工作区 id 去查（然后 404）。
api_router.include_router(workdirs_router)
api_router.include_router(chat_router)
api_router.include_router(files_router)
api_router.include_router(office_router)
# 顶栏模型选择器的候选清单（未配置 LLM_AVAILABLE_MODELS 时为空）
api_router.include_router(models_router)

# ---------- Phase 2：HITL 审批 + 工具治理查询 ----------
api_router.include_router(approval_router)
# 模型主动提问的回答接口（与审批并列，但事件与留痕是分开的两条路）
api_router.include_router(questions_router)
api_router.include_router(tools_router)

# ---------- Phase 4：链路追踪 + 评估报告 ----------
api_router.include_router(observability_router)

# ---------- 用量账本（token 消耗流水，只读） ----------
# 与追踪分开是刻意的：trace 有保留期会滚动删除，账本只增不删。
# 本路由没有任何写接口，见 app/api/usage.py 与 app/usage/ledger.py。
api_router.include_router(usage_router)

# ---------- Phase 5：增量索引 + 长期记忆 ----------
api_router.include_router(indexing_router)
api_router.include_router(memory_router)

# ---------- 助手人设（前端可编辑） ----------
api_router.include_router(persona_router)

# ---------- 助理专属（打开会话时的主动问候） ----------
api_router.include_router(assistant_router)

# ---------- 快捷指令（可复用的问法，输入框 `/` 与命令面板共读） ----------
api_router.include_router(quick_commands_router)

# ---------- 外部渠道状态（「助理挂在哪些 IM 上」） ----------
# 只读凭据落盘结果，不做网络探活 —— 探活会和通道 worker 抢长轮询游标，见 api/channels.py。
api_router.include_router(channels_router)

# ---------- Phase 4b：语音（STT 输入 + TTS 播报） ----------
api_router.include_router(voice_router)

# ---------- Phase 4b：GraphRAG（多跳图检索：状态查看 + 显式构建） ----------
api_router.include_router(graph_router)

# ---------- Phase 7：会话历史（列表 / 恢复 / 删除） ----------
api_router.include_router(conversations_router)

# ---------- 检索模型（向量召回 / 精排的运行时切换、自检、重建向量库） ----------
api_router.include_router(rag_router)

# ---------- 语言模型（默认回答模型的运行时切换与自检） ----------
# 与上面那条共用 data/runtime_config.json，但按域收窄：
# 两个面板的「恢复默认」各摘各的键，互不影响。
api_router.include_router(llm_router)

# ---------- Phase 9: 后台子任务（Supervisor 架构，见 ADR 0019） ----------
# 进度查询的第二条通道（第一条是主对话 SSE 里的 subtask 事件）。
api_router.include_router(tasks_router)

# ---------- 终端（侧边栏「终端」页签） ----------
# 用户亲手输入命令 —— 发起者即审批者，安全层（黑名单/沙箱/环境隔离）照搬 shell 工具。
api_router.include_router(terminal_router)

# ---------- 工作空间模块（群组 + 数字员工） ----------
api_router.include_router(workspace_router)

# ---------- 智能体注册表（主对话专员的定义与表现） ----------
# 只管专员，不管员工 —— 两者的边界见 `app/api/agents.py` 的模块注释。
api_router.include_router(agents_router)

# ---------- 技能（Skills，渐进式披露） ----------
# 列清单 / 看正文 / 开关 / 重扫，写盘能力不在这里（一个技能就是磁盘上的一个文件夹）。
api_router.include_router(skills_router)

# ---------- MCP 控制面 ----------
# 配置写在 data/mcp/servers.json，工具活在进程内；每个写动作都以一次对账收尾。
api_router.include_router(mcp_router)

# ---------- 插件市场（发现 / 安装 / 卸载 / 启停） ----------
# 目录源 = 内置 index.json + 可选远程 URL；安装只认一次性凭证。
# 技能落盘后由 SkillStore 指纹自认，MCP 写 servers.json 后对账 —— 不发明第二条加载链路。
api_router.include_router(market_router)

# ---------- Phase 5：权限模式 + allow/ask/deny 规则 ----------
# 只有用户能放宽档位；演练不跑钩子、不执行任何东西（判据见模块注释）。
api_router.include_router(permissions_router)

# ---------- 电脑操控（仅助理）：总开关 + 连接状态 ----------
api_router.include_router(computer_control_router)

# ---------- Phase 5：Hooks ----------
# 出厂关闭，关掉时一个进程都不起。试跑是真的执行，界面上不许写成"模拟"。
api_router.include_router(hooks_router)

# ---------- 定时任务（cron 式周期触发，执行复用 Phase 9 的子任务执行器） ----------
# 挂在最后：它只有自己的 /schedules 前缀，不与任何既有路由抢路径；
# 路由**内部**的静态段（/status、/runs）先于 /{schedule_id} 注册，见 api/schedules.py 模块注释。
api_router.include_router(schedules_router)

# ---------- 可视化工作流（画布定义 + 手动/定时/助理三种触发） ----------
# 工作流不是 LangGraph 图：它执行的是用户画出来的线性/分支流程，见 workflows/engine.py。
api_router.include_router(workflows_router)
