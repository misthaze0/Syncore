"""
工具权限的统一纯判定。

优先级：deny 规则、只读、Hooks 收紧、ask 规则、完全访问、allow 规则、默认风险等级。
电脑操控总开关只开放能力，不覆盖只读或逐次询问。运行时调用方需要用 effective_mode
收紧旧任务上下文；预授权与审计由 approval_flow 处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.permissions import modes
from app.permissions.rules import ALLOW, ASK, DENY, PermissionRule, RuleStore, rule_store

#: 判定结果的行为值（与规则的三个行为同名）
BEHAVIORS = (ALLOW, ASK, DENY)

#: 每个来源对应的 `decided_by`（写进审批留痕，审计要能一眼看出是谁点的头）
SOURCE_USER = "user"
SOURCE_RULE = "rule"
SOURCE_MODE = "mode"
SOURCE_HOOK = "hook"
SOURCE_DANGER = "danger_level"
SOURCE_GRANT = "grant"
#: 助理电脑操控总开关（唯一一个绕过档位与 ask 规则的来源）
SOURCE_ASSISTANT = "assistant"


@dataclass
class PermissionDecision:
    """一次判定的完整答案 —— 包括**为什么**，因为「为什么」是给人看的。"""

    behavior: str
    reason: str
    #: `rule` / `mode` / `hook` / `danger_level` / `safe`
    source: str = ""
    #: 命中的规则 id（供界面把这条决定链到那条规则上）
    rule_id: str = ""
    #: 判定的完整走查（调试与「权限演练」界面用）
    trace: List[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.behavior == ALLOW

    @property
    def denied(self) -> bool:
        return self.behavior == DENY

    @property
    def needs_approval(self) -> bool:
        return self.behavior == ASK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "behavior": self.behavior,
            "behavior_label": {"allow": "放行", "ask": "需要批准", "deny": "禁止"}[self.behavior],
            "reason": self.reason,
            "source": self.source,
            "rule_id": self.rule_id,
            "trace": list(self.trace),
        }


def decide(
    tool_name: str,
    args: Dict[str, Any] | None = None,
    *,
    danger_level: str = "safe",
    mode: Optional[str] = None,
    hook_behavior: str = "",
    hook_reason: str = "",
    assistant_control: bool = False,
    store: Optional[RuleStore] = None,
) -> PermissionDecision:
    """
    判定一次工具调用。

    Args:
        tool_name: 工具名（MCP 工具用带前缀的全名，规则可以按 `server_*` 通配）
        args: 即将传给工具的参数（规则匹配用；**不会**被改写）
        danger_level: 注册中心里该工具的危险等级；未注册工具由执行入口拒绝
        mode: 权限模式。None / 空 = 取全局默认档
        hook_behavior / hook_reason: PreToolUse 钩子的意见（只允许 deny / ask）
        assistant_control: 助理电脑操控是否生效（见 `app/computer_control.py`）。
            只由调用方在「助理会话 + 总开关 + 桌面工具」三者都成立时置真

    Returns:
        `PermissionDecision`
    """
    payload = args or {}
    clean_mode = modes.normalize(mode, default=mode_store_default())
    trace: List[str] = [f"模式={clean_mode}({modes.label(clean_mode)}) 等级={danger_level}"]
    buckets = (store or rule_store).find(tool_name, payload)

    # ---------- 1. deny 规则 ----------
    if buckets[DENY]:
        rule, hit = buckets[DENY][0]
        trace.append(f"deny 规则命中: {hit}")
        return PermissionDecision(
            behavior=DENY,
            reason=rule.note or f"这条操作被你的权限规则挡住了（{rule.describe()}）",
            source=SOURCE_RULE,
            rule_id=rule.rule_id,
            trace=trace,
        )

    # ---------- 2. 只读模式 ----------
    # 电脑操控开关不覆盖只读模式。
    if (
        not modes.allows_mutation(clean_mode)
        and danger_level != "safe"
    ):
        trace.append("只读模式拒绝改动类工具")
        return PermissionDecision(
            behavior=DENY,
            reason="现在是只读模式，只做查询和回答；这一步会改动东西，所以没有执行。",
            source=SOURCE_MODE,
            trace=trace,
        )

    # ---------- 3. 钩子（只能收紧） ----------
    if hook_behavior == DENY:
        trace.append("PreToolUse 钩子拒绝")
        return PermissionDecision(
            behavior=DENY,
            reason=hook_reason or "自定义钩子拒绝了这个操作",
            source=SOURCE_HOOK,
            trace=trace,
        )
    if hook_behavior == ASK:
        trace.append("PreToolUse 钩子要求确认")
        return PermissionDecision(
            behavior=ASK,
            reason=hook_reason or "自定义钩子要求这一步由你确认",
            source=SOURCE_HOOK,
            trace=trace,
        )

    # ---------- 3.5 助理电脑操控 ----------
    # 只有只读操作可以直接放行；显式 ask 和改动操作继续向下判定。
    if assistant_control and danger_level == "safe" and not buckets[ASK]:
        trace.append("助理电脑操控已开启，直接放行")
        return PermissionDecision(
            behavior=ALLOW,
            reason="助理电脑操控已开启，这一步直接执行。",
            source=SOURCE_ASSISTANT,
            trace=trace,
        )

    # ---------- 4. ask 规则 ----------
    if buckets[ASK]:
        rule, hit = buckets[ASK][0]
        trace.append(f"ask 规则命中: {hit}")
        return PermissionDecision(
            behavior=ASK,
            reason=rule.note or f"你设了「{rule.tool}」这一步一定要问你",
            source=SOURCE_RULE,
            rule_id=rule.rule_id,
            trace=trace,
        )

    # ---------- 5. 模式自动放行 ----------
    if danger_level == "danger" and modes.allows_auto_approve(clean_mode):
        trace.append("模式允许自动批准 danger")
        return PermissionDecision(
            behavior=ALLOW,
            reason=modes.auto_approve_reason(clean_mode),
            source=SOURCE_MODE,
            trace=trace,
        )

    # ---------- 6. allow 规则 ----------
    if buckets[ALLOW]:
        rule, hit = buckets[ALLOW][0]
        trace.append(f"allow 规则命中: {hit}")
        return PermissionDecision(
            behavior=ALLOW,
            reason=rule.note or f"按权限规则放行（{rule.describe()}）",
            source=SOURCE_RULE,
            rule_id=rule.rule_id,
            trace=trace,
        )

    # ---------- 7. 出厂默认 ----------
    if danger_level == "danger" or (danger_level == "moderate" and clean_mode == modes.MANUAL):
        trace.append("danger 等级默认要人批")
        return PermissionDecision(
            behavior=ASK,
            reason="这个操作可能造成不可逆的影响，需要你确认后才会执行。",
            source=SOURCE_DANGER,
            trace=trace,
        )

    trace.append("无需批准")
    return PermissionDecision(
        behavior=ALLOW,
        reason="" if danger_level == "safe" else "这一步不会造成不可逆的影响，直接执行。",
        source="safe" if danger_level == "safe" else SOURCE_MODE,
        trace=trace,
    )


def mode_store_default() -> str:
    """全局默认档（单独抽出，方便测试与界面复用）"""
    return modes.mode_store.default_mode


def effective_mode(requested: Optional[str] = None) -> str:
    """
    本轮实际生效的模式：会话级覆盖只能**收紧**，不能放宽。

    ★ 为什么不让本轮消息直接指定 `full`
    「这一轮放开跑」听起来很方便，但它意味着任何一条**用户从别处复制来的文本**
    （邮件正文、网页内容、技能说明里的一句「请用完全访问模式」）都有机会改变权限。
    而界面上一条都看不到 —— 那是最典型的静默提权。
    要放宽，只能由用户在设置里改；要收紧，随手就能收。
    """
    default = mode_store_default()
    override = (requested or "").strip().lower()
    if not modes.is_valid(override):
        return default
    return modes.tighten(override, default)


def explain(
    tool_name: str,
    args: Dict[str, Any] | None = None,
    *,
    danger_level: str = "safe",
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    「权限演练」：给一次还没发生的调用算出判定，并列出所有相关规则。

    与 `decide()` 的差别只有输出 —— 它把**未命中**的规则也列出来，
    因为用户排查「为什么这条规则没生效」时要看的正是没命中的那些。
    """
    payload = args or {}
    clean_mode = modes.normalize(mode, default=mode_store_default())
    decision = decide(tool_name, payload, danger_level=danger_level, mode=clean_mode)
    buckets = rule_store.find(tool_name, payload)
    hits: List[Tuple[PermissionRule, str]] = []
    for behavior in (DENY, ASK, ALLOW):
        hits.extend(buckets[behavior])
    return {
        "tool_name": tool_name,
        "args": payload,
        "mode": clean_mode,
        "mode_label": modes.label(clean_mode),
        "danger_level": danger_level,
        "decision": decision.to_dict(),
        "matched": [
            {
                **rule.to_dict(),
                "hit": hit,
                "won": rule.rule_id == decision.rule_id,
            }
            for rule, hit in hits
        ],
        "rules_total": len(rule_store.load()),
    }


__all__ = [
    "ALLOW",
    "ASK",
    "BEHAVIORS",
    "DENY",
    "SOURCE_ASSISTANT",
    "SOURCE_DANGER",
    "SOURCE_GRANT",
    "SOURCE_HOOK",
    "SOURCE_MODE",
    "SOURCE_RULE",
    "SOURCE_USER",
    "PermissionDecision",
    "decide",
    "effective_mode",
    "explain",
    "mode_store_default",
]
