"""
长期记忆工具 —— 让助手能在对话里查、记、忘

★ 为什么 `memory_list` 的过滤是「关键词」而不是「搜索」
记忆存储是一份 key→item 的 JSON（`app/memory/store.py`），没有向量索引。
所以这里的过滤就是子串匹配。这件事必须写在返回文案里，因为模型会拿它下结论：
文案只说「没找到」，模型就会对用户说「你没提过这个偏好」——
而真相可能是那条偏好写成了另一种措辞。**「没搜到」与「没有」不是一回事**，
在这个功能上尤其贵（用户会因此重新交代一遍，或者怀疑系统记性差）。

★ 三个工具的权限差别
`memory_list` 对所有专员开放（读偏好不会造成伤害，各专员都需要它来「像同一个人」）；
`memory_remember` / `memory_forget` 只给主对话（`allowed_agents=["supervisor"]`）。
理由不是写入更危险，而是**写入需要一个明确的归属**：
后台子任务的一条中间结论如果被当成用户偏好写进去，
用户之后每次对话都会被这条他从未说过的东西带偏，而设置页里它的来源写着「手动添加」。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tools.decorator import agent_tool
from app.agents.tools.context import ToolContext
from app.config import settings
from app.memory import get_manager
from app.memory.models import CATEGORIES
from app.memory.retention import memory_state, retention

logger = logging.getLogger(__name__)

#: 一次最多列出的条数（记忆会随时间增长，全量倒进上下文会挤掉检索内容）
MAX_LIST_ITEMS = 20


def _health() -> bool:
    """工具健康检查：记忆存储可读即为健康"""
    try:
        get_manager().store.stats()
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("记忆存储健康检查失败: %s", e)
        return False


def _line(item: Any, *, show_inactive: bool = False) -> str:
    text = f"- [{item.key}] {item.value}｜{item.source_label}｜置信度 {item.confidence:.2f}"
    if not item.active and show_inactive:
        text += f"｜已失效（{item.inactive_reason or '被后续记忆否定'}）"
    elif item.active:
        text += f"｜记忆强度 {retention(item):.0%}"
        if memory_state(item) == "dormant":
            text += "｜较久未确认的旧记录，请先确认是否仍适用"
    return text


@agent_tool(
    display_name="查看长期记忆",
    description=(
        "列出用户的长期偏好（写作风格、称呼、常用格式等）。"
        "用户问「你记得我什么」「我说过要用什么格式吗」，或回答前需要确认既有偏好时调用。"
    ),
    category="记忆",
    danger_level="safe",
    read_only=True,
    health_handler=_health,
)
def memory_list(query: str = "", include_inactive: bool = False, ctx: ToolContext | None = None) -> str:
    """列出长期偏好。

    Args:
        query: 关键词过滤（子串匹配，匹配内容与分类）；留空列出全部
        include_inactive: 是否连已失效的旧偏好一起列出
    """
    if not settings.MEMORY_ENABLED:
        return "（长期记忆功能当前在设置里是关闭的，所以现在没有任何已存偏好。）"
    try:
        items = get_manager().list_items()
        if ctx and ctx.employee_id:
            from app.workspace.scoped_memory import scoped_memory
            common = [i for i in items if i.active and i.category in {"output_format", "language", "tone", "length", "tooling"}]
            rows = scoped_memory.list(ctx.employee_id)
            lines = [f"- [{r['scope']}:{r['key']}] {r['value']}（依据：{r['evidence']}）" for r in rows
                     if not query or query.lower() in (r['key'] + r['value']).lower()]
            lines += [_line(i) for i in common if not query or query.lower() in (i.key + i.value).lower()]
            return "\n".join(lines[:MAX_LIST_ITEMS]) or "没有找到当前项目或本人范围内的记忆。"
    except Exception as e:  # noqa: BLE001
        # ★ 说「查不到」而不是返回空列表：空列表会被理解成「没有偏好」，而真相是读挂了
        return f"（暂时读不到长期记忆：{e}）"

    if not items:
        return "长期记忆里还没有内容（助手还没学到任何偏好）。"

    keyword = (query or "").strip().lower()
    if keyword:
        items = [
            i for i in items
            if keyword in i.value.lower()
            or keyword in i.key.lower()
            or keyword in (CATEGORIES.get(i.category, "") + i.category).lower()
        ]
    if not include_inactive:
        items = [i for i in items if i.active]

    total = len(items)
    if total == 0:
        return (
            f"按关键词「{query}」没有匹配到偏好。\n"
            "注意这是**子串匹配**，不是语义搜索：换个措辞写的同一条偏好可能没被匹配上。"
            "所以请说「没找到相关的记录」，不要据此断定用户从没提过这件事；"
            "需要确认时可以用 memory_list 留空关键词再看一遍全部。"
        )

    shown = items[:MAX_LIST_ITEMS]
    lines = [f"长期偏好共 {total} 条{'（含已失效）' if include_inactive else ''}："]
    lines.extend(_line(i, show_inactive=include_inactive) for i in shown)
    if total > len(shown):
        lines.append(f"…其余 {total - len(shown)} 条未显示，可用 query 关键词缩小范围。")
    if keyword:
        lines.append("（以上是子串匹配的结果，不是语义搜索；漏掉的可能是措辞不同。）")
    return "\n".join(lines)


@agent_tool(
    display_name="记住偏好",
    description=(
        "把用户明确说过的偏好记进长期记忆，下次对话会默认遵循。"
        "只在用户**亲口**表达偏好时使用（「以后都…」「记住…」「我习惯…」），不要替用户猜测。"
    ),
    category="记忆",
    danger_level="moderate",
    allowed_agents=["supervisor"],
    health_handler=_health,
)
def memory_remember(
    value: str,
    evidence: str = "",
    category: str = "",
    key: str = "",
) -> str:
    """记下一条用户偏好。

    Args:
        value: 要记住的内容，一句话，用用户自己的说法（例：「周报用 Markdown，不要太长」）
        evidence: 用户的原话，用于日后追溯与人工核对；留空则标记为手动添加
        category: 分类，可选 output_format/language/tone/length/workflow/tooling/schedule/identity/other；留空自动判断
        key: 去重键；留空则用分类名。**同类偏好想并存时必须给一个不同的 key**
    """
    if not settings.MEMORY_ENABLED:
        return "（长期记忆功能在设置里是关闭的，这条没有记进去。需要的话请到设置页打开后我再记。）"
    if not (value or "").strip():
        return "value 不能为空 —— 要记的偏好得是一句具体的话，比如「报告用 Markdown」。"
    try:
        result = get_manager().add_manual(value, key=key, category=category, evidence=evidence)
    except Exception as e:  # noqa: BLE001
        return f"（写入长期记忆失败：{e}）"
    if result is None:
        return "这条内容太短或全是空白，没有写入。"

    item = result.item
    if result.action == "unchanged":
        return f"已经记着了，内容一模一样：{item.value}"
    if result.action == "protected":
        return (
            f"没有覆盖 [{item.key}]：那条是用户亲手设置的，"
            "自动/对话写入不允许悄悄改掉（需要改动请先跟用户确认，或在设置页修改）。"
        )
    verb = "记住了" if result.action == "created" else "更新了"
    return f"{verb}：{item.value}（key={item.key}，分类={CATEGORIES.get(item.category, item.category)}）"


@agent_tool(
    display_name="忘记偏好",
    description=(
        "让一条长期偏好失效（用户说「别再按那个来了」「忘掉那条」时）。"
        "key 用 memory_list 拿；这是软删除，可以在设置页的记忆面板里恢复。"
    ),
    category="记忆",
    danger_level="moderate",
    allowed_agents=["supervisor"],
    health_handler=_health,
)
def memory_forget(key: str, reason: str = "用户要求忘记") -> str:
    """让一条偏好失效。

    Args:
        key: 要失效的偏好键（形如 output_format），必须是 memory_list 里出现过的
        reason: 为什么失效，会存进记录里供用户查看
    """
    if not (key or "").strip():
        return "需要指定 key。先用 memory_list 看清是哪一条，再决定忘掉它。"
    try:
        status = get_manager().forget_by_user(key, reason=reason)
    except Exception as e:  # noqa: BLE001
        return f"（操作长期记忆失败：{e}）"

    if status == "not_found":
        return (
            f"记忆里没有 [{key}] 这条。"
            "用 memory_list 确认一下实际的 key（可能是措辞不同，不是没有记过）。"
        )
    if status == "already_inactive":
        return f"[{key}] 已经是失效状态了，不用再处理。"
    if status == "disabled":
        return "（长期记忆功能在设置里是关闭的，这次没有改动。）"
    if status == "failed":
        return f"[{key}] 没能改成失效（存储写入失败），请先到设置页确认。"
    return f"已让 [{key}] 失效，之后不再遵循；设置页的记忆面板里还能看到它，也可以恢复。"


__all__ = ["memory_forget", "memory_list", "memory_remember"]
