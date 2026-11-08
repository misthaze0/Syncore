"""
工具治理相关数据模型 (Phase 2)
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ToolInfo(BaseModel):
    """工具信息"""
    name: str
    #: 面向用户的中文名。前端、审批弹窗、进度、提示词统一读它；
    #: `name` 是给模型和程序用的稳定标识，不再直接作为界面标题。
    display_name: str = ""
    description: str
    danger_level: Literal["safe", "moderate", "danger"]
    allowed_agents: List[str]
    version: str = "1.0.0"
    args_schema: Optional[dict] = None  # 参数 JSON Schema
    health: str = "unknown"  # healthy / degraded / down / unknown

    # ---------- 治理维度（Phase 1 工具框架） ----------
    #: 治理界面分组：文件 / 搜索 / 终端 / 网络 / 文档 / 编排 / 记忆 / 日程 / 邮件 …
    category: str = "general"
    #: 来源：builtin（内置代码）/ mcp（远端 server 接进来的）。
    #: 技能（`app/skills.py`）**不是一种来源**：按 Agent Skills 规范，技能是
    #: 「一份说明 + 材料文件」，不是可执行端点，它用的仍是 builtin 的 `use_skill`。
    #: 权限判定不看这个，只用于展示与审计 —— 各来源在注册中心里同权
    source: str = "builtin"
    #: MCP 工具所属的 server 名；内置工具为空串。电脑操控按它识别桌面工具，
    #: 不按工具名前缀 —— 前缀是用户可改的配置，改名后能力门控不能静默失效。
    server: str = ""
    #: 只读工具：权限模式「自动放行读取」只放这一类
    read_only: bool = False
    #: 不在治理界面展示（内部工具，例如健康探测）
    hidden: bool = False


class ToolResult(BaseModel):
    """工具执行结果"""
    success: bool
    result: str
    duration_ms: int
    error: Optional[str] = None
    #: 输出是否被截断/落盘
    truncated: bool = False
    #: 超长输出的完整内容落盘路径（相对工作目录），模型可用读取工具续读
    output_path: str = ""
    #: 本次产出的文件（相对工作目录），前端据此渲染「本轮产出文件」
    files: List[str] = []
    #: 原生图像块单独传递；不拼进文本、日志、SSE 或超长输出文件。
    images: List[dict] = Field(default_factory=list, repr=False)


class ApprovalRequest(BaseModel):
    """
    HITL 审批请求。

    ★ `run_id` / `employee_id` 是定位键的一部分（护栏 6，见 `ApprovalBroker`）
    工作空间里 `thread_id` 就是群组 id，三个员工共用它。
    少传一维就**匹配不上**（返回 success=False，弹窗提示「该审批已过期」），
    这里刻意不做宽松匹配：便利匹配的代价是「甲的同意放行乙的危险操作」。
    """

    call_id: str
    approved: bool
    thread_id: str = "default"  # 会话 ID，用于定位等待中的审批
    run_id: str = ""
    employee_id: str = ""
    scope: Literal["once", "same_operation"] = "once"
    request_id: str = ""


class ApprovalResponse(BaseModel):
    """HITL 审批响应"""
    success: bool
    call_id: str
    approved: bool
    message: str = ""
    #: 本次审批对应的留痕记录 ID（Phase 6）。为空表示没有匹配到留痕记录
    #: ——通常意味着这个审批早就超时了，调用方据此可以提示“该弹窗已过期”
    record_id: str = ""
    grant_id: str = ""


class ApprovalBatchItem(BaseModel):
    """批量处理里的一条：定位一个挂起的审批并给出决策"""

    call_id: str
    approved: bool
    thread_id: str = "default"
    #: 与 `ApprovalRequest` 同样的四元组定位（护栏 6）
    run_id: str = ""
    employee_id: str = ""


class ApprovalBatchRequest(BaseModel):
    """跨会话批量处理挂起的审批"""

    items: List[ApprovalBatchItem]
    #: 是否把每条的结果都回传（items 很多时可以关掉，只看 summary）
    verbose: bool = True


class QuestionAnswerItem(BaseModel):
    """用户对一个问题的回答"""

    selected: List[str] = []
    other: str = ""


class QuestionAnswerRequest(BaseModel):
    """
    提交一次提问的回答。

    ★ 定位键与审批同构：`thread_id + call_id + run_id + employee_id`。
    多一个 `question_id`：旧卡片上的回答不能唤醒新一次提问
    （`call_id` 是模型给的，重试时很可能重复）。
    """

    call_id: str
    thread_id: str = "default"
    question_id: str = ""
    answers: dict[str, QuestionAnswerItem] = {}
    skipped: bool = False
    run_id: str = ""
    employee_id: str = ""


class QuestionAnswerResponse(BaseModel):
    success: bool
    call_id: str
    question_id: str = ""
    status: str = ""
    message: str = ""
    record_id: str = ""


class ToolListResponse(BaseModel):
    """工具列表响应"""
    total: int
    stats: dict
    #: 分类 → 工具数（治理界面按它分组；已排除 hidden 工具）
    categories: dict = {}
    tools: List[ToolInfo]
    #: 主对话（supervisor direct）可直接调用的工具名
    main_tools: List[str] = []
    #: 工具名 → 主对话不可用原因（danger / desktop / hidden / web_off / mcp_scope）
    main_unavailable: dict = {}
    #: 作用域异常：例如 MCP 连接器的 allowed_agents 里写着已不存在的专员
    scope_warnings: List[dict] = []
