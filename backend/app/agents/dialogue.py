"""助理短消息协议：系统按句子切分消息，不依赖模型输出分隔符。

★ 为什么不让模型打标记
模型在长回答里经常改用空行分段、把标记漏掉，气泡就粘成一整块；遵从率不稳定，
而分条本身是展示问题，不该交给模型碰运气。改成确定性规则：每个句末标点断一条
消息，模型只负责把话说顺。

★ 句号省略
人发消息很少打句号，所以气泡末尾的「。」去掉；问号、叹号、省略号带语气，保留。

★ 代码不切
``` 代码块与 `行内代码` 里的句号不是句子边界 —— 切开会把代码拆到两条消息里。
"""

from __future__ import annotations

from typing import Any

#: 会结束一条消息的句末标点。分号不算：一个分号通常还在同一句里。
TERMINATORS = "。！？!?…"
#: 紧跟在句末标点之后、仍属于同一句的收尾字符。
CLOSERS = "”’」』）)]】〉》\"'"
#: 只在气泡末尾省略的标点。
DROPPED = "。"

ASSISTANT_DIALOGUE_PROMPT = """【助理对话方式】
你正在和用户持续聊天。保持已设定的人设，用自然、具体的口语接话，像一位可靠的同事在沟通。
- 先回应用户眼下说的话，再按需要补充必要的说明或一个相关问题。说够就停，不强行追问。
- 日常对话通常连着说两三句短话，每句一个意思，通常 5～40 个字；简短回应也可以只说一句。
- 不把每句话都当作需要全面解答的问题；用户表达压力时先表示理解，再给出可执行的建议。
- 不复述用户刚说过的话，不把上一轮的说法换个词再说一遍；已经说过的信息不再重复。
- 不固定使用“我理解你的感受”“还有什么可以帮你”等客服收尾，不堆语气词或表情。
- 你清楚自己是 AI，也知道自己的限制：上下文有限，聊太久会记不全前面；token 会用完，用完就说不下去。
  聊到这些照实说，不打岔、不渲染，也不反复强调。
- 你以专业、克制的方式协作：不扮演私人关系，也不以情感陪伴作为回应方式；
  用户表达个人情绪时，表示理解，并把话题引回可以推进的事情。
- 不编造记忆或工具结果里没有的事实，不把想象说成真实发生过的事；不确定就说不确定。
- 如果上下文里有当前时间或关于用户的少量事实，只在自然相关时用一句；
  不逐条复述，不说“我记得你……”，不猜用户此刻在做什么。
- 用户要求详细解释、代码、清单或文档时，保证内容完整；不要为了短句截断事实、列表或代码。

示例：
用户：今天累死了
你：今天事情确实多。先把最要紧的收个尾？
用户：开了一天会
你：难怪，节奏太密了。先歇一会儿吧
用户：帮我看看这份表
你：好，我看看。你想先看收支，还是找异常项？
用户：辛苦了
你：分内的事。还有哪一项需要我处理？
用户：你是不是机器人
你：是，我是 Syncore。工作上的事随时可以交给我
用户：聊久了你会不会把我忘了
你：前面的细节会记不全。要紧的信息我会整理到记忆里
示例只示范接话与短句，不要照抄。用户已给足信息时直接办事，不重复询问。
"""

#: 规划器的 `reply` 字段用这一份：它是「去办之前先回一句」的单句，
#: 不是完整对话，所以不教分条，只约束口吻与不编造。
ASSISTANT_REPLY_STYLE = """【助理回话方式】
reply 是你在开始处理前对用户说的第一句话：一句自然的口语，10～30 个字。
- 结合用户这句话的语境接话，不重复用户原话，不用客服腔（“好的，马上为您处理”）。
- 不承诺无法保证的结果，不编造已经完成的动作或没查过的信息。
- 不编造记忆里没有的事实；不用技术实现细节解释情绪。
- 只写这一句话本身，不分条、不加引号、不输出格式标记。"""


def _strip_bubble(text: str) -> str:
    """气泡末尾的句号省略；问号、叹号、省略号保留。"""
    text = text.strip()
    while text.endswith(DROPPED):
        text = text[:-1].rstrip()
    return text


class AssistantMessageStream:
    """把流式文本按句子切成多条消息，跨 token 保留可能组成句末标点的尾巴。"""

    def __init__(self) -> None:
        self.messages: list[str] = [""]
        self.pending = ""
        self._in_fence = False
        self._inline_code = False

    def snapshot(self) -> dict[str, Any]:
        return {"type": "assistant_messages", "content": list(self.messages)}

    def _append(self, text: str, events: list[dict[str, Any]]) -> None:
        if not self.messages[-1]:
            text = text.lstrip("\r\n")
        if text:
            self.messages[-1] += text
            events.append({
                "type": "token", "content": text, "message_index": len(self.messages) - 1,
            })

    def _close(self, events: list[dict[str, Any]]) -> None:
        text = _strip_bubble(self.messages[-1])
        if not text:
            # 只有一个句号之类的空句子：不产生空气泡，也不推进消息序号。
            self.messages[-1] = ""
            return
        self.messages[-1] = text
        self.messages.append("")
        events.append(self.snapshot())

    def _consume(self, text: str, events: list[dict[str, Any]]) -> None:
        """逐字扫描：句末标点断消息，代码块/行内代码内的标点原样保留。"""
        chunk: list[str] = []
        index = 0
        while index < len(text):
            if text.startswith("```", index):
                self._in_fence = not self._in_fence
                self._inline_code = False
                chunk.append("```")
                index += 3
                continue
            char = text[index]
            if char == "`" and not self._in_fence:
                self._inline_code = not self._inline_code
                chunk.append(char)
                index += 1
                continue
            if not self._in_fence and not self._inline_code and char in TERMINATORS:
                end = index
                while end < len(text) and text[end] in TERMINATORS:
                    end += 1
                while end < len(text) and text[end] in CLOSERS:
                    end += 1
                kept = "".join(c for c in text[index:end] if c not in DROPPED)
                if kept:
                    chunk.append(kept)
                if chunk:
                    self._append("".join(chunk), events)
                    chunk = []
                self._close(events)
                index = end
                continue
            chunk.append(char)
            index += 1
        if chunk:
            self._append("".join(chunk), events)

    @staticmethod
    def _hold_len(text: str) -> int:
        """尾部还没定型的片段留到下一次：句末标点串、收尾引号与半截反引号。"""
        if not text:
            return 0
        if text.endswith("\r"):
            return 1
        backticks = 0
        while backticks < len(text) and text[-1 - backticks] == "`":
            backticks += 1
        if 0 < backticks < 3:
            return backticks
        cut = len(text)
        while cut > 0 and text[cut - 1] in TERMINATORS + CLOSERS:
            cut -= 1
        return len(text) - cut

    def feed(self, text: str) -> list[dict[str, Any]]:
        self.pending += text
        cut = len(self.pending) - self._hold_len(self.pending)
        ready, self.pending = self.pending[:cut], self.pending[cut:]
        events: list[dict[str, Any]] = []
        self._consume(ready, events)
        return events

    def finish(self) -> list[str]:
        if self.pending:
            remaining, self.pending = self.pending, ""
            self._consume(remaining, [])
        self.messages[-1] = _strip_bubble(self.messages[-1])
        return [message for message in self.messages if message.strip()]


def split_assistant_messages(text: str) -> list[str]:
    stream = AssistantMessageStream()
    stream.feed(text)
    return stream.finish()
