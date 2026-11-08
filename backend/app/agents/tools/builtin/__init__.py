"""
内置工具集合 —— 装配入口

工具定义分布在各个 pack 模块里：每个工具在**自己的函数**上声明描述、参数 Schema、
危险等级与权限，`import` 即注册。本模块只做两件事 —— 把 pack 导进来触发注册，
以及给应用启动提供一个明确入口。

★ 这里不再维护权限总表

原先文件顶部抄了一份「工具 × 危险等级 × 授权 Agent」的表，改工具的人不会记得
同步改它，于是治理界面、prompt、真实权限三者会各说各话。现在那份表只能从
`tool_registry.list_all()` 读出来（`GET /api/tools`），代码即唯一事实。
"""

from __future__ import annotations

import logging

# 导入即注册。web_search 是被 web.py 复用的底层实现，自身不注册工具。
from app.agents.tools.builtin import (  # noqa: F401
    doc_ops,
    files,
    knowledge,
    memory_ops,
    office,
    questions,
    shell,
    skills_ops,
    subtasks,
    team,
    web,
    workflows,
)
from app.agents.tools.registry import tool_registry

logger = logging.getLogger(__name__)


def register_builtin_tools() -> None:
    """注册全部内置工具（幂等：装饰器在 import 时已完成，这里只做一次自检）"""
    stats = tool_registry.stats()
    if not stats["total"]:
        logger.error("内置工具注册结果为空 —— pack 模块可能没被导入")
        return
    logger.info(
        "内置工具就绪: %s 个（safe=%s moderate=%s danger=%s），分类=%s",
        stats["total"], stats["safe"], stats["moderate"], stats["danger"],
        tool_registry.categories(),
    )


__all__ = ["register_builtin_tools"]
