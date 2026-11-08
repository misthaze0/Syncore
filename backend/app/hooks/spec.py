"""
Hooks 定义 —— 用户自己写的命令行脚本，在关键节点自动执行

★ 这是什么、为什么要它

审批只能回答「让不让做」，回答不了「做之前先跑一下我的检查」。
钩子把这半步交给用户：`PreToolUse` 跑一个脚本校验参数、
`PostToolUse` 跑格式化、`UserPromptSubmit` 把当前时间/项目状态塞进来。
市面 agent 软件里它是标配，而且是非开发者也能受益的一块 ——
「每次她删文件之前先备份到某个目录」这种需求，除了钩子无处安放。

★ 出厂**关闭**，而且是全局一个开关

钩子 = 由 agent 的行为触发的**本机代码执行**。
它的危险程度高于任何一个 danger 工具：danger 工具至少还在审批链里，
而钩子不弹窗、不审批，直接跑。所以：

1. `HOOKS_ENABLED` 默认 false，界面上要用户明确打开总开关；
2. 钩子**只能收紧**权限判定，不能替用户放行（见 `executor.py` 与 `permissions/policy.py`）；
3. 每次执行都写留痕（谁在什么时候跑了什么、退出码多少）。

★ 命令是**静态配置**，不是模板

命令串里不做任何变量插值，参数通过 **stdin 的 JSON** 传进去。
这不是洁癖：如果 payload 里的字段会被拼进命令行，那么一次
「工具参数里带 `; rm -rf` 的调用」就变成了注入入口 ——
而那个参数是模型写的，不是用户写的。

★ 五个事件

| 事件 | 什么时候 | 典型用法 |
| :--- | :--- | :--- |
| `PreToolUse` | 每次工具调用**之前**（判定阶段） | 参数校验、先备份 |
| `PostToolUse` | 工具执行完 | 格式化、通知 |
| `UserPromptSubmit` | 用户消息进入图之前 | 补充上下文 |
| `Notification` | 审批弹窗出现时 | 桌面提醒、发到自己手机上 |
| `Stop` | 本轮回答结束 | 收尾任务、记录 |

不做 `SessionStart`：本产品的「会话」是 LangGraph 的 thread，
它的生命周期由前端决定，没有稳定的进程级起点，硬造一个只会让钩子看起来不可靠。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

PRE_TOOL = "PreToolUse"
POST_TOOL = "PostToolUse"
PROMPT_SUBMIT = "UserPromptSubmit"
NOTIFICATION = "Notification"
STOP = "Stop"

HOOK_EVENTS: tuple[str, ...] = (PRE_TOOL, POST_TOOL, PROMPT_SUBMIT, NOTIFICATION, STOP)

EVENT_LABELS: Dict[str, str] = {
    PRE_TOOL: "工具调用前",
    POST_TOOL: "工具调用后",
    PROMPT_SUBMIT: "收到我的消息",
    NOTIFICATION: "要我批准时",
    STOP: "这一轮结束时",
}

#: 只有这两个事件参与权限判定（其它事件只影响上下文与提醒）
DECISION_EVENTS: tuple[str, ...] = (PRE_TOOL,)

_MAX_COMMAND_CHARS = 2000


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class HookSpec:
    """一条钩子定义"""

    event: str
    command: str
    #: 工具名通配（`bash` / `mcp_notion_*` / 空=全部）。只对工具类事件有意义。
    tool: str = ""
    timeout: int = 0  # 0 = 用全局默认
    enabled: bool = True
    note: str = ""
    hook_id: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.hook_id:
            self.hook_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = _now_iso()

    @property
    def timeout_seconds(self) -> int:
        return self.timeout if self.timeout > 0 else settings.HOOKS_TIMEOUT_SECONDS

    def matches_tool(self, tool_name: str) -> bool:
        pat = (self.tool or "").strip().casefold()
        if not pat or pat == "*":
            return True
        return fnmatch.fnmatch((tool_name or "").casefold(), pat)

    def describe(self) -> str:
        scope = f"（{self.tool}）" if self.tool else ""
        return f"{EVENT_LABELS.get(self.event, self.event)}{scope} → {self.command[:60]}"

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["event_label"] = EVENT_LABELS.get(self.event, self.event)
        payload["description"] = self.describe()
        payload["effective_timeout"] = self.timeout_seconds
        return payload

    def to_storage(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "HookSpec":
        return cls(
            event=str(raw.get("event") or "").strip(),
            command=str(raw.get("command") or "").strip(),
            tool=str(raw.get("tool") or "").strip(),
            timeout=int(raw.get("timeout") or 0),
            enabled=bool(raw.get("enabled", True)),
            note=str(raw.get("note") or "").strip(),
            hook_id=str(raw.get("hook_id") or ""),
            created_at=str(raw.get("created_at") or ""),
        )


def validate(hook: HookSpec) -> str | None:
    if hook.event not in HOOK_EVENTS:
        return f"事件只能是 {' / '.join(HOOK_EVENTS)} 之一"
    if not hook.command:
        return "命令不能为空"
    if len(hook.command) > _MAX_COMMAND_CHARS:
        return f"命令最长 {_MAX_COMMAND_CHARS} 个字符"
    if not 0 <= hook.timeout <= 300:
        return "超时请在 0–300 秒之间（0 表示用全局默认）"
    return None


class HookStore:
    """钩子表 + 全局总开关（单文件，唯一写入者）"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None

    def _read(self) -> Dict[str, Any]:
        path = settings.hooks_store_path
        if not path.exists():
            return {
                "version": 1,
                "enabled": bool(settings.HOOKS_ENABLED),
                "hooks": [],
            }
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Hooks 配置读取失败，本次按「无钩子」处理: %s", e)
            return {"version": 1, "enabled": False, "hooks": []}
        if not isinstance(raw, dict):
            return {"version": 1, "enabled": False, "hooks": []}
        hooks: List[HookSpec] = []
        for item in raw.get("hooks") or []:
            if not isinstance(item, dict):
                continue
            hook = HookSpec.from_dict(item)
            if validate(hook) is None:
                hooks.append(hook)
        # 总开关出厂默认 false：文件里没写过就绝不允许执行任何脚本
        return {
            "version": 1,
            "enabled": bool(raw.get("enabled", settings.HOOKS_ENABLED)),
            "hooks": hooks,
        }

    def _write(self, payload: Dict[str, Any]) -> None:
        path = settings.hooks_store_path
        body = {
            "version": 1,
            "enabled": bool(payload.get("enabled")),
            "hooks": [h.to_storage() for h in payload.get("hooks") or []],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load(self, *, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if self._cache is None or force:
                self._cache = self._read()
            return {**self._cache, "hooks": list(self._cache["hooks"])}

    @property
    def global_enabled(self) -> bool:
        return bool(self.load()["enabled"])

    def set_global(self, enabled: bool) -> bool:
        with self._lock:
            payload = self.load(force=True)
            payload["enabled"] = bool(enabled)
            self._write(payload)
            self._cache = payload
        logger.info("Hooks 总开关: %s", "开" if enabled else "关")
        return bool(enabled)

    def upsert(self, hook: HookSpec) -> tuple[HookSpec, str]:
        err = validate(hook)
        if err:
            raise ValueError(err)
        with self._lock:
            payload = self.load(force=True)
            hooks: List[HookSpec] = payload["hooks"]
            action = "updated"
            for i, existing in enumerate(hooks):
                if existing.hook_id == hook.hook_id:
                    hook.created_at = existing.created_at
                    hooks[i] = hook
                    break
            else:
                hooks.insert(0, hook)
                action = "created"
            self._write({**payload, "hooks": hooks})
            self._cache = payload
        logger.info("钩子 %s: %s", action, hook.describe())
        return hook, action

    def delete(self, hook_id: str) -> bool:
        with self._lock:
            payload = self.load(force=True)
            hooks = [h for h in payload["hooks"] if h.hook_id != hook_id]
            if len(hooks) == len(payload["hooks"]):
                return False
            self._write({**payload, "hooks": hooks})
            # ★ 快照要换成**删过的那份**，不是 payload。
            # 写盘用的是新列表、内存留的是旧列表，症状是「界面上删掉了，
            # 但这条钩子还在每次工具调用时跑」—— 只有重启才停。
            payload["hooks"] = hooks
            self._cache = payload
        return True

    def set_enabled(self, hook_id: str, enabled: bool) -> Optional[HookSpec]:
        with self._lock:
            payload = self.load(force=True)
            for i, h in enumerate(payload["hooks"]):
                if h.hook_id == hook_id:
                    h.enabled = enabled
                    payload["hooks"][i] = h
                    self._write(payload)
                    self._cache = payload
                    return h
        return None

    def for_event(self, event: str, tool_name: str = "") -> List[HookSpec]:
        """
        这次事件要跑哪些钩子。

        ★ 总开关关了就什么都不返回
        「用户没打开总开关」和「用户打开了但没有 PreToolUse 钩子」在执行侧必须完全等价 ——
        任何一处把总开关当成「只是界面提示」，都会变成未经允许的代码执行。
        """
        if not self.global_enabled:
            return []
        out = []
        for hook in self.load()["hooks"]:
            if not hook.enabled or hook.event != event:
                continue
            if event in (PRE_TOOL, POST_TOOL) and not hook.matches_tool(tool_name):
                continue
            out.append(hook)
        return out

    def stats(self) -> Dict[str, Any]:
        payload = self.load()
        hooks = payload["hooks"]
        return {
            "global_enabled": bool(payload["enabled"]),
            "total": len(hooks),
            "enabled": sum(1 for h in hooks if h.enabled),
            "by_event": {e: sum(1 for h in hooks if h.event == e) for e in HOOK_EVENTS},
            "default_timeout_seconds": settings.HOOKS_TIMEOUT_SECONDS,
        }


#: 进程级单例
hook_store = HookStore()


__all__ = [
    "DECISION_EVENTS",
    "EVENT_LABELS",
    "HOOK_EVENTS",
    "HookSpec",
    "HookStore",
    "NOTIFICATION",
    "POST_TOOL",
    "PRE_TOOL",
    "PROMPT_SUBMIT",
    "STOP",
    "hook_store",
    "validate",
]
