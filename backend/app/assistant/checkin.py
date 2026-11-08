"""
助理主动问候 —— 用户打开助理会话时，助手可以先开口一句

★ 为什么是「打开时检查」而不是后台定时推送

触发点放在「用户打开会话」这一下，符合「用户打开会话，才说一句」。
已绑定微信且有回复上下文时，同一条问候也会同步到微信；不另起定时问候。

★ 为什么门槛这么多

主动开口是**打扰**，不是功能展示。所以每一条都只紧不松：
- 必须在人设里显式打开（`proactive="light"`），默认之外没有别的档位；
- 静默时段（默认 22:00–08:00）绝不出声；
- 上一次必须是用户先开口、且已经过去足够久（默认 12 小时）；
- 每天最多几次由人设卡里的「每日上限」决定（默认 1 次）；同一会话两次尝试之间至少隔 5 分钟（防前端重连连刷）；
- 没有历史的新会话不主动开口 —— 欢迎语由界面负责，不需要模型再说一遍。

★ 为什么只有一次模型调用

这条路径在用户「刚打开页面」时执行，等待体验比内容质量更敏感：
一次调用、8 秒超时、120 token 上限。失败就静默返回不可用，
绝不重试 —— 主动问候失败的正确表现是「这次没有开口」。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.config import settings
from app.conversations import (
    ASSISTANT_MAIN_THREAD_ID,
    INITIATED_BY_ASSISTANT,
    INITIATED_BY_USER,
    conversation_store,
)
from app.conversations.models import STATUS_OK, ConversationTurn, now_iso
from app.persona import get_persona_manager
from app.persona.models import PersonaProfile
from app.persona.timeutil import is_quiet_hours, local_now

logger = logging.getLogger(__name__)

CHECKIN_PROMPT = """【主动开口】
用户刚打开和你的会话。你可以自然地先说一句，点明当前可以协助的事项。
- 只输出**一条**消息正文，5～40 个字；不分条，不加引号，不写“你：”。
- 可以顺着最近聊过的事接一句（如果上面的信息里有自然相关的内容），
  但不要逐条复述，也不要问“你还记得吗”。
- 不催促、不追问、不制造焦虑；不说“检测到你回来了”“好久没聊了”。
- 不提到自己是按计划或定时发消息。
- 如果最近没有自然的话题，就简单打个招呼，别硬找话。
"""

#: 进程内冷却：前端重连、切页、双击都会再次请求；没有它就会连续打模型。
#: 只记「尝试过」，不记「成功」—— 模型失败时同样不该被立刻重试。
_last_attempt: Dict[str, float] = {}
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _compact(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def _recent_dialogue(turns: List[ConversationTurn]) -> str:
    """最近两轮压成几行，只用于「接得上话」，不做完整上下文"""
    lines: List[str] = []
    for turn in turns[-2:]:
        if turn.user_input:
            lines.append(f"用户：{_compact(turn.user_input, 120)}")
        if turn.answer:
            lines.append(f"你：{_compact(turn.answer, 160)}")
    if not lines:
        return ""
    return "【最近的对话】\n" + "\n".join(lines)


def _eligibility(profile: PersonaProfile, now: datetime) -> tuple[bool, str]:
    """
    是否该主动开口 + 不合适的原因（原因只用于日志与接口回执，不展示给用户）。
    """
    if not settings.ASSISTANT_PROACTIVE_ENABLED:
        return False, "disabled"
    if not profile.enabled:
        return False, "persona_off"
    if profile.proactive != "light":
        return False, "proactive_off"
    if is_quiet_hours(now, profile.quiet_start, profile.quiet_end):
        return False, "quiet_hours"

    loaded = conversation_store.get_conversation(ASSISTANT_MAIN_THREAD_ID)
    if loaded is None:
        return False, "no_history"
    _, turns = loaded
    if not turns:
        return False, "no_history"

    last = turns[-1]
    if last.initiated_by != INITIATED_BY_USER:
        # 上一条已经是助手说的：用户还没回应，再开口就是自说自话
        return False, "assistant_spoke_last"
    if not (last.answer or "").strip():
        return False, "no_answer"

    last_at = _parse_time(last.created_at)
    if last_at is not None:
        if now - last_at < timedelta(hours=profile.proactive_min_gap_hours):
            return False, "too_soon"
    else:
        # 时间戳坏掉时按「刚聊过」处理：宁可不出声，也不在刚聊完时打扰
        return False, "bad_timestamp"

    today = now.date()
    today_count = 0
    for turn in turns:
        if turn.initiated_by != INITIATED_BY_ASSISTANT:
            continue
        created = _parse_time(turn.created_at)
        if created is not None and created.date() == today:
            today_count += 1
    if today_count >= profile.proactive_daily_max:
        return False, "daily_limit"

    return True, ""


def _clean_reply(raw: str) -> str:
    """把模型输出收成一条短消息：去推理、只取第一句、去引号与「你：」前缀"""
    from app.agents.dialogue import split_assistant_messages
    from app.agents.think import split_think

    _reasoning, text = split_think(str(raw or ""))
    messages = split_assistant_messages(text)
    text = messages[0] if messages else ""
    text = " ".join(text.split()).strip().strip("\"'“”‘’「」『』")
    for colon in ("：", ":"):
        if colon in text[:8]:
            prefix, rest = text.split(colon, 1)
            if 0 < len(prefix) <= 6 and rest.strip():
                text = rest.strip()
            break
    return text[:80]


async def _generate(turns: List[ConversationTurn]) -> str:
    """一次短调用生成问候；任何失败都返回空串（调用方静默跳过）"""
    from app.core.llm_factory import create_llm

    stable, dynamic = get_persona_manager().build_prompt_parts(
        ASSISTANT_MAIN_THREAD_ID, for_checkin=True
    )
    prompt = "\n\n".join(
        part
        for part in (stable, dynamic, _recent_dialogue(turns), CHECKIN_PROMPT)
        if part
    )
    try:
        llm = create_llm(
            streaming=False,
            max_tokens=settings.ASSISTANT_PROACTIVE_MAX_TOKENS,
            max_retries=0,
            timeout=settings.ASSISTANT_PROACTIVE_TIMEOUT_SECONDS,
        )
        message = await asyncio.wait_for(
            llm.ainvoke(prompt),
            timeout=settings.ASSISTANT_PROACTIVE_TIMEOUT_SECONDS,
        )
        content = message.content if isinstance(message.content, str) else str(message.content)
    except asyncio.TimeoutError:
        logger.info("主动问候生成超时（本次不发声）")
        return ""
    except Exception as e:  # noqa: BLE001
        logger.info("主动问候生成失败（本次不发声）: %s", e)
        return ""
    return _clean_reply(content)


async def maybe_checkin() -> Dict[str, Any]:
    """
    尝试生成并落盘一条主动问候。

    返回值是给前端的回执：`ok=True` 时带 `text` / `turn_id` / `created_at`；
    否则带 `reason`（quiet_hours / too_soon / daily_limit / cooldown …），
    前端不需要展示原因，只需要知道「这次没有说话」。
    """
    async with _get_lock():
        now = local_now()
        profile = get_persona_manager().get_profile()
        eligible, reason = _eligibility(profile, now)
        if not eligible:
            return {"ok": False, "reason": reason}

        last = _last_attempt.get(ASSISTANT_MAIN_THREAD_ID, 0.0)
        if time.monotonic() - last < settings.ASSISTANT_PROACTIVE_COOLDOWN_SECONDS:
            return {"ok": False, "reason": "cooldown"}
        _last_attempt[ASSISTANT_MAIN_THREAD_ID] = time.monotonic()

        loaded = conversation_store.get_conversation(ASSISTANT_MAIN_THREAD_ID)
        turns = loaded[1] if loaded else []
        text = await _generate(turns)
        if not text:
            return {"ok": False, "reason": "generation_failed"}

        # 生成期间用户可能已经又发了一轮（这把锁只拦并发的 checkin，拦不住主链路写入）。
        # 此时再追加快照，问候就会落在用户新消息之后、变成答非所问的插话 —— 整条丢弃。
        after = conversation_store.get_conversation(ASSISTANT_MAIN_THREAD_ID)
        after_turns = after[1] if after else []
        if (after_turns[-1].turn_id if after_turns else "") != (turns[-1].turn_id if turns else ""):
            logger.info("主动问候生成期间对话已变化（本次不发声）")
            return {"ok": False, "reason": "interrupted"}

        turn_id = uuid.uuid4().hex[:16]
        summary = conversation_store.append_turn(
            ASSISTANT_MAIN_THREAD_ID,
            user_input="",
            answer=text,
            # token 事件会在落盘前被剔除（answer 已能还原内容），
            # 这里保留它是为了让「实时」与「重放」共用同一种事件形状。
            events=[{"type": "token", "content": text}],
            status=STATUS_OK,
            turn_id=turn_id,
            initiated_by=INITIATED_BY_ASSISTANT,
        )
        if summary is None:
            logger.info("主动问候已生成但落盘失败（下次打开时不展示）")
            return {"ok": False, "reason": "persist_failed"}

        try:
            from app.channels.wechat_worker import wechat_channel

            wechat_channel.mirror_turn(ASSISTANT_MAIN_THREAD_ID, text)
        except Exception as exc:  # noqa: BLE001 - 微信不可用仍照常展示桌面问候
            logger.warning("助理问候同步到微信失败: %s", exc)

        logger.info("助理主动问候已发送: %s 字", len(text))
        return {
            "ok": True,
            "reason": "",
            "text": text,
            "turn_id": turn_id,
            "created_at": summary.updated_at or now_iso(),
            "initiated_by": INITIATED_BY_ASSISTANT,
        }


__all__ = ["CHECKIN_PROMPT", "maybe_checkin"]
