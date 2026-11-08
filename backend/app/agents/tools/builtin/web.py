"""
内置工具 —— 联网（web_search / web_fetch）

★ 为什么拆成两个工具

原先只有 `web_search`，靠「query 里有没有网址」在内部路由到抓取路。后果是模型
必须猜这台机器上的路由规则：想搜索时粘贴了引用来源的链接 → 意外走成 23s 的抓取；
想读网页时忘了带协议头 → 静默变成一次关键词搜索，返回一堆无关摘要。
主流 Agent 把两者分开命名（WebSearch / WebFetch），意图由工具名表达，
不由参数格式猜测。

底层实现复用 `web_search.py` 里那套经过实测校准的网关调用与诚实降级逻辑
（响应里没有 web_search_call / web_extractor_call 就等于没执行，
绝不能把模型编的「搜索结果」当事实回灌）。
"""

from __future__ import annotations

import logging
import re
import asyncio

import httpx

from app.agents.tools.builtin import web_search as _impl
from app.agents.tools.decorator import agent_tool
from app.config import settings
from app.schemas.tools import ToolResult

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


async def _call(query: str, *, fetch: bool, max_results: int) -> str:
    """按指定路型调用网关（fetch=True 走 web_extractor，False 走 web_search）"""
    budget = (
        settings.WEB_FETCH_TIMEOUT_SECONDS
        if fetch
        else settings.WEB_SEARCH_TIMEOUT_SECONDS
    )
    payload = _impl._payload(query)
    # _payload 依据 query 里有没有 URL 自行选路；这里按调用方的**显式意图**覆盖，
    # 模型选哪个工具就是哪个意思，不再依赖参数格式。
    payload["tools"] = [{"type": "web_search"}] + (
        [{"type": "web_extractor"}] if fetch else []
    )
    if fetch:
        payload["enable_thinking"] = True
    elif settings.WEB_SEARCH_DISABLE_THINKING:
        payload["enable_thinking"] = False

    try:
        async with httpx.AsyncClient(timeout=_impl._timeout(budget)) as client:
            resp = await client.post(
                f"{_impl._base_url()}/responses", headers=_impl._headers(), json=payload
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json() or {}
            # 与 web_search.py 同一处坑：网关把 code/message 放在顶层
            err = body.get("error") or {}
            detail = str(
                err.get("code")
                or err.get("message")
                or body.get("code")
                or body.get("message")
                or ""
            )
        except Exception:  # noqa: BLE001
            pass
        lowered = detail.lower()
        if "quota" in lowered or "permission_denied" in lowered or "free tier" in lowered:
            logger.error("联网后端账号额度不足/被拒: %s", detail)
            return (
                "[联网额度不足] 联网检索的账号免费额度已耗尽或访问被拒，本次无法联网。"
                "请基于已有知识回答，不要臆造搜索结果，并建议用户检查搜索后端账户余额。"
            )
        logger.warning("联网调用失败: %s", e)
        return f"[联网不可用] 调用失败（HTTP {e.response.status_code}）。请基于已有知识回答，不要臆造结果。"
    except Exception as e:  # noqa: BLE001
        logger.warning("联网调用失败: %s", e)
        return f"[联网不可用] 调用失败（{type(e).__name__}）。请基于已有知识回答，不要臆造结果。"

    extracted = _impl._extract(data, max_results=max_results)
    if extracted is None:
        logger.warning("联网工具未生效：服务端模型未触发 %s (query=%r)", "web_extractor" if fetch else "web_search", query)
        return (
            "[联网未生效] 这次请求没有被服务端实际执行检索/抓取。"
            "请基于已有知识回答，不要臆造搜索结果或网页内容。"
        )
    return extracted


@agent_tool(
    display_name="联网搜索",
    category="网络",
    danger_level="safe",
    read_only=True,
    allowed_agents=["browser_agent"],
    health_handler=_impl._health,
)
async def web_search(query: str = "", max_results: int = 5, queries: list[str] | None = None) -> str | ToolResult:
    """联网搜索实时信息，返回带来源链接的事实性摘要。

    需要「当下/最新」的信息（天气、汇率、新闻、版本、价格、政策）时用它 ——
    离线 MCP 工具返回的是基线数据，别拿它冒充实时结果。
    多个独立主题请用 queries 一次批量搜索，内部限流并行，避免逐个等待。
    要读某个具体网页请改用 web_fetch。

    Args:
        query: 搜索关键词（不要在这里粘贴网址）
        max_results: 最多返回几条来源
        queries: 独立查询列表（最多6条），例如美股、美债、汇率、商品、欧洲、日本
    """
    if queries:
        if len(queries) > 6:
            return _checked_result("[错误] 每批最多6个独立查询，请拆分批次。")
        items = [q.strip() for q in queries if q.strip()]
        if not items:
            return _checked_result("[错误] 搜索主题不能为空。")
        slots = asyncio.Semaphore(3)
        async def search_one(topic: str) -> str:
            async with slots:
                return await _call(topic, fetch=False, max_results=max(1, int(max_results)))
        results = await asyncio.gather(*(search_one(topic) for topic in items))
        text = "\n\n".join(f"【{topic}】\n{result}" for topic, result in zip(items, results))
        if all(isinstance(_checked_result(result), ToolResult) for result in results):
            return ToolResult(success=False, result="", duration_ms=0, error=text)
        return text
    if not query.strip():
        return _checked_result("[错误] 请提供搜索主题。")
    return _checked_result(await _call(query, fetch=False, max_results=max(1, int(max_results))))


@agent_tool(
    display_name="读取网页",
    category="网络",
    danger_level="safe",
    read_only=True,
    allowed_agents=["browser_agent"],
    health_handler=_impl._health,
)
async def web_fetch(
    url: str,
    focus: str = "",
    max_results: int = 8,
) -> str | ToolResult:
    """读取指定网页并返回内容摘要（走服务端网页抓取）。

    用户发来链接要求「看看这篇 / 总结一下」时用这个，而不是 web_search。
    内容是服务端模型读页后生成的摘要，并会列出实际被读取的页面地址。

    Args:
        url: 完整网页地址（含 http/https）
        focus: 想让回答聚焦的点，例如「只看价格与发布时间」
        max_results: 最多列出几个来源页面
    """
    if not _URL_RE.search(url or ""):
        return _checked_result(f"[错误] '{url}' 不像一个网页地址，需要带 http:// 或 https://。要搜索资料请用 web_search。")
    query = f"{url.strip()}\n请重点回答：{focus.strip()}" if focus.strip() else url.strip()
    return _checked_result(await _call(query, fetch=True, max_results=max(1, int(max_results))))


def _checked_result(text: str) -> str | ToolResult:
    if text.startswith(("[联网", "[错误]")):
        return ToolResult(success=False, result="", duration_ms=0, error=text)
    return text


__all__ = ["web_fetch", "web_search"]
