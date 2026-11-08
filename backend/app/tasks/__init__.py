"""
后台子任务的持久化层（Supervisor 架构，见 ADR 0019）。

对外三个入口：

- `TaskRecord` / `ProgressEntry` / `TaskProgressView` —— 数据契约（`models.py`）
- `TaskStore` / `task_store` —— 持久化（`store.py`）
- `SubtaskExecutor` —— 后台执行器（`executor.py`，阶段 3 才接上）

★ 这个包为什么独立于 `app/agents/`

子任务有**自己的生命周期**：主对话那一轮早就结束了（图早就 END 了），
它还在跑；用户关了页面、刷新、甚至重启服务，它的状态都还在。
把它塞进 `agents/` 会让人误以为它的生命周期跟着图走 ——
而图的生命周期是 SSE 连接决定的。
分包的唯一目的是让「它比图活得久」这件事在目录结构上就是显然的。
"""

from app.tasks.models import (
    ProgressEntry,
    TaskProgressView,
    TaskRecord,
    make_task_id,
    is_valid_task_id,
)
from app.tasks.store import TaskStore, task_store

__all__ = [
    "ProgressEntry",
    "TaskProgressView",
    "TaskRecord",
    "TaskStore",
    "is_valid_task_id",
    "make_task_id",
    "task_store",
]
