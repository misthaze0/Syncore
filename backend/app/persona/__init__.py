"""
助手人设（Persona）—— 用户可在前端调整的「这个助手是谁」

★ 为什么需要这个模块

在此之前，助手的名字、性格、说话方式全部硬编码在 `core/prompts.py` 与前端
欢迎语里 —— 想把「Syncore」改成别的名字、换个说话风格，只能改代码。
而「助手叫什么、什么性格」恰恰是 WorkBuddy / Kimi 这类产品里**用户最常改**的设置。

★ 设计原则（与长期记忆同一套取舍）

1. **人设是增益，不是依赖**。文件损坏 / 读取失败时按默认人设继续跑，
   绝不让问答链路 500 —— 与 `memory/store.py` 的哲学一致。
2. **文件是唯一事实来源**。不进 Redis（可降级依赖会丢）、不进数据库
   （单文件 JSON 可 git diff、可备份）。
3. **注入点只有一处**：`agents/nodes/runner.py` 构造 system_prompt 的地方。
   与不变量 27（记忆注入）同一条收敛纪律 —— 将来新增任何子 Agent 都自动带人设。

★ 边界：主 Agent 的意图识别**不吃人设**

`main_agent.py` 的意图分类器是一个路由引擎，「轻松幽默」的人设不该影响
「这句话归 rag 还是 code」的判断 —— 那会把路由精度变成人设的牺牲品。
所以人设只注入子 Agent（真正说话的地方），这点是有意为之，不是遗漏。
"""

from __future__ import annotations

from app.persona.manager import PersonaManager, get_persona_manager
from app.persona.models import (
    DEFAULT_PERSONA,
    DEFAULT_PRESET,
    PERSONA_PRESETS,
    PROACTIVE_LEVELS,
    TONE_CUSTOM,
    PersonaProfile,
    TONE_PRESETS,
)
from app.persona.store import PersonaStore, get_store

__all__ = [
    "DEFAULT_PERSONA",
    "DEFAULT_PRESET",
    "PERSONA_PRESETS",
    "PROACTIVE_LEVELS",
    "PersonaManager",
    "PersonaProfile",
    "PersonaStore",
    "TONE_CUSTOM",
    "TONE_PRESETS",
    "get_persona_manager",
    "get_store",
]
