"""
工作空间模块 —— 群组 + 数字员工。

对外导出：
- `workspace_store`：六表 CRUD 单例
- `emp_thread_id` / `is_employee_thread_id`：员工私聊的命名空间隔离
- `employee_runtime`：per-employee persona / 工具白名单渲染
- `group_bus` / `orchestrator`：群组事件总线与编排器（阶段 3 挂载）

★ 员工私聊 thread 的命名空间隔离（不变量 58 的第三根前缀）

- 用户主会话：uuid4
- 后台子任务：`subtask_<task_id>`（`agents/subgraph.py::subtask_thread_id`）
- 员工私聊：`emp_<employee_id>`（本模块）
- 助理会话：`assistant_<uuid>`（`conversations/store.py`，2026-10 新增）

前缀让「不相撞」成为结构保证；主对话入口（`graph.get_agent_event_stream`）
与员工私聊入口各自拒绝其它前缀，挡住手工构造的撞入（`assistant_` 例外：
助理对话走主链路，隔离发生在列表/搜索接口的 scope 过滤）。
"""

from app.workspace.db import WorkspaceDB, workspace_db
from app.workspace.store import WorkspaceStore, workspace_store

#: 员工私聊 thread 的前缀。员工 id 是 16 位 hex，`emp_` 占 4 位，总长 20，
#: 满足 `^[A-Za-z0-9_-]{1,64}$`（不变量 47），与 `subtask_`（8 位）同理。
EMPLOYEE_THREAD_PREFIX = "emp_"


def emp_thread_id(employee_id: str) -> str:
    """员工私聊的 thread_id（checkpointer key 与 conversation 文件共用）。"""
    return f"{EMPLOYEE_THREAD_PREFIX}{employee_id}"


def is_employee_thread_id(thread_id: str) -> bool:
    """这个 thread_id 属于员工私聊命名空间（主对话入口必须拒绝它）。"""
    return bool(thread_id) and thread_id.startswith(EMPLOYEE_THREAD_PREFIX)


__all__ = [
    "EMPLOYEE_THREAD_PREFIX",
    "WorkspaceDB",
    "WorkspaceStore",
    "emp_thread_id",
    "is_employee_thread_id",
    "workspace_db",
    "workspace_store",
]
