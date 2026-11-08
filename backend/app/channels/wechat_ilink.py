"""
微信 iLink（ClawBot）协议层

登录三步：`get_bot_qrcode` 拿一个二维码链接 → 手机微信扫 → `get_qrcode_status`
长轮询到 `confirmed`，把 `bot_token` 落进账号文件。收发信是另外两条：
`getupdates` 长轮询取消息、`sendmessage` 回消息。

这里**只讲协议**，不 import 任何 Agent 代码。把消息接到助理主链路、把回答送回去，
是 `app/channels/wechat_worker.py` 的事 —— 协议层能脱离后端单独测，
而 worker 不需要知道网关的请求长什么样。

★ 为什么和 scripts/wechat_probe.py 长得很像
那个脚本是「协议到底怎么说」的现场取证工具，得能脱离后端单跑；这里是产品路径。
两份同源于 2026-10 扒的 @ilink/clawbot npm 包，改协议记得两边一起改。

★ 这里出现的都是敏感值
`qrcode_img_content` 是一条**登录链接**（扫它=授权），`bot_token` 是长期凭据，
收发的消息正文是用户的聊天内容。所以模块只把链接交给调用方画成二维码，
自己不打日志；账号文件是含 token 的，只落在 `CHANNELS_WECHAT_DIR` 下。
"""

from __future__ import annotations

import base64
import io
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
ILINK_APP_ID = "bot"
# 协议里写死的客户端版本（2.4.10 拆成 16 位段）：网关按它决定下发什么能力
ILINK_APP_CLIENT_VERSION = (2 << 16) | (4 << 8) | 10
CHANNEL_VERSION = "2.4.10"
BOT_AGENT = "XiaojiuSecret/1.0"

QR_LONG_POLL_S = 35
# 二维码有效期是网关说了算，本地只拿来定超时（探针实测 5 分钟内有效）
LOGIN_TIMEOUT_S = 300

#: `getupdates` 在网关侧也是长轮询（最多挂 40s），超时要盖住它
GETUPDATES_LONG_POLL_S = 40

# 协议里的消息形状（与探针同一份口径）
MSG_TYPE_USER = 1       # 用户发来的
MSG_TYPE_BOT = 2        # 机器人发的（回信时用）
MSG_ITEM_TEXT = 1
MSG_STATE_FINISH = 2

#: 单条微信消息的正文上限。长回答拆成几条发，避免被网关按长度拒掉。
#: 取值低于常见 IM 的 2000 字上限，同时保证一条消息放得下一段完整段落。
REPLY_CHUNK_CHARS = 1800

#: 账号文件的读改写要串行：worker 在存游标，扫码登录在存新 token，
#: 两边同时「读-改-写」会丢掉对方那次更新（游标丢了会重放消息，token 丢了要重扫）。
_ACCOUNT_LOCK = threading.RLock()


@dataclass
class LoginTicket:
    """一次登录尝试的入口：`qrcode` 是轮询用的令牌，`qr_url` 是画进二维码的链接"""

    qrcode: str
    qr_url: str
    started_at: int


def _random_uin() -> str:
    uint32 = int.from_bytes(secrets.token_bytes(4), "big")
    return base64.b64encode(str(uint32).encode()).decode()


def _headers(token: Optional[str] = None) -> Dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        "X-WECHAT-UIN": _random_uin(),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _base_info() -> Dict[str, str]:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def _unwrap(endpoint: str, resp: httpx.Response) -> Dict[str, Any]:
    if resp.status_code != 200:
        raise RuntimeError(f"{endpoint} HTTP {resp.status_code}: {resp.text[:300]}")
    parsed = resp.json()
    if parsed.get("ret", 0) != 0:
        raise RuntimeError(f"{endpoint} ret={parsed.get('ret')} errmsg={parsed.get('errmsg')}")
    return parsed


def start_login() -> LoginTicket:
    """申请一张登录二维码。已绑定过会把现有 token 一起报上去，微信那头可能直接免扫"""
    existing = load_account().get("bot_token")
    local_tokens: List[str] = [str(existing)] if existing else []
    body = {"local_token_list": local_tokens, "base_info": _base_info()}
    url = f"{BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}"
    with httpx.Client() as client:
        resp = client.post(url, headers=_headers(), json=body, timeout=15.0)
    parsed = _unwrap("get_bot_qrcode", resp)
    qr_url = str(parsed.get("qrcode_img_content") or "").strip()
    qrcode = str(parsed.get("qrcode") or "").strip()
    if not qr_url or not qrcode:
        raise RuntimeError(f"get_bot_qrcode 没给回二维码：{str(parsed)[:300]}")
    return LoginTicket(qrcode=qrcode, qr_url=qr_url, started_at=int(time.time()))


def poll_login(qrcode: str, verify_code: Optional[str] = None, base: str = BASE_URL) -> Dict[str, Any]:
    """问一次扫码结果。这一步在网关侧是长轮询（最多挂 35s），所以超时给到 40s"""
    ep = f"{base.rstrip('/')}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
    if verify_code:
        ep += f"&verify_code={verify_code}"
    headers = {k: v for k, v in _headers().items() if k not in ("Content-Type", "AuthorizationType")}
    with httpx.Client() as client:
        resp = client.get(ep, headers=headers, timeout=QR_LONG_POLL_S + 5)
    return _unwrap("get_qrcode_status", resp)


def load_account() -> Dict[str, Any]:
    with _ACCOUNT_LOCK:
        path = settings.wechat_account_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


def _write_account(account: Dict[str, Any]) -> None:
    """先写临时文件再替换：写一半被杀不能留下半份 JSON（那等于凭据丢了）"""
    path = settings.wechat_account_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_account(status: Dict[str, Any], fallback_base: str) -> Dict[str, Any]:
    """把 `confirmed` 那一包写进账号文件 —— 路径与探针共用，扫完界面上的「已绑定」才会翻过来"""
    token = str(status.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("confirmed 但没拿到 bot_token")
    with _ACCOUNT_LOCK:
        previous = load_account()
        account = {
            "bot_token": token,
            "baseurl": status.get("baseurl") or fallback_base,
            "ilink_bot_id": status.get("ilink_bot_id"),
            "ilink_user_id": status.get("ilink_user_id"),
            "logged_in_at": int(time.time()),
        }
        # 同一个 bot 重新扫码时保留游标：游标清零会把最近收到的消息再放一遍，
        # 用户会看到几条重复回答
        if previous.get("bot_token") == token:
            for key in ("get_updates_buf", "reply_context"):
                if previous.get(key):
                    account[key] = previous[key]
        _write_account(account)
    return account


def save_cursor(token: str, cursor: str) -> None:
    """
    把收信游标落盘。

    ★ 必须先推进游标再处理消息（worker 的调用顺序）
    进程在处理到一半被杀时，消息不会重放；代价是理论上可能漏答一条 ——
    相比「重启后把十几条旧消息逐条重答一遍」，这个取舍更接近用户预期。

    token 对不上说明期间换了绑定，这次游标属于旧账号，直接丢弃。
    """
    if not cursor:
        return
    with _ACCOUNT_LOCK:
        account = load_account()
        if str(account.get("bot_token") or "") != token:
            return
        if account.get("get_updates_buf") == cursor:
            return
        account["get_updates_buf"] = cursor
        _write_account(account)


def save_reply_context(token: str, user_id: str, context_token: str) -> bool:
    """保存绑定用户最近的收信上下文，供电脑端主动同步使用；换绑后旧消息不能覆盖它。"""
    if not user_id or not context_token:
        return False
    with _ACCOUNT_LOCK:
        account = load_account()
        if str(account.get("bot_token") or "") != token:
            return False
        owner = str(account.get("ilink_user_id") or "")
        if owner and owner != user_id:
            return False
        account["reply_context"] = {"user_id": user_id, "context_token": context_token}
        _write_account(account)
    return True


# ============================================================
# 收发信（通道 worker 用）
# ============================================================


async def _post_async(
    endpoint: str,
    body: Dict[str, Any],
    *,
    token: Optional[str] = None,
    timeout: float = 15.0,
    base: str = BASE_URL,
) -> Dict[str, Any]:
    url = f"{base.rstrip('/')}/{endpoint}"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=_headers(token), json=body, timeout=timeout)
    return _unwrap(endpoint, resp)


async def notify_start(token: str, base: str = BASE_URL) -> None:
    """告诉网关「这条长轮询要开始了」。探针实测失败也不影响收发，所以调用方可以忽略异常"""
    await _post_async(
        "ilink/bot/msg/notifystart",
        {"base_info": _base_info()},
        token=token,
        timeout=10.0,
        base=base,
    )


async def get_updates(token: str, cursor: str, base: str = BASE_URL) -> Dict[str, Any]:
    """取一批新消息。网关会挂住这条请求最多 40 秒，直到有消息或超时"""
    return await _post_async(
        "ilink/bot/getupdates",
        {"get_updates_buf": cursor, "base_info": _base_info()},
        token=token,
        timeout=GETUPDATES_LONG_POLL_S + 5,
        base=base,
    )


async def send_text(
    token: str,
    base: str,
    to_user_id: str,
    text: str,
    context_token: Optional[str] = None,
) -> Dict[str, Any]:
    """回一条文字消息。`context_token` 原样带回，网关靠它把回答挂到那条提问下面"""
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"xjms:{int(time.time() * 1000)}-{secrets.token_hex(4)}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": MSG_ITEM_TEXT, "text_item": {"text": text}}],
            "context_token": context_token,
        },
        "base_info": _base_info(),
    }
    return await _post_async("ilink/bot/sendmessage", body, token=token, timeout=20.0, base=base)


def extract_text(msg: Dict[str, Any]) -> str:
    """取出一条消息里的全部文字（多条文本项按换行拼起来；图片/语音取不到文字）"""
    parts: List[str] = []
    for item in msg.get("item_list") or []:
        if item.get("type") != MSG_ITEM_TEXT:
            continue
        text = (item.get("text_item") or {}).get("text")
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def split_reply(text: str, limit: int = REPLY_CHUNK_CHARS) -> List[str]:
    """
    长回答切成几条。优先在换行处切，切完不丢正文。

    微信不是浏览器，一条几千字的回复会被网关按长度拒绝；而那正好是
    「她答了，但微信那头什么都没收到」的一种来源。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    chunks: List[str] = []
    buf = ""
    for line in cleaned.splitlines():
        while len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return [chunk.strip("\n") for chunk in chunks if chunk.strip()]


def qr_png_data_url(text: str) -> str:
    """把登录链接画成二维码（PNG data URL）。segno 纯 Python、只依赖标准库，装不上就该报错而不是给张空图"""
    try:
        import segno
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时才会走到
        raise RuntimeError("缺少 segno，画不出二维码：pip install segno") from exc
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=2)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
