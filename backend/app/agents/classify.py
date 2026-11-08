"""
意图标签与关键词兜底。

★ 意图不再单独调模型

Router 时代 `_classify` 的输出决定「派给哪个专员」。
Supervisor 时代派发由 `supervisor.py` 的任务拆解决定，意图只剩两个去处：

1. 顶栏文案（`humanize.ts::intentProgress`）与 `intent` SSE 事件；
2. 不变量 12（知识库优先）：direct 模式下 `file` 会先预取检索结果。

既然 Supervisor 每轮本来就要读一遍用户输入，意图就并进它那一次规划调用
（`SupervisorPlan.intent`），不再单独烧一次 LLM 请求。

★ 为什么关键词兜底还留着

规划三层全挂时 `_plan` 会退成 direct + 关键词意图：用户至少还能得到一个回答，
顶栏也还能显示一句像样的进度文案，而不是空值。
所以这里只保留标签定义与**模型完全不可用时**的规则兜底。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

IntentLabel = Literal["pc", "file", "software", "browser", "general"]

VALID_INTENTS = {"pc", "file", "software", "browser", "general"}


class IntentResult(BaseModel):
    """关键词兜底的输出"""

    intent: IntentLabel = Field(description="识别出的意图类别")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="置信度 0-1")


_KEYWORDS: dict[str, list[str]] = {
    "software": ["软件", "安装", "卸载", "工具", "更新"],
    "pc": ["系统", "进程", "内存占用", "磁盘", "cpu", "开机", "卡顿", "服务", "命令"],
    "browser": ["搜索", "搜一下", "网页", "网站", "上网", "联网", "http"],
    "file": [
        "文件", "目录", "文件夹", "重命名", "file", "folder",
        "删掉", "删除", "新建", "写入", "读取", "保存到",
        "文档", "资料", "知识库", "代码", "脚本",
    ],
}


def keyword_fallback(user_input: str) -> IntentResult:
    """
    关键词规则兜底，命中多个时按优先级取第一个。

    只有在模型完全不可用时才会走到这里，所以宁可粗糙也要保证有值。
    默认返回 file —— 文件管家带知识库检索，是最安全的默认值
    （它会先检索知识库，检索不到再靠模型自身知识回答）。
    """
    lowered = user_input.lower()
    for intent, words in _KEYWORDS.items():
        if any(w in lowered for w in words):
            return IntentResult(intent=intent, confidence=0.4)  # type: ignore[arg-type]
    return IntentResult(intent="file", confidence=0.3)


__all__ = [
    "IntentLabel",
    "IntentResult",
    "VALID_INTENTS",
    "keyword_fallback",
]
