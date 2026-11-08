"""
短期审批授权：绑定会话、请求者、指定工具与可选的全部参数指纹。

新 UI 只创建全部参数完全相同的授权。每条有次数与有效期双上限，可撤销，重启清空；
匹配与消费不跨 await 执行。明确的 ask 规则与 Hooks 要求由流程层保证不可跳过。
"""

from __future__ import annotations

import logging
import time
import uuid
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from app.config import settings

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class ApprovalGrant:
    """一条预授权"""

    grant_id: str
    thread_id: str
    #: 作用域的第三维（护栏 6）。空串 = 非工作空间路径；它**不参与**通配 ——
    #: 「一个员工授权、全组员工放行」这种语义在本项目里不存在。
    employee_id: str = ""
    #: 空元组表示“该会话内的全部 danger 工具”（前端可显式选择这一项）
    tool_names: tuple[str, ...] = ()
    max_uses: int = 1
    used: int = 0
    created_at: str = ""
    expires_at_epoch: float = 0.0
    expires_at: str = ""
    note: str = ""
    revoked: bool = False
    revoked_at: str = ""
    args_fingerprint: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.max_uses - self.used)

    def covers(self, tool_name: str) -> bool:
        return tool_name in self.tool_names

    def active_at(self, now: Optional[float] = None) -> bool:
        moment = time.time() if now is None else now
        return not self.revoked and self.remaining > 0 and moment < self.expires_at_epoch

    def to_dict(self) -> Dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "thread_id": self.thread_id,
            "employee_id": self.employee_id,
            "tool_names": list(self.tool_names),
            "max_uses": self.max_uses,
            "used": self.used,
            "remaining": self.remaining,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "note": self.note,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at,
            "active": self.active_at(),
            "scope": "same_operation" if self.args_fingerprint else "tool",
        }


class GrantRegistry:
    """预授权注册表（内存态，单进程）"""

    def __init__(self) -> None:
        self._grants: Dict[str, ApprovalGrant] = {}

    def create(
        self,
        *,
        thread_id: str,
        tool_names: Sequence[str] = (),
        max_uses: Optional[int] = None,
        ttl_seconds: Optional[int] = None,
        note: str = "",
        employee_id: str = "",
        args: Optional[Dict[str, Any]] = None,
    ) -> ApprovalGrant:
        """
        建立一条预授权。

        Args:
            thread_id: 作用域会话；**必填**（不提供"对所有会话生效"的形态）
            tool_names: 覆盖的工具名；传空表示该会话内的全部 danger 工具
            max_uses: 最多自动放行几次；缺省取配置值
            ttl_seconds: 有效期秒数；缺省取配置值
            employee_id: 作用域的数字员工；工作空间里**必须给**，
                否则这条授权会同时放行同一个群组里所有成员的危险操作
        """
        uses = min(100, max(1, int(max_uses if max_uses is not None else settings.APPROVAL_GRANT_MAX_USES)))
        ttl = min(3600, max(1, int(ttl_seconds if ttl_seconds is not None else settings.APPROVAL_GRANT_TTL_SECONDS)))
        if not thread_id or not tool_names:
            raise ValueError("临时授权必须指定会话和工具")
        expires = time.time() + ttl

        grant = ApprovalGrant(
            grant_id=uuid.uuid4().hex[:12],
            thread_id=thread_id,
            employee_id=employee_id,
            tool_names=tuple(dict.fromkeys(t for t in tool_names if t)),
            max_uses=uses,
            created_at=_now_iso(),
            expires_at_epoch=expires,
            expires_at=datetime.fromtimestamp(expires, tz=timezone.utc).astimezone().isoformat(
                timespec="seconds"
            ),
            note=note,
            args_fingerprint=self.fingerprint(args) if args is not None else "",
        )
        self._grants[grant.grant_id] = grant
        logger.info(
            "已建立审批预授权: thread=%s emp=%s tools=%s 次数=%s 有效期=%ss",
            thread_id, employee_id or "—", list(grant.tool_names) or "全部危险工具", uses, ttl,
        )
        return grant

    def match(
        self, thread_id: str, tool_name: str, *, employee_id: str = "", args: Optional[Dict[str, Any]] = None
    ) -> Optional[ApprovalGrant]:
        """
        找出能覆盖本次调用的有效预授权（同一会话可能有多个，取剩余次数最多的）。

        顺带清理过期项 —— 这样不需要单独的后台清理任务，
        而过期项本来也只会在"有审批发生"时才被关心。

        ★ `employee_id` 精确匹配（空串只匹配空串）：
        群组里三个员工共用 thread_id，宽松匹配等于让甲的授权替乙放行危险操作。
        """
        self.prune()
        candidates = [
            g for g in self._grants.values()
            if g.thread_id == thread_id
            and g.employee_id == employee_id
            and g.covers(tool_name)
            and (not g.args_fingerprint or (args is not None and g.args_fingerprint == self.fingerprint(args)))
            and g.active_at()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda g: (g.remaining, g.expires_at_epoch))

    def consume(self, grant_id: str) -> Optional[ApprovalGrant]:
        """消费一次（自动放行后调用）"""
        grant = self._grants.get(grant_id)
        if grant is None or not grant.active_at():
            return None
        grant.used += 1
        if grant.remaining <= 0:
            logger.info("预授权已用尽: %s", grant_id)
        return grant

    @staticmethod
    def fingerprint(args: Dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    def list_active(self) -> list[Dict[str, Any]]:
        self.prune()
        return [g.to_dict() for g in self._grants.values() if g.active_at()]

    def revoke(self, grant_id: str) -> bool:
        grant = self._grants.get(grant_id)
        if grant is None:
            return False
        grant.revoked = True
        grant.revoked_at = _now_iso()
        return True

    def revoke_thread(self, thread_id: str) -> int:
        """吊销某个会话的全部预授权（用户点「全部停止自动放行」时用）"""
        count = 0
        for grant in self._grants.values():
            if grant.thread_id == thread_id and not grant.revoked:
                grant.revoked = True
                grant.revoked_at = _now_iso()
                count += 1
        if count:
            logger.info("已吊销会话 %s 的 %s 条预授权", thread_id, count)
        return count

    def prune(self) -> int:
        """删除已失效（过期 / 用尽 / 已吊销）的授权"""
        dead = [
            gid for gid, g in self._grants.items()
            if g.revoked or g.remaining <= 0 or time.time() >= g.expires_at_epoch
        ]
        for gid in dead:
            self._grants.pop(gid, None)
        return len(dead)

    def stats(self) -> Dict[str, Any]:
        self.prune()
        active = [g for g in self._grants.values() if g.active_at()]
        return {
            "enabled": True,
            "active": len(active),
            "total": len(self._grants),
            "remaining_uses": sum(g.remaining for g in active),
            "default_ttl_seconds": settings.APPROVAL_GRANT_TTL_SECONDS,
            "default_max_uses": settings.APPROVAL_GRANT_MAX_USES,
        }

    def reset(self) -> None:
        """清空（测试用）"""
        self._grants.clear()


# 全局单例
grant_registry = GrantRegistry()


__all__ = ["ApprovalGrant", "GrantRegistry", "grant_registry"]
