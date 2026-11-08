"""
模型清单接口 —— 输入框右下角那颗「模型」钮的候选来源

★ 只列「保存成功的连接里的模型」

候选不再是 `settings.available_models` 那份全局手写清单，而是
`data/llm_profiles.json` 里**每家供应商各自保存过的模型**（`profiles[].models`）。
每条都带 `provider` 与 `profile_id`：对话下拉据此分组，切模型时把
**地址 + Key + 模型** 一起换过去 —— 只改 `LLM_MODEL` 会让模型与供应商错位。

没配 Key 的供应商即使目录里有预设模型名也不会出现在这里：那是填表建议，
不是可用的连接。列出来只会让人选到一个必然 401 的选项。

★ 为什么不再返回 `selectable`（2026-10-18 改版）

原来「只有一个候选就不渲染下拉」，理由是点了也不会变。输入框改版后这颗钮常驻：
它同时是「这一句用谁回答」的入口和通往「模型管理」的入口 ——
只有一个候选时用户更想问的恰恰是「为什么只有一个」，那就该点得开。

★ 与 `/api/llm/models`（设置页）的分工

这里回答「**现在能选什么**」（只读候选，供输入框渲染，永不失败）；
那边回答「**默认是哪个、怎么改**」（读配置 + 写运行时覆写 + 自检）。
输入框下拉里点某个模型 = 走 `PUT /api/llm/models` 把它设为**全局默认**
（2026-10-20 改版：不再有「只覆盖这一句」），所以这个只读接口不承载切换。
"""

import logging
from typing import Any, Dict
from urllib.parse import urlparse

from fastapi import APIRouter

from app.config import settings
from app.llm import model_settings, profiles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])


def _provider_label() -> str:
    """
    当前这条连接「叫什么」—— 模型弹层顶上那行小字。

    预设供应商用它的名字（阿里云百炼 / DeepSeek…）；自定义端点的名字
    一律是「自定义端点」（表单没有改名的地方），那等于没说 —— 换成端点域名，
    至少能回答「我现在用的是哪台」。
    """
    base_url = str(settings.LLM_BASE_URL or "").strip()
    if not base_url:
        return ""
    host = urlparse(base_url).hostname or ""
    hit = None
    try:
        hit = profiles.find_by_base_url(profiles.ensure_seeded(), base_url)
        if hit and hit.get("builtin"):
            return str(hit["name"])
    except Exception as e:  # noqa: BLE001
        # 目录读不出来不该让清单接口失败 —— 这行小字丢了也只是少个副标题
        logger.warning("匹配供应商名称失败（不影响候选清单）: %s", e)
    return host or (str(hit["name"]) if hit else "")


@router.get("")
async def list_models() -> Dict[str, Any]:
    """
    可用模型清单。

    - `models`: `[{"id", "label", "default", "provider", "profile_id"}]` ——
      每家保存成功连接里的模型；`default` 标出「模型 + 连接」都对上的那一个
    - `default`: 当前全局默认模型名
    - `provider`: 当前生效连接的供应商显示名（自定义端点给域名）
    - `active_profile_id`: 当前生效连接对应的 profile；不在目录里为 null
    """
    active_id = None
    try:
        catalog = profiles.ensure_seeded()
        models = model_settings.usable_models(catalog)
        active = profiles.find_by_base_url(
            catalog, str(settings.LLM_BASE_URL or "").strip()
        )
        active_id = active["id"] if active else None
    except Exception as e:  # noqa: BLE001
        # 目录损坏/不可读时退回旧的全局清单：输入框至少还能显示当前模型
        logger.warning("读取供应商目录失败，退回全局候选清单: %s", e)
        models = settings.available_models

    if not models and settings.LLM_MODEL:
        # 一份候选都没有也要能回答「现在用的是谁」：当前默认模型本身
        # 就是一个配好并且在用的模型，缺了它这颗钮只能显示空白。
        models = [
            {
                "id": settings.LLM_MODEL,
                "label": settings.LLM_MODEL,
                "default": True,
                "provider": _provider_label(),
                "profile_id": active_id,
            }
        ]
    return {
        "models": models,
        "default": settings.LLM_MODEL,
        "provider": _provider_label(),
        "active_profile_id": active_id,
    }
