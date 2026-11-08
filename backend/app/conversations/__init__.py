"""
会话历史 —— 把「聊过什么」变成可回看、可继续的一等实体（Phase 7）

在此之前，一个线程的对话只活在两个地方：Redis Checkpointer（给模型看的图状态）
与浏览器内存（用户切走标签页就没了）。于是：

- 刷新/关闭页面 = 聊天记录消失（`thread_id` 存在 sessionStorage，新标签页更是直接新建）；
- 没有任何办法「回到昨天那个会话」；
- 重启 Redis 之后连模型自己也不记得刚才聊过什么，而界面上什么提示都没有。

| 文件 | 解决什么 | 对外入口 |
| :--- | :--- | :--- |
| `models.py` | 一轮对话的数据形态（含事件流快照与四种收尾状态） | `ConversationTurn` |
| `store.py`  | 落盘、索引、列表/删除/改名（文件是唯一事实来源） | `conversation_store` |

★ 与 Checkpointer / 长期记忆的分工

    conversations   用户视角的「聊天记录」 —— 跨进程、跨重启，语义是"回看"
    checkpointer    模型视角的「上下文」   —— Redis 内存态，语义是"接着聊"
    memory          用户画像的「偏好」     —— 跨会话持续影响输出，语义是"记住你是谁"

三者都必要，且**不能互相替代**：聊天记录里没有偏好（那是记忆的事），
偏好里也没有聊天记录（记忆只存被抽出来的结论）。恢复一个历史会话时，
三者会同时被用到：历史负责界面，Checkpointer 负责上下文（缺失时由历史播种）。

★ 为什么它是「增益」不是「依赖」

写入口在 SSE 事件流里，读入口是一组只读接口。任何一步失败
（磁盘满、索引损坏、thread_id 非法）都只表现为「这次没记上 / 列表少一条」，
问答链路本身完全不受影响 —— 与观测层、记忆层同一条边界。
"""

from __future__ import annotations

from app.conversations.models import (
    INITIATED_BY_ASSISTANT,
    INITIATED_BY_USER,
    STATUS_ABORTED,
    STATUS_ERROR,
    STATUS_INTERRUPTED,
    STATUS_LABELS,
    STATUS_OK,
    ConversationSummary,
    ConversationTurn,
    derive_title,
    infer_status,
    is_trivial_turn,
    summarize_turns,
)
from app.conversations.store import (
    ASSISTANT_MAIN_THREAD_ID,
    ASSISTANT_THREAD_PREFIX,
    ConversationStore,
    conversation_store,
    is_assistant_thread_id,
    is_valid_thread_id,
)

__all__ = [
    "ASSISTANT_MAIN_THREAD_ID",
    "ASSISTANT_THREAD_PREFIX",
    "INITIATED_BY_ASSISTANT",
    "INITIATED_BY_USER",
    "ConversationStore",
    "ConversationSummary",
    "ConversationTurn",
    "STATUS_ABORTED",
    "STATUS_ERROR",
    "STATUS_INTERRUPTED",
    "STATUS_LABELS",
    "STATUS_OK",
    "conversation_store",
    "derive_title",
    "infer_status",
    "is_assistant_thread_id",
    "is_trivial_turn",
    "is_valid_thread_id",
    "summarize_turns",
]
