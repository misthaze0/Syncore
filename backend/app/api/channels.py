"""
外部渠道 —— 状态（`GET /channels`）与微信扫码登录（`/channels/wechat/login*`）

★ 状态接口只读内存与凭据文件，不去敲网关探活

微信 iLink 是**客户端主动长轮询**收消息的。状态接口要是去调一次 `getupdates`
探活，就会和通道 worker 抢同一份游标 —— 探一次活，用户那条消息就被这个
「只是想知道红点该什么颜色」的请求吃掉。所以这里只回答两件确定的事：
这台机器上有没有绑定凭据（`bound`），以及 worker 进程在不在跑（`running`）。
（登录那几条接口当然要走网络，但它碰的是 `get_qrcode_status`，和收信游标无关。）

★ `bound` 与 `running` 是两件事

凭据在 ≠ 通道在跑：后端停了、图没初始化、worker 正在退避重连，微信那头都
可能没人回话。界面按 `running` 说「已连接」，按 `bound` 说「已绑定」——
把两者混成一个布尔值，会让「凭据还在但服务死了」显示成一切正常。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.channels import wechat_ilink
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])


def _worker_state() -> Dict[str, Any]:
    """worker 的内存快照。导入失败也不能让状态接口 500 —— 那只是少了一行字"""
    try:
        from app.channels.wechat_worker import wechat_channel

        return wechat_channel.status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("微信通道状态读取失败: %s", exc)
        return {}


def _wechat_state() -> Dict[str, Any]:
    """微信（iLink / ClawBot）状态：只看账号文件与 worker 内存，不发任何网络请求"""
    worker = _worker_state()
    state: Dict[str, Any] = {
        "key": "wechat",
        "label": "微信",
        "transport": "ilink",
        "bound": False,
        "running": bool(worker.get("running")),
        "last_error": str(worker.get("last_error") or ""),
        "last_message_at": worker.get("last_message_at") or 0,
    }
    path = settings.wechat_account_path
    if not path.exists():
        return {**state, "running": False, "detail": "未绑定"}
    try:
        account = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 文件在但读不出来 —— 说清是文件的问题，别退化成「未绑定」让人以为没扫过码
        logger.warning("微信账号文件读不出来 %s: %s", path, exc)
        return {**state, "running": False, "detail": "凭据文件读不出来"}
    if not str(account.get("bot_token") or "").strip():
        return {**state, "running": False, "detail": "凭据不完整"}
    if state["running"]:
        detail = "已连接" if not state["last_error"] else "已连接（重连中）"
    else:
        detail = "已绑定，通道未运行"
    return {
        **state,
        "bound": True,
        "detail": detail,
        "bound_at": account.get("logged_in_at"),
    }


@router.get("")
async def list_channels() -> Dict[str, Any]:
    """渠道清单（目前只有微信一条）"""
    channels: List[Dict[str, Any]] = [_wechat_state()]
    return {"channels": channels}


# ============================================================
# 扫码登录（一次性会话，状态只在内存里）
# ============================================================

#: 还没到终态的档 —— 用来判断「重复点连接要不要换一张码」
_ACTIVE = {"waiting", "scanned", "verifycode"}


class VerifyCodeRequest(BaseModel):
    code: str


@dataclass
class _Login:
    qrcode: str
    qr_png: str
    started_at: float
    base: str
    status: str = "waiting"
    detail: str = ""
    verify_code: Optional[str] = None
    task: Optional[asyncio.Task] = None


_LOGIN: Optional[_Login] = None
_LOGIN_LOCK = asyncio.Lock()


async def _poll_loop(login: _Login) -> None:
    """
    后台轮询网关，把结果写进 `_LOGIN`；界面只读这份快照。

    ★ 为什么轮询在后端而不是让浏览器直接问网关
    浏览器跨域敲不动 iLink，而且 `get_qrcode_status` 在网关侧是**长轮询**（一次挂 35s）：
    前端每 1.5s 刷一次就会叠出二十几条并发长轮询，同一张码被问成这样只会先被限流。
    所以后端只留一条轮询，界面爱怎么刷快照都行 —— 读内存不花钱。
    """
    deadline = login.started_at + wechat_ilink.LOGIN_TIMEOUT_S
    errors = 0
    while time.time() < deadline:
        try:
            st = await asyncio.to_thread(
                wechat_ilink.poll_login, login.qrcode, login.verify_code, login.base
            )
            errors = 0
        except Exception as exc:  # 网络抖一下不该让整次登录作废
            errors += 1
            if errors >= 4:
                login.status, login.detail = "failed", f"问不到扫码结果：{exc}"
                return
            await asyncio.sleep(2)
            continue

        code = str(st.get("status") or "")
        if code == "wait":
            login.status, login.detail = "waiting", ""
        elif code == "scaned":
            login.status, login.detail, login.verify_code = "scanned", "", None
        elif code == "need_verifycode":
            login.status, login.detail = "verifycode", ""
            while login.verify_code is None and time.time() < deadline:
                await asyncio.sleep(0.4)
        elif code == "scaned_but_redirect":
            # 网关会把这次会话挪到另一台机器上，后续请求都得跟着换 host
            host = str(st.get("redirect_host") or "").strip()
            if host:
                login.base = f"https://{host}"
        elif code == "expired":
            login.status, login.detail = "expired", "二维码已过期"
            return
        elif code == "binded_redirect":
            login.status, login.detail = "bound", "这个微信号之前已经绑过，不用重复扫"
            return
        elif code == "confirmed":
            try:
                account = await asyncio.to_thread(wechat_ilink.save_account, st, login.base)
            except Exception as exc:
                logger.warning("微信凭据落盘失败: %s", exc)
                login.status, login.detail = "failed", f"凭据没存下来：{exc}"
                return
            bot_id = str(account.get("ilink_bot_id") or "")
            # 凭据刚写完就让 worker 用新 token 重连长轮询 —— 之前已经有一条
            # 挂在旧凭据上（或者根本没有），不重启就得等下次后端启动才生效。
            try:
                from app.channels.wechat_worker import wechat_channel

                started = await wechat_channel.restart()
            except Exception as exc:  # noqa: BLE001 - 绑定成功不该因为 worker 起不来而报失败
                logger.warning("微信通道启动失败（绑定已保存）: %s", exc)
                started = False
            suffix = " · 已连接" if started else ""
            login.status = "confirmed"
            login.detail = f"{bot_id}{suffix}" if bot_id else f"已绑定{suffix}"
            return
        else:
            login.status, login.detail = "failed", f"网关回了没见过的状态 {code or '（空）'}"
            return

    login.status, login.detail = "expired", "等太久，二维码已经失效"


def _login_snapshot() -> Dict[str, Any]:
    if _LOGIN is None:
        return {"status": "idle", "detail": ""}
    left = int(_LOGIN.started_at + wechat_ilink.LOGIN_TIMEOUT_S - time.time())
    return {
        "status": _LOGIN.status,
        "detail": _LOGIN.detail,
        "qr_png": _LOGIN.qr_png if _LOGIN.status in _ACTIVE else "",
        # 终态没有「还剩多久」这回事，给 0 而不是负数，省得界面再判一次
        "expires_in": max(0, left) if _LOGIN.status in _ACTIVE else 0,
    }


@router.post("/wechat/login")
async def start_wechat_login() -> Dict[str, Any]:
    """开一次扫码登录；已经在等扫的时候把当前那张码原样还回去（换一张等于让用户重扫）"""
    global _LOGIN
    async with _LOGIN_LOCK:
        if _LOGIN is not None and _LOGIN.status in _ACTIVE:
            return {"state": _login_snapshot()}
        try:
            ticket = await asyncio.to_thread(wechat_ilink.start_login)
            png = await asyncio.to_thread(wechat_ilink.qr_png_data_url, ticket.qr_url)
        except Exception as exc:
            # 网关的原文只进日志，不往界面抛 —— errmsg 里可能带着令牌
            logger.warning("申请微信登录二维码失败: %s", exc)
            raise HTTPException(status_code=502, detail="微信没给二维码，稍后再试") from exc
        if _LOGIN is not None and _LOGIN.task is not None:
            _LOGIN.task.cancel()
        _LOGIN = _Login(
            qrcode=ticket.qrcode, qr_png=png, started_at=ticket.started_at, base=wechat_ilink.BASE_URL
        )
        _LOGIN.task = asyncio.create_task(_poll_loop(_LOGIN))
        return {"state": _login_snapshot()}


@router.get("/wechat/login")
async def get_wechat_login() -> Dict[str, Any]:
    """当前登录会话的快照（不打网关，读内存）"""
    return {"state": _login_snapshot()}


@router.post("/wechat/login/verify")
async def submit_verify_code(body: VerifyCodeRequest) -> Dict[str, Any]:
    """手机上弹出数字验证码时，把它交给正在轮询的那条会话"""
    async with _LOGIN_LOCK:
        if _LOGIN is None or _LOGIN.status != "verifycode":
            raise HTTPException(status_code=409, detail="现在没有在等验证码的登录")
        _LOGIN.verify_code = body.code.strip()[:8]
        _LOGIN.status, _LOGIN.detail = "scanned", ""
        return {"state": _login_snapshot()}


@router.delete("/wechat/login")
async def cancel_wechat_login() -> Dict[str, Any]:
    """关掉扫码弹窗：停掉轮询，会话作废（已绑定的凭据不受影响）"""
    global _LOGIN
    async with _LOGIN_LOCK:
        if _LOGIN is not None and _LOGIN.task is not None:
            _LOGIN.task.cancel()
        _LOGIN = None
    return {"state": {"status": "idle", "detail": ""}}
