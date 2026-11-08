"""
内置工具 —— 联网搜索 + 网页抓取 (web_search / web_extractor)

走阿里云百炼 Responses API 的**服务端内置工具**：服务端模型带工具真实执行后
生成带事实依据的摘要。按 query 自动分两条路：

- **搜索路**（query 是关键词）：`web_search`，来源 URL 在 `web_search_call.action.sources`
  （⚠️ 不在 message 的 annotations 里，实测后者恒为空）；
- **抓取路**（query 含网址）：`web_extractor`（官方契约：必须与 web_search 同时启用），
  被读取的页面在 `web_extractor_call.urls`。用户发链接求「看这篇文章」就走这条路 ——
  它才是「读网页」意图的正确工具（2026-10-03 接入，限时免费）。

后端默认复用 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`（本项目的网关就是 Qwen 系
兼容站，实测可用）；需要独立搜索后端时用 `WEB_SEARCH_BASE_URL / _API_KEY / _MODEL`
单独指定。

★ 三个实测数字决定了实现（2026-10，qwen3.7-flash）：
- 搜索开思考 74s / 关思考 6.3s —— 搜索路默认强制关思考（`WEB_SEARCH_DISABLE_THINKING`；
  该参数是 Qwen 系网关扩展，换别家网关报 400 就关掉它）；
- **抓取路网关强制开思考**：非思考模式直接 400（"Normal mode does not support
  web_extractor"），实测抓取+摘要 ~23s —— 这就是抓取路预算单独给 60s 的原因；
- 输入必须**显式点名工具**（各路自己的前缀），否则服务端模型会凭自己的知识
  「推测着答」，返回里连工具调用条目都不出现。

★ 诚实边界：响应里没有 web_search_call / web_extractor_call 就等于**没执行**。
绝不能把模型编出来的「搜索结果」当成联网事实喂给主模型 —— 直接报「未生效」，
让它用自己的知识答。官方 FAQ 点名的「未触发」原因有二：模型自主判断无需联网
（用上面的点名前缀压低概率）、账号级 15 RPS 限流（**不报错、静默跳过**）——
所以「未生效」日志出现得反常地频繁时，先查请求频率再怀疑别的。

失败降级与旧版（DuckDuckGo Instant Answer，2026-10-03 前的实现）一致：
工具永不抛异常打断 Graph 流程。

★ 超时是这里最要紧的一个数
联网仍是**可选增益**：搜到了更好，搜不到模型靠自己的知识照样答。连接阶段单独给更短
的预算（地址不通是秒级可知的）；预算必须盖住健康耗时且**跟着搜索子调用模型走**：
qwen3.7-flash 档（搜索 ~6s / 抓取 ~23s）时是 20s/60s；换 qwen3.8-max 后（搜索
15~20s / 抓取 28~60s+，思考方差大）改成 40s/120s（2026-10-04 实测校准）——
都是「网关慢但不该死」与「用户不该干等」的取舍。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# 服务端模型对「要不要真搜」有裁量权 —— 不点名工具它就靠参数知识作答
_FORCE_SEARCH_PREFIX = "你必须使用联网搜索工具查询后回答："
_FORCE_FETCH_PREFIX = "你必须使用网页抓取工具读取该网页后回答："

# query 里带 URL = 用户意图是「读这个网页」→ 走官方 web_extractor（网页抓取）路；
# 否则是「查资料」→ 走 web_search（联网搜索）路。两条路在网关上有硬差异（见 _payload）。
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _base_url() -> str:
    """搜索后端；留空 = 复用 LLM 网关（同一把钥匙、同一个 base_url）"""
    return (settings.WEB_SEARCH_BASE_URL or settings.LLM_BASE_URL).rstrip("/")


def _api_key() -> str:
    return settings.WEB_SEARCH_API_KEY or settings.LLM_API_KEY


def _model() -> str:
    return settings.WEB_SEARCH_MODEL or settings.LLM_MODEL


def _headers() -> dict[str, str]:
    # 「没配就不发」：密钥为空时不兜空字符串、不造 `Bearer ` 空头
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _is_fetch(query: str) -> bool:
    """query 含网址 → 抓取路（web_extractor）；否则搜索路（web_search）"""
    return bool(_URL_RE.search(query or ""))


def _payload(query: str) -> dict[str, Any]:
    fetch = _is_fetch(query)
    tools = [{"type": "web_search"}]
    if fetch:
        # 官方契约：web_extractor 必须与 web_search 同时启用
        tools.append({"type": "web_extractor"})
    payload: dict[str, Any] = {
        "model": _model(),
        "input": f"{_FORCE_FETCH_PREFIX if fetch else _FORCE_SEARCH_PREFIX}{query}",
        "tools": tools,
        "stream": False,
    }
    if fetch:
        # ⚠️ 网关硬约束（实测 400）："Normal mode does not support web_extractor.
        # Please set enable_thinking to true." —— 抓取路必须显式开思考，
        # 代价是这条子调用回到 20s+ 档（实测 ~23s），预算见 WEB_FETCH_TIMEOUT_SECONDS。
        payload["enable_thinking"] = True
    elif settings.WEB_SEARCH_DISABLE_THINKING:
        # 搜索路维持关思考：实测同一查询开思考 74s / 关思考 6.3s
        payload["enable_thinking"] = False
    return payload


def _timeout(total: float) -> httpx.Timeout:
    """整体预算由调用方按路型给定；连接阶段再收紧（地址不通不值得等这么久）"""
    return httpx.Timeout(max(1.0, float(total)), connect=min(3.0, float(total)))


def _extract(data: dict[str, Any], max_results: int) -> str | None:
    """
    从 Responses API 响应里提取「真实发生过的检索」的摘要与来源

    Returns:
        摘要正文 + 来源列表；返回 None 表示服务端模型未实际触发联网检索。
    """
    sources: list[str] = []
    texts: list[str] = []
    searched = False

    for item in data.get("output") or []:
        item_type = item.get("type")
        if item_type == "web_search_call":
            searched = True
            action = item.get("action") or {}
            for src in action.get("sources") or []:
                url = src.get("url")
                if url and url not in sources:
                    sources.append(url)
        elif item_type == "web_extractor_call":
            # 抓取路：官方结构 web_extractor_call.urls（被读取的页面）
            searched = True
            for url in item.get("urls") or []:
                if url and url not in sources:
                    sources.append(url)
        elif item_type == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text" and part.get("text"):
                    texts.append(part["text"])

    if not searched:
        return None

    body = "\n\n".join(t.strip() for t in texts if t.strip()) or "（搜索服务未返回摘要文本）"
    lines = [body]
    if sources:
        lines.append("来源:")
        lines.extend(f"- {url}" for url in sources[:max_results])
    return "\n".join(lines)


async def web_search(query: str, max_results: int = 5) -> str:
    """
    联网搜索 / 网页抓取（按 query 自动路由），返回事实性摘要与来源链接

    - query 是关键词 → 联网搜索路（关思考，快）
    - query 含网页地址 → 网页抓取路（web_extractor，网关强制开思考，慢但能读正文）

    Args:
        query: 搜索关键词，或要读取的网页地址
        max_results: 最多展示的来源条数
    """
    budget = (
        settings.WEB_FETCH_TIMEOUT_SECONDS
        if _is_fetch(query)
        else settings.WEB_SEARCH_TIMEOUT_SECONDS
    )
    try:
        async with httpx.AsyncClient(timeout=_timeout(budget)) as client:
            resp = await client.post(
                f"{_base_url()}/responses", headers=_headers(), json=_payload(query)
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        # 4xx/5xx 要分清「额度墙」和「网关抖动」：本机网关免费额度耗尽时先挂起（表现为
        # ReadTimeout）、后硬 403（insufficient_quota / PERMISSION_DENIED）且间歇放行。
        # 混在一句「不可用」里会把排查方向引向代码 —— 额度问题必须点名。
        detail = ""
        try:
            body = e.response.json() or {}
            # 网关把 code/message 放在**顶层**（不是 OpenAI 的 error 嵌套）——
            # 两层都读，哪层有字读哪层。
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
            logger.error("搜索后端账号额度不足/被拒: %s", detail)
            return (
                "[搜索后端额度不足] 联网搜索的账号免费额度已耗尽或访问被拒，本次无法联网。"
                "请基于已有知识回答，不要臆造搜索结果，并建议用户检查搜索后端账户余额。"
            )
        logger.warning("联网搜索失败: %s", e)
        return f"[联网搜索不可用] 无法获取 '{query}' 的搜索结果（HTTP {e.response.status_code}）。请基于已有知识回答。"
    except Exception as e:  # noqa: BLE001
        logger.warning("联网搜索失败: %s", e)
        return f"[联网搜索不可用] 无法获取 '{query}' 的搜索结果（{type(e).__name__}）。请基于已有知识回答。"

    extracted = _extract(data, max_results=max(1, int(max_results)))
    if extracted is None:
        # 没搜/没抓就是没有：模型自己编的「搜索结果」比没有结果危害更大
        logger.warning("联网工具未生效：服务端模型未触发 web_search/web_extractor (query=%r)", query)
        return (
            f"[联网搜索未生效] '{query}' 的联网检索或网页抓取未被实际执行。"
            "请基于已有知识回答，不要臆造搜索结果。"
        )

    usage = data.get("usage") or {}
    logger.info(
        "联网搜索完成: query=%r, 工具调用=%s",
        query,
        (usage.get("x_tools") or {}).get("web_search", {}).get("count", "?"),
    )
    return extracted


async def _health() -> bool:
    """健康检查：搜索复用 LLM 网关，能带凭据探到 /models 即视为健康"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            resp = await client.get(f"{_base_url()}/models", headers=_headers())
            return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False
