"""
微信通道 worker：微信收信进入助理主链路，电脑端助理问答通过后台任务同步回微信。

★ 为什么单独一个模块
`wechat_ilink` 只讲协议，这里讲「一条微信消息怎么变成 `assistant_main` 的一轮对话」。
收发与同步都集中在这里，协议细节不会渗进 Agent，Agent 也不会渗进协议层。

★ 单例、可重启
扫码成功后 `api/channels.py` 调 `restart()`，新 token 立刻生效，不必重启后端。
取消旧任务最多丢掉一次已经挂住的长轮询（网关 40 秒后自然返回），不会重复回信。

★ 顺序：先落游标再回答
游标由 `_poll` 在分发消息前落盘，理由见 `wechat_ilink.save_cursor`。代价是
极端情况下可能漏答一条，换来的是进程重启后不会把旧消息逐条重答一遍。

★ 回答失败不静默
用户在微信那头只看得到「有没有回话」，看不到后端日志。所以拿不到回答时也要
回一句人话，而不是让这次提问石沉大海。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set

from app.channels import wechat_ilink

logger = logging.getLogger(__name__)

#: 进程内去重窗口。游标正常工作时消息不会重放；这层挡的是「落盘失败/重启」这类
#: 边界情况，以及网关偶发重复投递。只存 message_id，一千条的内存可以忽略。
_SEEN_MAX = 1000

#: 收信连续失败后的退避上限（秒）。网关重启几分钟是常态，别每 2 秒刷一遍日志。
_BACKOFF_MAX = 30


def _mask(value: str) -> str:
    """日志里只说「谁发的」，不整段抄用户的 id"""
    return f"{value[:6]}…(len={len(value)})" if len(value) > 6 else value


class WechatChannel:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._last_error = ""
        self._last_message_at = 0.0
        self._started_at = 0.0
        self._seen: Set[str] = set()
        self._seen_order: Deque[str] = deque()
        self._send_lock = asyncio.Lock()
        self._outbound_tasks: Set[asyncio.Task] = set()
        self._last_sync_error = ""

    # ---------- 生命周期 ----------

    def status(self) -> Dict[str, Any]:
        """给 `GET /channels` 用：只读内存，不碰网络"""
        running = self._task is not None and not self._task.done()
        return {
            "running": running,
            "started_at": int(self._started_at) if running else 0,
            "last_message_at": int(self._last_message_at) if self._last_message_at else 0,
            "last_error": self._last_sync_error or self._last_error,
        }

    async def start(self) -> bool:
        """有凭据就起一条长轮询。返回 False 表示「还没绑定」，不是错误"""
        async with self._lock:
            if self._task is not None and not self._task.done():
                return True
            account = wechat_ilink.load_account()
            if not str(account.get("bot_token") or "").strip():
                self._last_error = ""
                return False
            self._task = asyncio.create_task(
                self._poll(account), name="xjms-wechat-channel"
            )
            self._started_at = time.time()
            logger.info("✓ 微信通道已启动: %s", account.get("ilink_bot_id") or "未知 bot")
            return True

    async def stop(self) -> None:
        async with self._lock:
            task, self._task = self._task, None
            pending = set(self._outbound_tasks)
            if task is not None:
                pending.add(task)
        if not pending:
            return
        for pending_task in pending:
            pending_task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        logger.info("微信通道已停止")

    async def restart(self) -> bool:
        """换绑 / 重新登录后调用：用磁盘上的新凭据重建长轮询"""
        await self.stop()
        return await self.start()

    # ---------- 电脑端 → 微信 ----------

    def mirror_turn(
        self, thread_id: str, answer: str,
        *, reply_messages: Optional[List[str]] = None,
    ) -> Optional[asyncio.Task]:
        """只转发桌面助理的回答。后台发送，微信慢或失败不阻塞桌面的事件流。"""
        from app.agents.dialogue import split_assistant_messages
        from app.conversations.store import ASSISTANT_MAIN_THREAD_ID

        if thread_id != ASSISTANT_MAIN_THREAD_ID:
            return None
        account = wechat_ilink.load_account()
        if not account.get("bot_token"):
            return None
        context = account.get("reply_context") or {}
        owner = str(account.get("ilink_user_id") or "")
        if (
            not context.get("user_id")
            or not context.get("context_token")
            or (owner and owner != context["user_id"])
        ):
            self._last_sync_error = "微信同步尚未就绪，请先在微信给助理发一条消息"
            return None

        messages = []
        replies = reply_messages if reply_messages is not None else split_assistant_messages(answer)
        for reply in replies:
            messages.extend(wechat_ilink.split_reply(reply))
        if not messages:
            return None
        task = asyncio.create_task(
            self._deliver_mirror(account, messages), name="xjms-wechat-mirror"
        )
        self._outbound_tasks.add(task)
        task.add_done_callback(self._outbound_tasks.discard)
        return task

    async def _deliver_mirror(self, account: Dict[str, Any], messages: List[str]) -> None:
        try:
            # 整轮串行，避免两轮长回答的分片互相穿插；不占收信/生命周期锁。
            async with self._send_lock:
                for text in messages:
                    current = wechat_ilink.load_account()
                    if current.get("bot_token") != account.get("bot_token"):
                        return  # 排队期间换绑，旧对话不发给新账号。
                    context = current.get("reply_context") or {}
                    if context.get("user_id") != account["reply_context"]["user_id"]:
                        return
                    await wechat_ilink.send_text(
                        str(current["bot_token"]),
                        str(current.get("baseurl") or wechat_ilink.BASE_URL),
                        str(context["user_id"]),
                        text,
                        context.get("context_token"),
                    )
            self._last_sync_error = ""
        except Exception as exc:  # noqa: BLE001 - 同步失败不能让电脑端问答报错
            self._last_sync_error = "电脑端对话同步到微信失败，请在微信发一条消息后重试"
            logger.warning("电脑端对话同步到微信失败: %s", exc)

    # ---------- 收信 ----------

    async def _poll(self, account: Dict[str, Any]) -> None:
        token = str(account["bot_token"])
        base = str(account.get("baseurl") or wechat_ilink.BASE_URL)
        cursor = str(account.get("get_updates_buf") or "")

        try:
            await wechat_ilink.notify_start(token, base)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 探针实测失败也不影响收发
            logger.info("微信 notifystart 失败（不影响收消息）: %s", exc)

        failures = 0
        while True:
            started = time.monotonic()
            try:
                response = await wechat_ilink.get_updates(token, cursor, base)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络抖动按退避重试
                failures += 1
                self._last_error = str(exc)[:300]
                delay = min(2 * failures, _BACKOFF_MAX)
                logger.warning("微信收消息失败（%ss 后重试）: %s", delay, exc)
                await asyncio.sleep(delay)
                continue

            failures = 0
            self._last_error = ""

            new_cursor = str(response.get("get_updates_buf") or "")
            if new_cursor and new_cursor != cursor:
                cursor = new_cursor
                try:
                    await asyncio.to_thread(wechat_ilink.save_cursor, token, cursor)
                except Exception as exc:  # noqa: BLE001 - 游标写不进去也要先把消息答了
                    logger.warning("微信收信游标落盘失败（本轮消息照常处理）: %s", exc)

            messages = response.get("msgs") or []
            for message in messages:
                try:
                    await self._handle(message, token, base)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 一条消息失败不该拖垮整条通道
                    self._last_error = f"处理消息失败: {exc}"[:300]
                    logger.warning("微信消息处理失败: %s", exc, exc_info=True)

            # 网关正常会挂满长轮询；若它瞬时空返回，这里补一个停顿，避免空转打满网关
            if not messages and time.monotonic() - started < 1.0:
                await asyncio.sleep(1.0)

    def _remember(self, message_id: str) -> bool:
        """True = 第一次见到。只保留最近 `_SEEN_MAX` 条"""
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        self._seen_order.append(message_id)
        while len(self._seen_order) > _SEEN_MAX:
            self._seen.discard(self._seen_order.popleft())
        return True

    async def _handle(self, message: Dict[str, Any], token: str, base: str) -> None:
        if int(message.get("message_type") or 0) != wechat_ilink.MSG_TYPE_USER:
            return

        message_id = str(message.get("message_id") or message.get("seq") or "")
        if not message_id or not self._remember(message_id):
            return

        sender = str(message.get("from_user_id") or "").strip()
        if not sender:
            return

        try:
            saved = await asyncio.to_thread(
                wechat_ilink.save_reply_context,
                token, sender, str(message.get("context_token") or ""),
            )
            if saved:
                self._last_sync_error = ""
        except Exception as exc:  # noqa: BLE001 - 上下文存盘失败仍照常回答本条微信
            logger.warning("微信回复上下文保存失败: %s", exc)

        text = wechat_ilink.extract_text(message).strip()
        if not text:
            # 图片 / 语音 / 表情目前没有可读正文。静默跳过，不猜内容也不回错误，
            # 免得每发一张图就多一条「看不懂」的刷屏消息。
            logger.info("微信消息不含文字，跳过（sender=%s）", _mask(sender))
            return

        self._last_message_at = time.time()
        logger.info("微信收到消息: sender=%s chars=%s", _mask(sender), len(text))

        answer = await self._answer(text)
        if not answer:
            answer = "这次没答上来，稍后再发一遍试试。"

        for chunk in wechat_ilink.split_reply(answer):
            await wechat_ilink.send_text(
                token, base, sender, chunk, message.get("context_token")
            )
        logger.info("微信已回复: sender=%s chars=%s", _mask(sender), len(answer))

    # ---------- 问答 ----------

    async def _answer(self, text: str) -> str:
        """走与桌面端助理完全相同的主链路（同一个图、同一套工具、同一份人设）"""
        from app.agents.graph import get_agent_event_stream
        from app.api.chat import get_enabled_graph
        from app.conversations.store import ASSISTANT_MAIN_THREAD_ID

        graph = get_enabled_graph()
        if graph is None:
            self._last_error = "多 Agent 图未初始化"
            logger.warning("微信收到消息，但多 Agent 图未初始化，无法回答")
            return ""

        parts: List[str] = []
        clean = ""
        failure = ""
        async for event in get_agent_event_stream(
            user_input=text,
            thread_id=ASSISTANT_MAIN_THREAD_ID,
            graph=graph,
            web=True,
            use_graph=True,
        ):
            kind = event.get("type")
            if kind == "token":
                parts.append(str(event.get("content") or ""))
            elif kind == "error":
                failure = str(event.get("content") or "").strip()
            elif kind == "done":
                # `done.answer` 是后端切掉推理后的干净正文（有推理时才会带）
                clean = str(event.get("answer") or "").strip()

        answer = clean or "".join(parts).strip()
        if not answer and failure:
            self._last_error = f"回答失败: {failure}"[:300]
            logger.warning("微信回答失败: %s", failure)
        return answer


#: 进程级单例（与 `app.schedules.scheduler` 同一种组织方式）
wechat_channel = WechatChannel()


__all__ = ["WechatChannel", "wechat_channel"]
