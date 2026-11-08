"""
工作空间模块的请求模型与字段收敛。

★ 与 `persona/models.py` 的关系

「数字员工」的人设字段（name / tagline / tone / tone_custom / instructions）复用
助手人设 `PersonaProfile` 的语义与收敛规则（预设语气枚举、自定义语气、长度 clamp），
但**多出**两个 PersonaProfile 没有的维度：

- `scope`（参与范围）：员工「只做这些，越界要说明」的边界，注入 prompt。
- `base_agent`（能力模板）+ `tools_allowed`（工具白名单）：决定这个员工
  跑哪个领域子 Agent、能碰哪些工具。

所以这里不继承 PersonaProfile（它是个 dataclass 且语义是「单例助手」），
而是独立一套 Pydantic 请求模型，字段命名与它对齐。
"""

from __future__ import annotations

from typing import Any, Optional
from typing import Literal

from pydantic import BaseModel, Field

from app.persona.models import TONE_CUSTOM, TONE_PRESETS


def _clamp(s: str, max_len: int) -> str:
    return (s or "").strip()[:max_len]


class WorkspaceCreate(BaseModel):
    """创建工作空间（会**自动**创建一个同名群组，见需求 2）。"""

    name: str = Field(..., min_length=1, max_length=60, description="工作空间名")
    description: str = Field("", max_length=500, description="项目描述")


class WorkspaceUpdate(BaseModel):
    """局部更新工作空间（未提及的字段保持原样）。"""

    name: Optional[str] = Field(None, min_length=1, max_length=60)
    description: Optional[str] = Field(None, max_length=500)


class EmployeeCreate(BaseModel):
    """创建数字员工。"""

    workspace_id: str = Field(..., description="归属工作空间 id")
    name: str = Field(..., min_length=1, max_length=24, description="员工名字（角色定位）")
    tagline: str = Field("", max_length=60, description="一句话定位")
    tone: str = Field("professional", description="语气预设 key 或 custom")
    tone_custom: str = Field("", max_length=200, description="自定义语气（tone=custom 时生效）")
    instructions: str = Field("", max_length=1000, description="能力设定/补充人设")
    scope: str = Field("", max_length=1000, description="参与范围（只做这些，越界说明）")
    base_agent: str = Field("file_agent", description="能力模板：pc/file/software/browser_agent")
    tools_allowed: list[str] = Field(default_factory=list, description="插件市场已安装扩展的选择键；空=不分配可选扩展")
    enabled: bool = Field(True, description="是否启用")
    model: str = Field("", max_length=200, description="员工使用的模型；空为默认")
    model_profile_id: str = Field("", max_length=100, description="模型所属的已保存连接")


class EmployeeUpdate(BaseModel):
    """局部更新数字员工（未提及的字段保持原样）。"""

    name: Optional[str] = Field(None, min_length=1, max_length=24)
    tagline: Optional[str] = Field(None, max_length=60)
    tone: Optional[str] = None
    tone_custom: Optional[str] = Field(None, max_length=200)
    instructions: Optional[str] = Field(None, max_length=1000)
    scope: Optional[str] = Field(None, max_length=1000)
    base_agent: Optional[str] = None
    tools_allowed: Optional[list[str]] = None
    enabled: Optional[bool] = None
    model: Optional[str] = Field(None, max_length=200)
    model_profile_id: Optional[str] = Field(None, max_length=100)


class MemberAdd(BaseModel):
    """把一名员工拉进群组。"""

    employee_id: str = Field(..., description="员工 id")
    role: str = Field("member", description="member | orchestrator")


class OrchestrateRequest(BaseModel):
    """触发一次群组编排协作。"""

    instruction: str = Field(..., min_length=1, max_length=4000, description="交给团队办的一件事")
    #: ★ 运行时选项必须一路传到每个成员任务。
    #: 它们过去没被传（`orchestrator.start` 的签名里就没有），于是成员任务一律吃
    #: 全局默认：用户关了联网，群里派出去的活照样联网 —— 而且这三个值会落进
    #: `TaskRecord`，重启后 `sweep_orphans` 按它们重新派发，错一次错到底。
    web: bool = True
    graph: bool = True
    model: Optional[str] = Field(None, description="本轮指定模型；留空跟随全局")


class EmployeeChatRequest(BaseModel):
    """员工私聊（SSE）。"""

    message: str = Field(..., min_length=1, max_length=20000, description="发给该员工的话")
    thread_id: str = Field("", description="员工私聊 thread（emp_<id>）；留空则按员工 id 生成")
    web: bool = True
    graph: bool = True
    model: Optional[str] = None
    thinking: Optional[bool] = None


class GroupMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)
    recipient_employee_id: str = Field("", max_length=100)
    reply_to: str = Field("", max_length=100)
    intent: Literal["auto", "message", "task"] = "auto"
    client_id: str = Field("", max_length=100)


class CardReviewRequest(BaseModel):
    accepted: bool
    reason: str = Field("", max_length=1000)


class ScopedMemoryRequest(BaseModel):
    scope: Literal["project", "employee"] = "employee"
    key: str = Field(..., min_length=1, max_length=100)
    value: str = Field(..., min_length=1, max_length=2000)
    evidence: str = Field("", max_length=2000)


class KnowledgeScopeUpdate(BaseModel):
    """整体替换某个项目的知识边界（前缀列表；空列表 = 该项目不再声明范围）。

    ★ 这里刻意不做长度/条数校验

    规则只有一处真源：`knowledge_scope.validate_boundaries`（条数上限、单条长度、
    反斜杠归一成斜杠）。模型再校验一遍就是第二套判据 —— 界面上过了、落盘时被拒，
    或者反过来。校验失败由接口层译成 400。
    """

    prefixes: list[str] = Field(
        default_factory=list,
        description='形如 "项目A/" 或 "项目A/需求文档"；与索引侧 source 命名同源',
    )


#: 合法能力模板（与 `agents/registry.py::_seeds()` 的四个管家对齐）。
BASE_AGENTS = ("pc_agent", "file_agent", "software_agent", "browser_agent")

#: 旧版能力模板 → 新模板。员工行是落盘数据，改名后老库里的值靠这张表归一，
#: 而不是在读取处各自猜一遍。
LEGACY_BASE_AGENTS = {
    "rag_agent": "file_agent",
    "code_agent": "pc_agent",
    "email_agent": "file_agent",
    "calendar_agent": "file_agent",
}

#: 新建员工与归一失败时的默认模板。
DEFAULT_BASE_AGENT = "file_agent"


def normalize_base_agent(value: Any) -> str:
    """把任意落盘/入参的 base_agent 收敛成当前合法模板名。"""
    raw = str(value or "").strip()
    if raw in BASE_AGENTS:
        return raw
    return LEGACY_BASE_AGENTS.get(raw, DEFAULT_BASE_AGENT)


def normalize_tone(tone: str) -> str:
    """收敛语气取值：非法一律退回预设（与 PersonaProfile.__post_init__ 同一规则）。"""
    if tone in TONE_PRESETS or tone == TONE_CUSTOM:
        return tone
    return "professional"


def clamp_employee_payload(data: dict[str, Any]) -> dict[str, Any]:
    """把「创建/更新员工」的入参收敛成可落盘的安全字典。

    ★ 与 PersonaProfile 的 clamp 同一口径：长度上限、语气枚举、布尔归一，
    都在**数据入口一处**生效，调用方与存储层都不用各自再校验一遍。
    """
    out: dict[str, Any] = {}
    for field, limit in (("model", 200), ("model_profile_id", 100)):
        if field in data and data[field] is not None:
            out[field] = _clamp(str(data[field]), limit)
    if "name" in data and data["name"] is not None:
        out["name"] = _clamp(str(data["name"]), 24) or "未命名员工"
    if "tagline" in data and data["tagline"] is not None:
        out["tagline"] = _clamp(str(data["tagline"]), 60)
    if "tone" in data and data["tone"] is not None:
        out["tone"] = normalize_tone(str(data["tone"]))
    if "tone_custom" in data and data["tone_custom"] is not None:
        out["tone_custom"] = _clamp(str(data["tone_custom"]), 200)
    if "instructions" in data and data["instructions"] is not None:
        out["instructions"] = _clamp(str(data["instructions"]), 1000)
    if "scope" in data and data["scope"] is not None:
        out["scope"] = _clamp(str(data["scope"]), 1000)
    if "base_agent" in data and data["base_agent"] is not None:
        out["base_agent"] = normalize_base_agent(data["base_agent"])
    if "tools_allowed" in data and data["tools_allowed"] is not None:
        tools = data["tools_allowed"]
        if isinstance(tools, list):
            out["tools_allowed"] = [str(t) for t in tools if isinstance(t, str) and t]
        else:
            out["tools_allowed"] = []
    if "enabled" in data and data["enabled"] is not None:
        out["enabled"] = bool(data["enabled"])
    return out


__all__ = [
    "BASE_AGENTS",
    "EmployeeChatRequest",
    "EmployeeCreate",
    "EmployeeUpdate",
    "KnowledgeScopeUpdate",
    "MemberAdd",
    "OrchestrateRequest",
    "WorkspaceCreate",
    "WorkspaceUpdate",
    "clamp_employee_payload",
    "normalize_tone",
]
