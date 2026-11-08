"""桌面单通道调度；停止会取消等待并阻止后续动作，已送达系统的输入不能撤销。"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from app.schemas.tools import ToolResult


class DesktopRuntime:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.generation = 0
        self.revoked = False
        self._active: asyncio.Task | None = None
        self._active_name = ""
        self._events: deque[dict] = deque(maxlen=20)
        self._last_capture_at = ""

    def stop(self) -> None:
        self.generation += 1
        self.revoked = True
        if self._active is not None:
            self._active.cancel()

    def authorize(self) -> None:
        self.revoked = False

    def record(self, tool_name: str, success: bool) -> None:
        from app.computer_control import _now_iso

        self._events.appendleft({"tool": tool_name, "success": success, "at": _now_iso()})

    def captured(self) -> None:
        from app.computer_control import _now_iso

        self._last_capture_at = _now_iso()

    def summary(self) -> dict:
        return {"active": bool(self._active and not self._active.done()),
                "active_tool": self._active_name, "last_capture_at": self._last_capture_at,
                "events": list(self._events)}

    async def execute(self, tool: Any, args: dict, context: Any) -> ToolResult:
        from app.computer_control import assistant_control_turn

        generation = self.generation
        async with self._lock:
            if self.revoked or generation != self.generation or not assistant_control_turn(context.thread_id):
                return ToolResult(success=False, result="", duration_ms=0, error="电脑操控已停止，请由用户重新开启")
            task = asyncio.create_task(tool.invoke(args, context))
            self._active, self._active_name = task, tool.name
            try:
                result = await task
                if generation != self.generation:
                    return ToolResult(success=False, result="", duration_ms=0, error="电脑操控已停止；上一步结果请人工核对")
                if result.images:
                    self.captured()
                self.record(tool.info.display_name or tool.name, result.success)
                return result
            except asyncio.CancelledError:
                if generation != self.generation:
                    self.record(tool.info.display_name or tool.name, False)
                    return ToolResult(success=False, result="", duration_ms=0,
                                      error="用户已停止电脑操控，后续输入已拦截；已送达系统的输入请人工核对")
                raise
            finally:
                self._active, self._active_name = None, ""


desktop_runtime = DesktopRuntime()
