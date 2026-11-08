"""
「打一次非流式模型、拿一段文本 / 一个 JSON」这两个动作。

★ 为什么单独一个模块

工作空间里有两处要打模型（编排器的主持人、协调者的拆解与认领轮），
两处的**失败语义必须完全一样**：模型不可用就返回空串、绝不抛 ——
「模型是增益，不是依赖」（与记忆/人设同一取舍）。
写成两份迟早会有一份改成抛异常，而那时症状是「群组协作整体 500」。

`app/agents/` 下另有三个各写一遍的 JSON 抽取器（`supervisor` / `graph.extractor` /
`memory.extractor`），它们对「抠不出来」的处置各不相同，所以本模块**不去合并它们**，
只服务工作空间这两处 —— 收敛的范围就是「失败语义必须一致」的那两处。
"""

from __future__ import annotations

import json
import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


async def llm_attempt(system: str, user: str, *, model: str | None = None, profile_id: str | None = None) -> tuple[str, str]:
    """
    打一次非流式模型，返回 `(文本, 错误)`。

    ★ 为什么不复用「失败就返回空串」的那条路
    交付摘要的失败必须**点名原因**并把读者指向全文所在的地方（施工图 §4.4）。
    空串分不清「模型没配」与「模型返回了空」，调用方只能说一句笼统的失败 ——
    而那句笼统的话正是「模型没干活」的伪装。错误仍然**不抛**：抛出来的话，
    一次摘要失败会把整个任务收尾打挂。
    """
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.core.llm_factory import create_llm

        llm = create_llm(streaming=False, model=model or None, profile_id=profile_id, max_retries=0, max_tokens=4096)
        from app.workspace.budget import run_budget

        ticket = run_budget.reserve(len((system + user).encode("utf-8")) // 2 + 4096)
        resp = None
        try:
            timeout = min(120.0, run_budget.remaining_seconds(ticket[0])) if ticket[0] else 120.0
            resp = await asyncio.wait_for(llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user)]), timeout)
        finally:
            run_budget.finish(ticket, getattr(resp, "usage_metadata", None))
        text = str(getattr(resp, "content", "") or "").strip()
        if not text:
            return "", "模型没有返回内容"
        return text, ""
    except Exception as e:  # noqa: BLE001
        logger.warning("工作空间模型调用失败（降级）: %s", e)
        return "", str(e)[:200]


async def llm_text(system: str, user: str, *, model: str | None = None, profile_id: str | None = None) -> str:
    """打一次非流式模型，返回纯文本；任何异常降级为空串。"""
    text, _ = await llm_attempt(system, user, model=model, profile_id=profile_id)
    return text


def parse_json(text: str) -> Any:
    """
    从模型输出里尽量抠出一个 JSON 值。

    模型可能给纯 JSON、```json 围栏、或前后夹说明文字，所以三种都试；
    都失败返回 None（由调用方决定怎么收口 —— **不在这里兜底**，
    「抠不出来」和「抠出来但是不能用」对用户是两句话）。
    """
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        candidates.append(m.group(1).strip())
    # 兜底：截第一个 `{`/`[` 到最后一个 `}`/`]` 之间的内容
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


__all__ = ["llm_attempt", "llm_text", "parse_json"]
