"""
电脑操控 —— 助理对本机桌面的直接操作能力（全局总开关 + 作用域）

★ 为什么不是权限四档里的又一档

四档回答的是「她动你的文件和系统时要不要问」，而电脑操控回答的是另一件事：
「她能不能接管你正在用的这张桌面」。前者可以逐次审批，后者一旦开始就是
连续的鼠标键盘输入 —— 每一次点击都弹窗，等于没法用。所以它需要一个
**明确的一次性授权**：用户在设置页打开总开关，之后助理在这个能力内直接执行。

★ 为什么只对助理开放

普通对话、数字员工、后台子任务都不该有「替你点鼠标」的能力：它们的输入
可能来自网页、文档、定时任务，而屏幕内容本身又可以被注入。助理是用户
当面说话的那一条线，授权对象必须是它，不能是「所有 Agent」。

★ 为什么还要有 `server` 标记

桌面能力来自 MCP（当前是 Windows-MCP）。按「名字以 win_ 开头」认工具会在
用户改前缀后静默失效；注册时把 MCP server 名带进 `ToolInfo.server`，
这里只认 server，不认前缀。

★ 与规则、钩子的关系

总开关打开时，助理的桌面调用绕过「默认 danger 要审批」和 ask 规则，但仍然
受 deny 规则与 PreToolUse 钩子的拒绝约束。deny 是用户画的红线，不属于
「逐次打扰」；把它也绕过会让总开关变成一个能吃掉所有安全配置的超级开关。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import settings
from app.conversations.store import is_assistant_thread_id

logger = logging.getLogger(__name__)

#: 提供桌面操控工具的 MCP server 名（见 `data/mcp/servers.json`）。
#: 改这个常量之前先确认新 server 注册的 `ToolInfo.server` 与它一致。
COMPUTER_CONTROL_SERVER = "windows"

#: 唯一被允许使用电脑操控的 Agent。助理窗口走的是 supervisor 的 direct 循环。
ASSISTANT_AGENT = "supervisor"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ComputerControlStore:
    """
    总开关的落盘（单文件 + 原子写 + 内存快照）。

    ★ 为什么单独一个文件
    它与 `permissions/mode.json` 的生命周期不同：模式是「日常松紧」，总开关是
    「这台桌面要不要交给助理」。混在一个文件里会让两件事的写入互相覆盖，
    而那次覆盖不会报错，用户只会觉得「我明明开了」。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None

    def _read(self) -> Dict[str, Any]:
        path = settings.computer_control_path
        if not path.exists():
            return {"version": 1, "enabled": False, "changed_at": ""}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("电脑操控配置读取失败，退回关闭: %s", e)
            return {"version": 1, "enabled": False, "changed_at": ""}
        if not isinstance(raw, dict):
            return {"version": 1, "enabled": False, "changed_at": ""}
        return {
            "version": 1,
            # 只认真正的 `true`：配置文件被手改成 "yes"/1 之类一律按关闭处理。
            # 这是安全开关，宁可让用户再点一次，也不能把一个含糊的值读成授权。
            "enabled": raw.get("enabled") is True,
            "changed_at": str(raw.get("changed_at") or ""),
            "capture_after_action": raw.get("capture_after_action", True) is True,
            "capture_display": "primary" if raw.get("capture_display") == "primary" else "all",
            "vision_model": str(raw.get("vision_model") or ""),
            "vision_profile_id": str(raw.get("vision_profile_id") or ""),
        }

    def _write(self, payload: Dict[str, Any]) -> None:
        path = settings.computer_control_path
        tmp = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load(self, *, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if self._cache is None or force:
                self._cache = self._read()
            return dict(self._cache)

    @property
    def enabled(self) -> bool:
        return bool(self.load()["enabled"])

    def set_enabled(self, enabled: bool) -> bool:
        """用户动作入口（HTTP 设置接口）。模型与后台任务不得调用。"""
        clean = enabled is True
        with self._lock:
            payload = {
                **self.load(),
                "version": 1,
                "enabled": clean,
                "changed_at": _now_iso(),
            }
            self._write(payload)
            self._cache = payload
        logger.info("电脑操控总开关已%s", "开启" if clean else "关闭")
        return clean

    def update_preferences(self, patch: Dict[str, Any]) -> None:
        with self._lock:
            payload = {**self.load(), **patch, "changed_at": _now_iso()}
            self._write(payload)
            self._cache = payload


#: 进程级单例
computer_control_store = ComputerControlStore()


# ---------- 工具识别 ----------

def desktop_tool_infos() -> List[Any]:
    """当前注册中心里属于电脑操控的 `ToolInfo` 列表（按 server 标记）"""
    from app.agents.tools.registry import tool_registry

    return [
        info
        for info in tool_registry.list_all()
        if getattr(info, "server", "") == COMPUTER_CONTROL_SERVER
    ]


def desktop_tool_names() -> List[str]:
    return [info.name for info in desktop_tool_infos()]


def is_desktop_tool(tool_name: str) -> bool:
    from app.agents.tools.registry import tool_registry

    info = tool_registry.get_info(tool_name)
    return bool(info and getattr(info, "server", "") == COMPUTER_CONTROL_SERVER)


def describe_desktop_tools() -> str:
    """给调度 prompt 用的一行一个工具说明（不依赖 allowed_agents 配置）"""
    infos = desktop_tool_infos()
    if not infos:
        return "（桌面连接器尚未注册任何工具）"
    return "\n".join(
        f"- {info.display_name or info.name}（{info.name}）: {info.description}"
        for info in infos
    )


# ---------- 作用域判定 ----------

def assistant_control_turn(thread_id: str) -> bool:
    """这一轮是不是「助理窗口 + 总开关已开」"""
    from app.computer_runtime import desktop_runtime

    return not desktop_runtime.revoked and computer_control_store.enabled and is_assistant_thread_id(thread_id)


def assistant_control_active(thread_id: str, tool_name: str) -> bool:
    """这一次调用能不能按「助理电脑操控」放行：必须是桌面工具且作用域成立"""
    return assistant_control_turn(thread_id) and is_desktop_tool(tool_name)


def status() -> Dict[str, Any]:
    """设置页要的全部状态：总开关、连接器、可用工具数"""
    infos = desktop_tool_infos()
    server: Dict[str, Any] = {
        "name": COMPUTER_CONTROL_SERVER,
        "connected": False,
        "tools": 0,
        "error": "",
    }
    conn = None
    try:
        from app.agents.tools.mcp_adapter import mcp_manager

        conn = mcp_manager.get_connection(COMPUTER_CONTROL_SERVER)
        if conn is None:
            server["error"] = "连接器尚未建立"
        else:
            server.update(
                connected=bool(conn.connected),
                tools=len(conn.registered_names),
                error=conn.error or "",
            )
    except Exception as e:  # noqa: BLE001
        server["error"] = str(e)

    payload = computer_control_store.load()
    from app.computer_runtime import desktop_runtime

    remote_names = {t["name"].casefold() for t in conn.tools} if conn is not None else set()
    snapshot = next((t for t in conn.tools if t["name"].casefold() == "snapshot"), None) if conn is not None else None
    has_visual = "screenshot" in remote_names or bool(snapshot and "use_vision" in (snapshot.get("input_schema") or {}).get("properties", {}))
    capabilities = [
        {"id": "vision", "label": "截图与视觉", "available": has_visual},
        {"id": "mouse", "label": "鼠标操作", "available": "click" in remote_names and "move" in remote_names},
        {"id": "keyboard", "label": "键盘输入", "available": "type" in remote_names and "shortcut" in remote_names},
        {"id": "apps", "label": "应用切换", "available": "app" in remote_names},
    ]
    return {
        "enabled": bool(payload["enabled"]) and not desktop_runtime.revoked,
        "assistant_only": True,
        "changed_at": payload.get("changed_at", ""),
        "tools": len(infos),
        "tool_names": [info.name for info in infos],
        "server": server,
        "ready": bool(server["connected"] and all(c["available"] for c in capabilities)),
        "capabilities": capabilities,
        "preferences": {"capture_after_action": payload.get("capture_after_action", True),
                        "capture_display": payload.get("capture_display", "all"),
                        "vision_model": payload.get("vision_model", ""),
                        "vision_profile_id": payload.get("vision_profile_id", "")},
        "model": payload.get("vision_model") or settings.LLM_MODEL,
        "runtime": desktop_runtime.summary(),
    }


async def capture_desktop(*, conn: Any = None, respect_policy: bool = False) -> Any:
    """只读截屏；由用户点击预览或作为已授权桌面操作的结果采集。"""
    from app.agents.tools.mcp_adapter import mcp_manager
    from app.schemas.tools import ToolResult

    conn = conn or mcp_manager.get_connection(COMPUTER_CONTROL_SERVER)
    if conn is None or not conn.connected:
        raise ConnectionError("桌面连接器未连接，请先重新连接")
    by_name = {t["name"].casefold(): t for t in conn.tools}
    snapshot = by_name.get("screenshot") or by_name.get("snapshot")
    if snapshot is None:
        raise ValueError("连接器没有截图工具，请在插件市场检查 Windows-MCP 配置")
    props = (snapshot.get("input_schema") or {}).get("properties", {})
    args: dict[str, Any] = {}
    for key, value in {"use_vision": True, "use_ui_tree": False, "use_annotation": False}.items():
        if key in props:
            args[key] = value
    if "display" in props and computer_control_store.load().get("capture_display") == "primary":
        args["display"] = [0]
    if respect_policy:
        from app.agents.tools.context import get_tool_context
        from app.hooks import fire_tool_pre
        from app.permissions.policy import decide

        ctx = get_tool_context()
        local_name = f"{conn.config.effective_prefix}{snapshot['name']}"
        session = {"thread_id": ctx.thread_id, "task_id": ctx.task_id,
                   "employee_id": ctx.employee_id, "agent": ctx.agent_name or ASSISTANT_AGENT}
        pre = await fire_tool_pre(tool_name=local_name, args=args, session=session,
                                  permission_mode=ctx.permission_mode)
        decision = decide(local_name, args, danger_level=conn.config.danger_level,
                          mode=ctx.permission_mode, hook_behavior=pre.decision,
                          hook_reason=pre.reason, assistant_control=assistant_control_turn(ctx.thread_id))
        for notice in pre.notice:
            ctx.emit({"type": "hook_notice", "tool_name": local_name,
                      "event": "PreToolUse", "content": notice})
        if not decision.allowed:
            raise PermissionError(decision.reason or "截图被权限规则或钩子拒绝")
    result = await conn.call(snapshot["name"], args)
    if not isinstance(result, ToolResult) or not result.images:
        raise ValueError("连接器没有返回图像，请确认 Snapshot 支持 use_vision，或启用 Screenshot 工具")
    return result


def desktop_model_options() -> dict[str, Any]:
    """独立视觉模型复用已保存的供应商连接，不复制或暴露凭据。"""
    payload = computer_control_store.load()
    if not payload.get("vision_model"):
        return {}
    return {"model": payload["vision_model"], "profile_id": payload.get("vision_profile_id") or None}


__all__ = [
    "ASSISTANT_AGENT",
    "COMPUTER_CONTROL_SERVER",
    "ComputerControlStore",
    "assistant_control_active",
    "assistant_control_turn",
    "computer_control_store",
    "capture_desktop",
    "desktop_model_options",
    "describe_desktop_tools",
    "desktop_tool_infos",
    "desktop_tool_names",
    "is_desktop_tool",
    "status",
]
