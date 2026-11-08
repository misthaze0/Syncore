"""
长期记忆层 —— 让 Agent 跨会话记住用户的稳定偏好

| 文件           | 职责                                                  | 外部依赖         |
|----------------|-------------------------------------------------------|------------------|
| `models.py`    | `MemoryItem` / `MemoryCandidate` / 类别与去重键         | 无               |
| `store.py`     | 单文件 JSON 持久化（原子写 + stat 缓存 + 进程内锁）      | 文件系统         |
| `extractor.py` | 抽取：显式走正则（零延迟）/ 被动走 LLM（后台）           | LLM 网关（可降级）|
| `manager.py`   | 落库、去重、构造注入 Prompt 的块                        | 无               |

★ 定位：记忆是**增益**，不是依赖

关掉 `MEMORY_ENABLED` 或存储损坏时，系统退化为「不认识用户的助手」，
问答链路完全不受影响。这与 Milvus / ES / Redis 的降级策略是同一条哲学：
**只有 LLM 是硬依赖**。

★ 与 Redis 短期记忆的分工

- Redis Checkpointer（Phase 2）= **短期记忆**：这一段对话的消息历史，绑定 thread_id，
  跟着会话走，会话结束就没有意义。
- 本模块 = **长期记忆**：跨会话成立的偏好，绑定用户，与 thread_id 无关。

两者不重叠，也不要互相替代 —— 短期记忆解决「刚才说了什么」，
长期记忆解决「这个人是怎样的」。
"""

from app.memory.manager import (
    MemoryManager,
    drain_background_tasks,
    get_manager,
)
from app.memory.models import (
    CATEGORIES,
    RELATIONSHIP_CATEGORIES,
    RELATIONSHIP_KEYS,
    SOURCES,
    MemoryCandidate,
    MemoryItem,
)
from app.memory.store import MemoryStore, get_store

__all__ = [
    "CATEGORIES",
    "RELATIONSHIP_CATEGORIES",
    "RELATIONSHIP_KEYS",
    "SOURCES",
    "MemoryItem",
    "MemoryCandidate",
    "MemoryStore",
    "MemoryManager",
    "get_store",
    "get_manager",
    "drain_background_tasks",
]
