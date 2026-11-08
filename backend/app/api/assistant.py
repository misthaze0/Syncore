"""
助理专属接口 —— 目前只有「打开会话时助手可以先开口」

★ 为什么是 POST 而不是 GET

它会**写一条会话记录**（落盘一轮 `initiated_by=assistant`），不是只读查询。
用 GET 的话，浏览器预取、刷新、开发者工具的「重放请求」都会各自触发一次生成 ——
那意味着用户在没打开助理页时也在消耗 token。

★ 为什么不在服务启动时预热

主动问候依赖「用户此刻是否真在看」。后台生成只会得到一段没人看见的文本，
还会在用户没打开助理时白白消耗模型调用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter

from app.assistant import maybe_checkin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.post("/checkin")
async def assistant_checkin() -> Dict[str, Any]:
    """
    尝试让助理主动说一句。

    不满足条件时返回 `ok=false` + `reason`，这是**正常结果**而不是错误：
    前端拿到后什么都不做即可，不该显示报错。
    """
    result = await maybe_checkin()
    if not result.get("ok"):
        logger.debug("助理主动问候未触发: %s", result.get("reason"))
    return result
