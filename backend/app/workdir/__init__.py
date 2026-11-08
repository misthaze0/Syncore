"""
工作区（用户授权的本地目录）—— 登记表、会话绑定、目录浏览。

与 `app.workspace`（工作空间 = 群组 + 数字员工）是两回事，别混用。
详见 `store.py` 的模块注释。
"""

from __future__ import annotations

from app.workdir.store import (
    AUTO_BINDING,
    ICON_NAMES,
    ORIGIN_AUTO,
    ORIGIN_MANUAL,
    auto_root,
    auto_workdirs,
    browse,
    create_workdir,
    delete_workdir,
    dir_stats,
    ensure_auto_workdir,
    get_binding,
    get_workdir,
    is_valid_workdir_id,
    list_workdirs,
    quick_roots,
    resolve_thread_root,
    set_binding,
    touch_workdir,
    update_workdir,
)

__all__ = [
    "AUTO_BINDING",
    "ICON_NAMES",
    "ORIGIN_AUTO",
    "ORIGIN_MANUAL",
    "auto_root",
    "auto_workdirs",
    "browse",
    "create_workdir",
    "delete_workdir",
    "dir_stats",
    "ensure_auto_workdir",
    "get_binding",
    "get_workdir",
    "is_valid_workdir_id",
    "list_workdirs",
    "quick_roots",
    "resolve_thread_root",
    "set_binding",
    "touch_workdir",
    "update_workdir",
]
