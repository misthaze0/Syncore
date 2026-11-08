"""
模型推理正文的切分 —— 把「思考」从「回答」里摘出来。

★ 为什么需要这个模块
本项目的网关（百炼兼容路径 + 思考型模型）在**带工具**的 Agent 调用里，会把模型自己的
推理正文混进 `content` 一起流式吐出来。它不是我们的提示词造成的（全项目搜不到 `think`），
也不是偶发 —— 实测一轮里 `answer` 共 2964 字，其中约 2900 字是英文内心独白，
末尾还带着一个字面量 `</think>`，之后才是真正给用户的那 50 个字：

    currency
    - Data sources: Offline baseline data (weather), ISO 4217 (currency).
    ...
    Protocol:
    1. Search.
    2. Fail.
    3. Refuse.

    I will search.

    **Tool Call:**
    `web_search(query="Syncore 技术架构")`

    **End of Thought.**
    </think>

    抱歉呀，根据现有知识库内容，我无法回答这个问题。……

同一轮的 `usage` 也印证了这一点：`completion_tokens=4734` 里
`reasoning_tokens=3881` —— **一半以上的输出是推理**。所以这既是观感问题，
也是延迟问题；顺手也让 P1 清单里的「思考过程折叠」变得可做（原先判定做不到，
是因为只探测了 `additional_kwargs.reasoning_content`，没料到它会混进 `content`）。

★ 判断规则（只有这一处定义）
1. 文本里出现**开标签** `<think>` / `<thinking>`：把所有
   `开标签 … 闭标签`（未闭合则直到末尾）的区间划为推理，其余为正文。
2. 文本里**只有闭标签**：闭标签之前的全部内容都是推理 ——
   这正是本网关的形状（开标签被它吃掉了）。
3. 什么都没出现：整段都是正文。**绝大多数正常回答走这一条，行为与从前完全一致。**

⚠️ 已知取舍：规则 2 会把「正文里恰好引用了 `</think>` 字样」误判成推理前缀。
这种情况远比上面那个泄漏罕见，且泄漏的代价（用户看到一大段英文独白）严重得多，
所以这样取舍。真要精确，得让网关正确分流 reasoning_content —— 那不在本项目控制内。
"""

from __future__ import annotations

import re

#: 开标签：`<think>` / `<thinking>`
_OPEN = re.compile(r"<think(?:ing)?>", re.IGNORECASE)
#: 闭标签
_CLOSE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)
#: 一对完整的思考块；未闭合的（到文本末尾）也算
_PAIR = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|\Z)", re.IGNORECASE | re.DOTALL)
#: 剥掉块里残留的标签本身（只保留推理文字）
_TAG = re.compile(r"</?think(?:ing)?>", re.IGNORECASE)


def has_reasoning(text: str) -> bool:
    """文本里有没有思考标记（用于「值不值得跑一遍切分」的快速判断）"""
    if not text:
        return False
    low = text.lower()
    return "<think" in low or "</think" in low


def split_think(text: str) -> tuple[str, str]:
    """
    把一段模型输出切成 `(推理正文, 给用户的回答)`。

    纯函数、无状态、对同一输入永远给同一结果 —— 于是「实时那一屏」与
    「重放历史」看到的切分必然一致（和 `done.files` 用同一个派生函数的理由相同）。

    返回的推理正文已去掉标签与首尾空白；没有推理时是空串。
    """
    if not text or not has_reasoning(text):
        return "", text

    if _OPEN.search(text):
        # 规则 1：按块抽取。块外的内容按原顺序拼回，作为正文。
        chunks: list[str] = []
        pos = 0
        reasoning_parts: list[str] = []
        for match in _PAIR.finditer(text):
            chunks.append(text[pos : match.start()])
            reasoning_parts.append(match.group(0))
            pos = match.end()
        chunks.append(text[pos:])
        reasoning = _TAG.sub("", "\n".join(reasoning_parts))
        answer = "".join(chunks)
    else:
        # 规则 2：只有闭标签 —— 闭标签之前全是推理（本网关的形状）
        match = _CLOSE.search(text)
        if match is None:  # 理论上不可达（has_reasoning 已经确认有标记）
            return "", text
        reasoning = text[: match.start()]
        answer = text[match.end() :]

    reasoning = reasoning.strip()
    # 正文开头往往还留着一个换行；不 strip 掉的话渲染首行会多一段空行
    return reasoning, answer.strip()


__all__ = ["split_think", "has_reasoning"]
