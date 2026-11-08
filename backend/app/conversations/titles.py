"""
会话自动标题 —— 首轮结束后用模型把「在吗」变成「报销流程咨询」

★ 为什么要有它

标题默认取首轮用户输入的前 40 字（`models.derive_title`）。首轮若是「在吗」
「你好」这类寒暄，列表里就会出现一串一模一样的条目 —— 用户唯一能认出来的
线索只剩时间。**「不知道怎么找回来」是历史功能真正的失败模式**，
而不是「历史存不住」。

★ 三条硬约束（决定了它是现在这个样子）

1. **不在用户请求路径上**。用户在等第一个字的时候多一次模型调用，
   首字延迟会直接翻倍。所以它跑在**轮次落盘之后**另起的后台任务里。
2. **失败必须无声**。标题生成失败不该影响任何东西 —— 它面对的是一个
   已经有派生标题的会话，最差情况就是「保持首轮那 40 个字」。
3. **不覆盖任何非派生标题**。用户可能在几秒的生成窗口里自己改了名，
   这时模型的结果必须丢掉。判定由 `ConversationStore.apply_auto_title`
   在锁内完成（比对调用方看到的那份标题），所以「检查 + 写入」之间没有窗口。

★ 为什么只看开头几百字

标题只需要「这段对话在聊什么」，而这个信息集中在开头。把整篇长回答送进去
既慢又贵，还会让标题被结尾的细枝末节带偏 —— 摘要是越读越具体的。
"""

from __future__ import annotations

import asyncio
import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)

#: 自动标题的目标长度。比派生标题（`CONVERSATION_TITLE_CHARS`，默认 40）短得多：
#: 侧栏一行放不下 40 个字，而标题的作用是「一眼认出来」而不是「说清楚」。
AUTO_TITLE_CHARS = 16

#: 送给模型的原文上限（用户输入与回答各截这么多字）
MAX_INPUT_CHARS = 600

_PROMPT = (
    "下面是一段对话的开头。请用不超过 {n} 个字概括这段对话在聊什么，作为会话标题。\n"
    "只输出标题本身：不要引号、不要句末标点、不要「标题：」这类前缀、不要任何解释。\n\n"
    "用户：{user}\n"
    "助手：{answer}\n"
)

#: 模型常见的两处多余包装：「标题：」前缀、把整句包在引号里
_PREFIX_RE = re.compile(r"^(标题|题目|topic|title)\s*[:：]\s*", re.IGNORECASE)
_QUOTES = "\"'“”‘’《》「」【】"


def clean_title(raw: str, max_chars: int = AUTO_TITLE_CHARS) -> str:
    """
    把模型输出压成一行干净的标题。

    分四步，每一步都对应模型真会犯的一种毛病：输出多行（只取第一行）、
    带「标题：」前缀、整体被引号包住、句末带标点。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    text = text.splitlines()[0].strip()
    text = _PREFIX_RE.sub("", text)
    text = text.strip(_QUOTES).strip()
    text = text.rstrip("。.！!？?，,；;：:")
    # 压平中间空白：模型偶尔会用空格把一个词拆开
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


async def suggest_title(user_input: str, answer: str) -> str:
    """
    让模型给这段对话起个短标题。

    返回空串表示「没有拿到可用的标题」—— 调用方据此保持原标题不变。
    **所有**异常（网关不通、超时、输出为空、依赖缺失）都在这里被吃掉：
    这是旁路能力里的旁路，它不该有任何对外可见的失败。
    """
    if not (user_input or "").strip():
        return ""
    if not bool(getattr(settings, "CONVERSATION_AUTO_TITLE", True)):
        return ""

    prompt = _PROMPT.format(
        n=AUTO_TITLE_CHARS,
        user=(user_input or "")[:MAX_INPUT_CHARS],
        answer=(answer or "")[:MAX_INPUT_CHARS],
    )

    try:
        from app.core.llm_factory import create_llm

        # streaming=False：这里要的是一次完整输出，不是逐字给用户看。
        # max_retries=0：一个可以不要的标题不值得为重试多等一个超时周期。
        #
        # ⚠️ 刻意**不设** `max_tokens`。本机实测：给它 64 的预算时该网关的模型会把
        # 预算花在（不可见的）思考上，HTTP 200 但正文是空串 —— 于是「有超时兜底、
        # 却永远拿不到标题」，一个静默失效的限制。宁可让它多花十几秒，
        # 也不要一个看起来更「讲究」、实际把功能关掉的参数。
        llm = create_llm(streaming=False, max_retries=0)
        timeout = max(1.0, float(getattr(settings, "CONVERSATION_TITLE_TIMEOUT_SECONDS", 20.0)))
        # 双保险：ChatOpenAI 自己也带 timeout，但 wait_for 覆盖「连上了却不返回」这类
        # 只在网络层发生的情况，保证后台任务一定会退出
        result = await asyncio.wait_for(llm.ainvoke(prompt), timeout=timeout)
    except asyncio.TimeoutError:
        logger.info("自动标题超时，保持原标题")
        return ""
    except Exception as e:  # noqa: BLE001
        logger.info("自动标题生成失败，保持原标题: %s", e)
        return ""

    content = getattr(result, "content", result)
    if isinstance(content, list):
        # 多模态分块格式：只取其中的文本块
        content = "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return clean_title(str(content))


__all__ = ["AUTO_TITLE_CHARS", "clean_title", "suggest_title"]
