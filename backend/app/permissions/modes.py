"""
权限模式：只读、手动审批、自动执行、完全访问。

默认模式由用户设置 API 持久化，会话只能收紧。手动审批对改动类工具逐次确认；
自动执行放行常规改动、保留高风险确认；完全访问放行高风险并解除文件工具目录限制。
明确的询问、禁止规则和 Hooks 仍生效。文件边界是应用层检查，不是进程隔离。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

READ_ONLY = "read_only"
MANUAL = "manual"
AUTO = "auto"
FULL = "full"

#: 由紧到松。顺序是**语义**的一部分：收紧/放宽的比较依赖它，界面也按这个顺序排。
MODE_ORDER: tuple[str, ...] = (READ_ONLY, MANUAL, AUTO, FULL)

_LABELS: Dict[str, str] = {
    READ_ONLY: "只读",
    MANUAL: "手动审批",
    AUTO: "自动执行",
    FULL: "完全访问",
}

#: 给人看的一句话。措辞刻意保守 —— 这一档决定的是「她会不会动我的东西」，
#: 把 `full` 写成「高级功能」是会出事的。
_HINTS: Dict[str, str] = {
    READ_ONLY: "只查、只读、只回答。任何会改动东西的操作都会被直接拒绝，不弹窗。",
    MANUAL: "读取类操作直接做；修改文件、执行命令等改动类操作先由你确认。",
    AUTO: "自动执行常规操作；高风险操作仍需确认。文件工具遵守工作目录范围，明确的询问与禁止规则始终生效。",
    FULL: "自动执行高风险操作，并解除文件工具的目录限制；明确的询问与禁止规则始终生效。仅用于你信任的任务。",
}

#: 自动放行时写进留痕的话术（`decided_by` 的说明，见 approval/records.py）
def auto_approve_reason(mode: str) -> str:
    return f"按当前权限模式「{label(mode)}」自动放行"


def is_valid(mode: str) -> bool:
    return mode in MODE_ORDER


def normalize(mode: Optional[str], *, default: str = MANUAL) -> str:
    """
    宽松收口：空 / 未知值一律退回默认档，**不抛错**。

    ⚠️ 这里抛错的后果是「本轮对话整个失败」，而未知模式名只可能来自
    前端旧版本或手改过的配置文件 —— 为一个拼写错误打断作答不值得。
    退回哪一档由调用方决定（配置默认值 or `MANUAL`）。
    """
    text = (mode or "").strip().lower()
    return text if text in MODE_ORDER else default


def label(mode: str) -> str:
    return _LABELS.get(normalize(mode), mode)


def hint(mode: str) -> str:
    return _HINTS.get(normalize(mode), "未知权限模式")


def allows_mutation(mode: str) -> bool:
    """这一档允许不允许「会改动东西」的操作（moderate / danger）"""
    return normalize(mode) != READ_ONLY


def allows_auto_approve(mode: str) -> bool:
    """这一档能不能不等人工批准就执行 danger 操作"""
    return normalize(mode) == FULL


def escapes_file_boundary(mode: str) -> bool:
    """这一档是否解除沙箱 + `AGENT_ALLOWED_DIRS` 的目录限制"""
    return normalize(mode) == FULL


def compare(a: str, b: str) -> int:
    """-1 = a 更紧，0 = 同级，1 = a 更松（顺序即松紧，见模块注释）

    ★ 返回的是**符号**，不是档位差：`tighten()` 只比较 `<= 0`，
    但一个写着 -1/0/1 的函数别人敢写 `== 1`，写着「差了三档」的没人敢。
    契约收窄成符号，才有资格被人当布尔用。
    """
    left, right = MODE_ORDER.index(normalize(a)), MODE_ORDER.index(normalize(b))
    return (left > right) - (left < right)


def tighten(mode: str, cap: str) -> str:
    """取两者中**更紧**的一档。会话级覆盖只会收紧，不会放宽。"""
    return mode if compare(mode, cap) <= 0 else cap


@dataclass
class PermissionModeInfo:
    """一档模式的完整对外表示（界面直接渲染，不需要再拼文案）"""

    value: str
    label: str
    hint: str
    #: 界面用它排序/显示松紧程度
    looseness: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "label": self.label,
            "hint": self.hint,
            "looseness": self.looseness,
        }


def mode_catalog() -> List[Dict[str, Any]]:
    return [
        PermissionModeInfo(
            value=m, label=_LABELS[m], hint=_HINTS[m], looseness=i
        ).to_dict()
        for i, m in enumerate(MODE_ORDER)
    ]


class ModeStore:
    """
    默认模式的原子存储。文件不存在时取环境默认值，用户设置写入后以文件为准。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None

    def _read(self) -> Dict[str, Any]:
        path = settings.permission_mode_path
        if not path.exists():
            # 出厂默认来自 .env：装到新机器上时用户可以先用环境变量定一档，
            # 之后界面的选择覆盖它（文件一旦写下就以它为准）。
            return {
                "version": 1,
                "default_mode": normalize(settings.PERMISSION_MODE, default=MANUAL),
                "changed_at": "",
            }
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("权限配置读取失败，退回默认档 manual: %s", e)
            return {"version": 1, "default_mode": MANUAL, "changed_at": ""}
        if not isinstance(raw, dict):
            return {"version": 1, "default_mode": MANUAL, "changed_at": ""}
        return {
            "version": 1,
            "default_mode": normalize(raw.get("default_mode"), default=MANUAL),
            "changed_at": str(raw.get("changed_at") or ""),
        }

    def load(self, *, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if self._cache is None or force:
                self._cache = self._read()
            return dict(self._cache)

    def save(self, mode: str) -> Dict[str, Any]:
        """仅由用户设置 API 调用；写盘成功后再更新快照。"""
        if not is_valid(mode):
            raise ValueError("未知权限模式")
        with self._lock:
            payload = {
                "version": 1, "default_mode": mode,
                "changed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            path = settings.permission_mode_path
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            self._cache = payload
            return dict(payload)

    @property
    def default_mode(self) -> str:
        return self.load()["default_mode"]


#: 进程级单例
mode_store = ModeStore()


__all__ = [
    "AUTO",
    "FULL",
    "MANUAL",
    "MODE_ORDER",
    "READ_ONLY",
    "ModeStore",
    "allows_auto_approve",
    "allows_mutation",
    "auto_approve_reason",
    "compare",
    "escapes_file_boundary",
    "hint",
    "is_valid",
    "label",
    "mode_catalog",
    "mode_store",
    "normalize",
    "tighten",
]
