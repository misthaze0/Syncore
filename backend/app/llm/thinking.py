"""
深度思考开关的 per-model 适配。

一个布尔开关（界面上的「深度思考」）在不同模型族里是**不同的协议参数**，
甚至根本没有参数：

- Qwen 系网关：`enable_thinking`（扩展参数；本项目默认网关就是这种，
  第三方程式的转发站大多也认它）；
- OpenAI 官方：o 系列**始终思考**、强度用 `reasoning_effort` 调（low/medium/high）；
  gpt-5 系可用 `reasoning_effort: "none"` 真正关掉；其余模型（gpt-4o 等）没有推理模式；
- 智谱 GLM 官方（bigmodel.cn）：`thinking: {"type": "enabled"/"disabled"}`，
  且 OpenAI 兼容路径**默认开启** —— 「关思考」必须显式发 `disabled`，不发等于没关。
  glm-5.3 / glm-4.7 / glm-4.5v 强制思考，发 `disabled` 直接 400；
- DeepSeek 官方：deepseek-reasoner / r1 始终思考，deepseek-chat 无思考，
  两者都没有开关参数；
- 「仅思考」模型（qwq、*-thinking、o1/o3/o4…）：无法关闭，发「关」要么 400
  要么被无视。

判定入口是「**模型名 × 网关域名**」：官方域名按官方协议走；其余（含本项目
网关、未知中转站）维持 `enable_thinking` 旧行为 —— 适配新供应商不能以改坏
现有网关为代价。域名判不了的就当 Qwen 系对待，这正是现状。

两个出口：

- `capability(model, base_url)` → 给前端：能不能开 / 能不能关 / 为什么
  （禁用开关时的悬停文案）；
- `resolve(model, base_url, thinking)` → 给 `create_llm`：布尔开关翻成
  `extra_body` 片段。能力不允许的方向返回 None（不发字段，调用照旧）——
  界面已经禁用了，这里只是最后一道保险（绕过界面的调用同样安全）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)

STYLE_ENABLE_THINKING = "enable_thinking"
STYLE_REASONING_EFFORT = "reasoning_effort"
STYLE_GLM_THINKING = "thinking"
STYLE_NONE = "none"

#: 名字里带这些片段的模型「始终思考」（无法关闭）。
#: ⚠️ o 系列用**前缀**匹配而不是子串：`moonshot-v1-32k` 里也有 "o1"。
_THINKING_ONLY_SUBSTRINGS = ("qwq", "-thinking", "deepseek-r1", "deepseek-reasoner")
_THINKING_ONLY_PREFIXES = ("o1", "o3", "o4")


def _host(base_url: str) -> str:
    try:
        return (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return ""


def _is_thinking_only(name: str) -> bool:
    return name.startswith(_THINKING_ONLY_PREFIXES) or any(
        hint in name for hint in _THINKING_ONLY_SUBSTRINGS
    )


def _glm_forced(name: str) -> bool:
    """GLM 里「强制思考、发 disabled 会 400」的型号（官方文档清单）"""
    return name.startswith(("glm-4.5v", "glm-4.7", "glm-5.3"))


def _glm_toggleable(name: str) -> bool:
    return name.startswith(("glm-4.5", "glm-4.6", "glm-4.7", "glm-5"))


def _effort() -> str:
    """`reasoning_effort` 档位（o 系列 / gpt-5 的「开」用这个值）"""
    value = str(getattr(settings, "LLM_THINKING_EFFORT", "high") or "high").lower()
    return value if value in ("low", "medium", "high") else "high"


def _cap(style: str, can_on: bool, can_off: bool, reason: str = "") -> Dict[str, Any]:
    return {"style": style, "can_on": can_on, "can_off": can_off, "reason": reason}


def capability(model: str, base_url: str = "") -> Dict[str, Any]:
    """
    这个模型对「深度思考」开关的支持情况（前端禁用态与悬停文案的数据源）。
    """
    name = (model or "").strip().lower()
    if not name:
        return _cap(STYLE_NONE, False, False, "")
    host = _host(base_url)

    if "openai.com" in host:
        if name.startswith(_THINKING_ONLY_PREFIXES):
            return _cap(STYLE_REASONING_EFFORT, True, False, "o 系列始终思考，开关调整的是思考强度")
        if name.startswith("gpt-5"):
            return _cap(STYLE_REASONING_EFFORT, True, True, "思考强度在「无」与配置档位之间切换")
        return _cap(STYLE_NONE, False, True, "该模型没有推理模式")
    if "bigmodel" in host:
        if _glm_forced(name):
            return _cap(STYLE_GLM_THINKING, True, False, "该模型始终思考，无法关闭（发 disabled 会 400）")
        if _glm_toggleable(name):
            return _cap(STYLE_GLM_THINKING, True, True, "GLM 默认开启思考，关闭时会显式发送 disabled")
        return _cap(STYLE_NONE, False, True, "该模型没有思考开关")
    if "deepseek" in host:
        if _is_thinking_only(name):
            return _cap(STYLE_NONE, True, False, "该模型始终思考，无法关闭")
        return _cap(STYLE_NONE, False, True, "该模型没有思考模式")

    # 默认：Qwen 系网关 / 未知网关 —— 维持 enable_thinking 旧行为
    if _is_thinking_only(name):
        return _cap(STYLE_ENABLE_THINKING, True, False, "仅思考模型，无法关闭")
    return _cap(STYLE_ENABLE_THINKING, True, True, "")


def resolve(model: str, base_url: str, thinking: bool) -> Optional[Dict[str, Any]]:
    """
    把布尔开关翻成 `extra_body` 片段；该方向不被支持时返回 None（不发字段）。

    返回 None 的两种语义：a) 模型没有思考开关（照常调用）；
    b) 请求的方向做不到（如要求关闭一个始终思考的模型）—— 记一条日志，
    按「模型自己的默认行为」跑。
    """
    cap = capability(model, base_url)
    if thinking and not cap["can_on"]:
        logger.info("深度思考：模型 %s 不支持开启，忽略开关", model)
        return None
    if not thinking and not cap["can_off"]:
        logger.info("深度思考：模型 %s 无法关闭（始终思考），忽略开关", model)
        return None

    style = cap["style"]
    if style == STYLE_ENABLE_THINKING:
        return {"enable_thinking": thinking}
    if style == STYLE_REASONING_EFFORT:
        return {"reasoning_effort": _effort() if thinking else "none"}
    if style == STYLE_GLM_THINKING:
        return {"thinking": {"type": "enabled" if thinking else "disabled"}}
    return None


__all__ = ["capability", "resolve"]
