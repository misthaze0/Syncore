"""
语音接口 —— STT 转写 / TTS 合成 / 能力状态

★ 为什么 TTS 返回二进制而不是 base64 JSON

音频走 `Response(content=…, media_type="audio/mpeg")`，前端拿 blob 直接喂
`new Audio()` / `URL.createObjectURL`。base64 会让响应体膨胀 33%，且前端还要
再解一次 —— 没有任何好处。截断等元信息放响应头（`X-Truncated`），
不为了两个字段把整个响应包成 JSON。

★ 为什么 STT 要限大小

ASR 按音频时长计费。不设上限时，一个手滑传进来的 500MB 视频文件
会先吃满内存再吃光配额。10MB 已远超「一轮语音提问」的量级。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

import numpy as np
from fastapi import APIRouter, File, HTTPException, Response, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from app.config import settings
from app.voice import wake as wake_voice
from app.voice.exceptions import VoiceError, ext_of
from app.voice.model_settings import VoiceSettingsError, probe as voice_probe, reset as voice_reset, state as voice_state, update as voice_update
from app.voice.stt import stt_status, transcribe as transcribe_voice
from app.voice.tts import PCM_SAMPLE_RATE, DashscopeTTS, get_engine, prepare_speech, synthesize, tts_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


@router.get("/status")
async def voice_status() -> Dict[str, Any]:
    """
    语音能力状态。

    STT 的 available：本地后端只查模型文件是否在，云端后端只看有没有 key
    （惰性单例的教训见架构不变量 23：状态查询不能谎报）。两边都不打网关 ——
    真实可达性以一次转写为准。
    """
    return {
        "enabled": settings.VOICE_ENABLED,
        "stt": stt_status(),
        "tts": tts_status(),
    }


# ---------------------------------------------------------------- 桌面助手唤醒（KWS）


class WakeWordRequest(BaseModel):
    word: str = Field(..., min_length=1, max_length=32)


@router.get("/wake/status")
async def wake_status() -> Dict[str, Any]:
    """唤醒模型是否就绪。只查文件，不加载模型。"""
    info = wake_voice.status()
    info["enabled"] = settings.VOICE_ENABLED
    if not settings.VOICE_ENABLED:
        info["available"] = False
        info["reason"] = "语音功能未启用（VOICE_ENABLED=false）"
    return info


@router.post("/wake/check")
async def wake_check(payload: WakeWordRequest) -> Dict[str, Any]:
    """
    校验唤醒词（永远 200：非法词本身就是要展示给用户的结果）。

    只做归一化与分词校验；模型缺失时也允许保存，等模型就绪后再实际启用。
    """
    if not settings.VOICE_ENABLED:
        return {"ok": False, "word": payload.word, "tokens": [], "reason": "语音功能未启用"}
    try:
        result = await asyncio.to_thread(wake_voice.check_wake_word, payload.word)
    except VoiceError as e:
        return {"ok": False, "word": payload.word, "tokens": [], "reason": str(e)}
    return {"ok": True, "reason": None, **result}


# 同一时刻只保留一条唤醒连接：新连接顶掉旧连接（开发热重载时旧页可能来不及断开）。
_active_wake: WebSocket | None = None
_active_guard = asyncio.Lock()
_MAX_WAKE_FRAME_BYTES = 256 * 1024


async def _close_ws(websocket: WebSocket, code: int = 1000) -> None:
    try:
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001
        pass


@router.websocket("/wake")
async def wake_socket(websocket: WebSocket) -> None:
    """
    桌面助手唤醒 WebSocket。

    协议：客户端先发 `{"type":"start","wake_word":"…"}`，服务端加载模型后回
    `{"type":"ready"}`；随后客户端发送 Float32 LE、单声道 16kHz 的二进制帧。
    命中回 `{"type":"wake","keyword":"…"}`，客户端用 `pause/resume/stop` 控制。
    """
    await websocket.accept()

    if not settings.VOICE_ENABLED:
        await websocket.send_json({"type": "error", "code": "disabled", "message": "语音功能未启用"})
        await _close_ws(websocket, 4403)
        return

    global _active_wake
    async with _active_guard:
        previous = _active_wake
        _active_wake = websocket
    if previous is not None and previous is not websocket:
        await _close_ws(previous, 4001)

    session: wake_voice.WakeSession | None = None
    paused = False
    try:
        first = await asyncio.wait_for(websocket.receive(), timeout=10.0)
        raw = first.get("text")
        if first.get("type") == "websocket.disconnect" or raw is None:
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("type") != "start":
            await websocket.send_json({"type": "error", "code": "bad_start", "message": "第一帧必须是 start"})
            return

        word = str(payload.get("wake_word") or wake_voice.DEFAULT_WAKE_WORD)
        try:
            session = await asyncio.to_thread(wake_voice.get_engine().create_session, word)
        except VoiceError as e:
            await websocket.send_json({"type": "error", "code": "unavailable", "message": str(e)})
            await _close_ws(websocket, 4401)
            return

        await websocket.send_json({
            "type": "ready",
            "keyword": session.word,
            "sample_rate": wake_voice.SAMPLE_RATE,
        })

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data is not None:
                if paused or (session is not None and session.detected):
                    continue
                if not data or len(data) % 4 or len(data) > _MAX_WAKE_FRAME_BYTES:
                    await websocket.send_json({"type": "error", "code": "bad_frame", "message": "音频帧格式不对"})
                    break
                samples = np.frombuffer(data, dtype="<f4")
                detected = await asyncio.to_thread(session.feed, samples)
                if detected:
                    await websocket.send_json({"type": "wake", "keyword": detected})
                continue

            raw = message.get("text")
            if raw is None:
                continue
            try:
                command = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "code": "bad_json", "message": "无法解析控制消息"})
                continue
            if not isinstance(command, dict):
                continue
            kind = command.get("type")
            if kind == "pause":
                paused = True
                if session is not None:
                    await asyncio.to_thread(session.reset)
            elif kind == "resume":
                paused = False
                if session is not None:
                    await asyncio.to_thread(session.reset)
            elif kind == "stop":
                break
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})

    except asyncio.TimeoutError:
        await websocket.send_json({"type": "error", "code": "timeout", "message": "等待 start 超时"})
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("唤醒 WebSocket 异常：%s: %s", type(e).__name__, e)
    finally:
        async with _active_guard:
            if _active_wake is websocket:
                _active_wake = None
        await _close_ws(websocket)


@router.post("/transcribe")
async def transcribe_audio(
    file: UploadFile = File(..., description="音频文件（wav/mp3/aac/flac/opus/ogg/amr）"),
) -> Dict[str, Any]:
    """
    语音 → 文字（本地 SenseVoice / 云端 qwen3-asr-flash，按配置分派）。

    浏览器 MediaRecorder 默认产出 webm —— 不在支持列表里，前端会先转
    16kHz 单声道 WAV 再上传（`lib/audio.ts`），这里收到即转写。
    本地后端只接受 WAV；云端后端接受 wav/mp3/aac/flac/opus/ogg/amr。
    """
    if not settings.VOICE_ENABLED:
        raise HTTPException(status_code=503, detail="语音功能未启用（VOICE_ENABLED=false）")

    max_bytes = int(settings.VOICE_MAX_AUDIO_MB * 1024 * 1024)
    audio = await file.read(max_bytes + 1)
    if not audio:
        raise HTTPException(status_code=400, detail="上传的音频为空")

    if len(audio) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"音频超过 {settings.VOICE_MAX_AUDIO_MB}MB 上限（收到 {len(audio) / 1024 / 1024:.1f}MB）",
        )

    fmt = ext_of(filename=file.filename, content_type=file.content_type)
    try:
        text = await transcribe_voice(audio, fmt)
    except VoiceError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    stt = stt_status()
    return {
        "text": text,
        "format": fmt,
        "bytes": len(audio),
        "backend": stt.get("backend"),
        "model": stt.get("model"),
        "empty": not text,
    }


class SpeakRequest(BaseModel):
    """朗读请求。text 传 Agent 的回答原文即可（Markdown 由后端清理）"""

    text: str = Field(..., min_length=1, max_length=5000)


@router.post("/tts")
async def speak(payload: SpeakRequest) -> Response:
    """文字 → 音频（mp3）。超长文本按 600 字符截断（`X-Truncated: true`）"""
    if not settings.VOICE_ENABLED:
        raise HTTPException(status_code=503, detail="语音功能未启用（VOICE_ENABLED=false）")

    try:
        result = await synthesize(payload.text)
    except VoiceError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    headers = {
        "X-Truncated": "true" if result["truncated"] else "false",
        "X-TTS-Backend": result["backend"],
        "X-TTS-Chars": str(result["chars"]),
        # 音频内容由每次请求的文本决定，缓存无副作用且能显著提速重复朗读
        "Cache-Control": "no-store",
    }
    return Response(content=result["audio"], media_type=result["mime"], headers=headers)


# ---------------------------------------------------------------- 语音模型设置


@router.post("/tts/stream")
async def speak_stream(payload: SpeakRequest):
    """Interactive speech: emit 24 kHz mono s16le PCM before synthesis finishes."""
    if not settings.VOICE_ENABLED:
        raise HTTPException(status_code=503, detail="语音功能未启用（VOICE_ENABLED=false）")
    try:
        text, truncated = prepare_speech(payload.text)
        engine = get_engine()
        if not isinstance(engine, DashscopeTTS):
            # Encoded backends keep their original format; the player detects it.
            return await speak(payload)
        chunks = engine.stream(text)
        try:
            first = await anext(chunks)
        except StopAsyncIteration as exc:
            raise VoiceError("TTS 返回空音频", status_code=502) from exc
    except VoiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    async def body():
        try:
            yield first
            async for chunk in chunks:
                yield chunk
        finally:
            await chunks.aclose()

    return StreamingResponse(body(), media_type=f"audio/pcm;rate={PCM_SAMPLE_RATE};channels=1", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        "X-TTS-Backend": engine.name, "X-TTS-Model": settings.VOICE_TTS_MODEL,
        "X-Truncated": str(truncated).lower(),
    })


class VoiceSettingsPatch(BaseModel):
    """部分更新语音配置 —— 只传要改的键"""

    VOICE_STT_BACKEND: str | None = None
    VOICE_STT_MODEL: str | None = None
    VOICE_TTS_BACKEND: str | None = None
    VOICE_TTS_MODEL: str | None = None
    VOICE_TTS_VOICE: str | None = None
    VOICE_TTS_VOICE_EDGE: str | None = None
    VOICE_TTS_RATE: str | None = None


@router.get("/settings")
async def get_voice_settings() -> Dict[str, Any]:
    """当前生效的语音配置 + 覆写现状（只读）"""
    return voice_state()


@router.put("/settings")
async def put_voice_settings(payload: VoiceSettingsPatch) -> Dict[str, Any]:
    """部分更新语音配置；落盘即生效（引擎/识别器单例已重置）"""
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        return voice_update(patch)
    except VoiceSettingsError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/settings/reset")
async def reset_voice_settings() -> Dict[str, Any]:
    """清空语音域的运行时覆写 → 回到 backend/.env（不动对话/检索模型）"""
    return voice_reset()


@router.post("/settings/probe")
async def probe_voice_settings() -> Dict[str, Any]:
    """用当前配置真合成一小段语音（永远 200：失败本身就是答案）"""
    return await voice_probe()
