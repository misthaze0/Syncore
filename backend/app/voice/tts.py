"""
TTS（文字转语音）—— 双后端可插拔，与精排的 api/none 是同一模式。

- `dashscope`（默认）：`qwen3-tts-flash`，交互播报使用 SSE 音频片段；
  文件朗读保留完整音频下载。前端不接触带签名的 OSS 链接。
- `edge`：`edge-tts`（微软 Edge 朗读接口）。免费、无需密钥，需要联网。

★ 文本必须先清理再合成

Agent 的回答是 Markdown：代码块逐字念出来是灾难，URL 念一遍同样不可接受。
`clean_text_for_tts()` 在合成前剔除这些内容（纯函数，有单测）。
单次合成上限 600 字符（qwen3-tts 的 512 token 限制），超出截断并显式告知。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from app.config import settings
from app.voice.exceptions import (
    VoiceError,
    clean_text_for_tts,
    truncate_for_tts,
)

logger = logging.getLogger(__name__)

MP3_MIME = "audio/mpeg"
WAV_MIME = "audio/wav"
PCM_SAMPLE_RATE = 24000


async def _sse_objects(chunks: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    """Read SSE across arbitrary HTTP boundaries, including CRLF and a final unterminated event."""
    pending = b""
    data: list[str] = []

    def parse() -> dict[str, Any] | None:
        raw = "\n".join(data)
        data.clear()
        if not raw or raw == "[DONE]":
            return None
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise VoiceError("TTS 流式响应无法解析", status_code=502) from exc
        if not isinstance(value, dict):
            raise VoiceError("TTS 流式响应格式异常", status_code=502)
        return value

    async for chunk in chunks:
        pending += chunk
        if len(pending) > 2 * 1024 * 1024:
            raise VoiceError("TTS 流式响应帧过大", status_code=502)
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            text = line.rstrip(b"\r").decode("utf-8")
            if not text:
                value = parse()
                if value is not None:
                    yield value
            elif text.startswith("data:"):
                data.append(text[5:].lstrip(" "))
    if pending.startswith(b"data:"):
        data.append(pending[5:].decode("utf-8").strip())
    value = parse()
    if value is not None:
        yield value


def _detect_audio_meta(audio: bytes, declared: str) -> tuple[str, str]:
    """
    由字节头判定真实格式与 MIME。

    DashScope 的 `parameters.format` 并不可靠（实测传 `mp3` 仍返回 WAV），
    所以不能信声明、只信 `RIFF….WAVE` / ID3 / MP3 帧头这些字节签名。
    """
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        return "wav", WAV_MIME
    if audio[:3] == b"ID3":
        return "mp3", MP3_MIME
    if len(audio) >= 2 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0:
        return "mp3", MP3_MIME
    # 认不出就按声明兜底（极端情况下不至于让前端拿不到 content-type）
    return declared, MP3_MIME if declared == "mp3" else WAV_MIME


class DashscopeTTS:
    """Buffered download for exports; SSE PCM for interactive speech using the same model/voice."""

    name = "dashscope"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        audio_format: str = "mp3",
        timeout: float = 60.0,
        base: str = "https://dashscope.aliyuncs.com",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._format = audio_format
        self._timeout = timeout
        self._base = base.rstrip("/")
        # Engine lifetime follows model/voice settings. Never reuse another voice's greeting.
        self._pcm_cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_bytes = 0

    async def stream(self, text: str, *, use_cache: bool = True) -> AsyncIterator[bytes]:
        cached = self._pcm_cache.get(text) if use_cache else None
        if cached is not None:
            self._pcm_cache.move_to_end(text)
            yield cached
            return
        payload = {"model": self._model, "input": {"text": text, "voice": self._voice}}
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json", "X-DashScope-SSE": "enable"}
        url = f"{self._base}/api/v1/services/aigc/multimodal-generation/generation"
        keep: list[bytes] = []
        total = 0
        finished = False
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout, sock_read=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status != 200:
                        raise VoiceError(f"TTS 网关错误 {response.status}", status_code=502)
                    async for event in _sse_objects(response.content.iter_any()):
                        if event.get("code") or event.get("status_code", 200) != 200:
                            raise VoiceError(f"TTS 合成失败：{event.get('message') or event.get('code')}", status_code=502)
                        output = event.get("output") or {}
                        audio = output.get("audio") or {}
                        finished = finished or output.get("finish_reason") == "stop" or bool(audio.get("url"))
                        encoded = audio.get("data")
                        if not encoded:
                            continue
                        try:
                            pcm = base64.b64decode(encoded, validate=True)
                        except (ValueError, binascii.Error) as exc:
                            raise VoiceError("TTS 返回了无效音频片段", status_code=502) from exc
                        if not pcm:
                            continue
                        total += len(pcm)
                        if len(text) <= 50 and total <= 1024 * 1024:
                            keep.append(pcm)
                        yield pcm
        except TimeoutError as exc:
            raise VoiceError("语音合成超时，请稍后再试。", status_code=504) from exc
        except aiohttp.ClientError as exc:
            raise VoiceError("语音合成连接中断，请稍后再试。", status_code=502) from exc
        if not total or total % 2 or not finished:
            raise VoiceError("TTS 没有返回完整的音频", status_code=502)
        if use_cache and len(text) <= 50 and total <= 1024 * 1024:
            audio = b"".join(keep)
            self._cache_bytes -= len(self._pcm_cache.pop(text, b""))
            self._pcm_cache[text] = audio
            self._cache_bytes += len(audio)
            while len(self._pcm_cache) > 8 or self._cache_bytes > 2 * 1024 * 1024:
                _, old = self._pcm_cache.popitem(last=False)
                self._cache_bytes -= len(old)

    async def synthesize(self, text: str) -> bytes:
        payload: dict[str, Any] = {
            "model": self._model,
            "input": {"text": text, "voice": self._voice},
            "parameters": {"format": self._format},
        }
        url = f"{self._base}/api/v1/services/aigc/multimodal-generation/generation"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    body = await resp.json(content_type=None)
                if resp.status != 200:
                    msg = body.get("message") or body.get("code") or body
                    raise VoiceError(f"TTS 网关错误 {resp.status}: {msg}", status_code=502)
                try:
                    audio_url = body["output"]["audio"]["url"]
                except (KeyError, TypeError) as e:
                    raise VoiceError(f"TTS 响应结构异常: {e}") from e
                # URL 只在服务端停留：立即下载，避免前端持有会过期的签名链接
                try:
                    async with session.get(audio_url) as dl:
                        if dl.status != 200:
                            raise VoiceError(f"音频下载失败 {dl.status}", status_code=502)
                        return await dl.read()
                except VoiceError:
                    raise
                except aiohttp.ClientError as e:
                    # 下载阶段的网络错误也归 502，但要和「网关错误」区分开，便于排查
                    raise VoiceError(f"音频下载失败: {e}", status_code=502) from e
        except VoiceError:
            raise
        except aiohttp.ClientError as e:
            raise VoiceError(f"TTS 网络请求失败: {e}") from e


class EdgeTTS:
    """edge-tts 免费后端（无需密钥）"""

    name = "edge"

    def __init__(self, *, voice: str, rate: str = "+0%", timeout: float = 60.0) -> None:
        self._voice = voice
        self._rate = rate
        self._timeout = timeout

    async def synthesize(self, text: str) -> bytes:
        try:
            import edge_tts  # 延迟导入：未安装时只影响该后端，不影响模块加载
        except ImportError as e:
            raise VoiceError(
                "edge-tts 未安装（pip install edge-tts）", status_code=500
            ) from e

        try:
            communicate = edge_tts.Communicate(text, voice=self._voice, rate=self._rate)
            chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
        except Exception as e:  # noqa: BLE001 —— edge-tts 的异常类型不稳定，统一收口
            raise VoiceError(f"edge-tts 合成失败: {e}") from e

        audio = b"".join(chunks)
        if not audio:
            raise VoiceError("edge-tts 返回空音频（文本可能无有效内容）")
        return audio


_engine: DashscopeTTS | EdgeTTS | None = None


def reset_engine() -> None:
    """
    丢弃缓存的 TTS 引擎（语音配置运行时变更后调用）。

    引擎在构造时捏住了 api_key / model / voice —— 不重置的话，设置页换了
    模型或音色界面上显示已生效，实际合成的还是旧配置。
    """
    global _engine
    _engine = None


def get_engine() -> DashscopeTTS | EdgeTTS:
    """按配置构造 TTS 引擎（惰性单例；配置错误时显式报错，不静默换后端）"""
    global _engine
    if _engine is not None:
        return _engine

    backend = settings.VOICE_TTS_BACKEND.strip().lower()
    if backend == "dashscope":
        _engine = DashscopeTTS(
            api_key=settings.LLM_API_KEY,
            model=settings.VOICE_TTS_MODEL,
            voice=settings.VOICE_TTS_VOICE,
            audio_format=settings.VOICE_TTS_FORMAT,
            timeout=settings.VOICE_TTS_TIMEOUT_SECONDS,
        )
    elif backend == "edge":
        _engine = EdgeTTS(
            voice=settings.VOICE_TTS_VOICE_EDGE,
            rate=settings.VOICE_TTS_RATE,
            timeout=settings.VOICE_TTS_TIMEOUT_SECONDS,
        )
    else:
        raise VoiceError(
            f"未知 TTS 后端 '{backend}'（可选: dashscope / edge）", status_code=500
        )
    return _engine


async def synthesize(raw_text: str) -> dict[str, Any]:
    """
    文本 → 音频。清理 → 截断 → 合成。

    Returns:
        {"audio": bytes, "mime": str, "truncated": bool, "chars": int, "backend": str}
    """
    text, truncated = prepare_speech(raw_text)
    engine = get_engine()
    audio = await engine.synthesize(text)
    if not audio:
        raise VoiceError(f"TTS 后端 {engine.name} 返回空音频")

    fmt, mime = _detect_audio_meta(audio, settings.VOICE_TTS_FORMAT)
    return {
        "audio": audio,
        "mime": mime,
        "format": fmt,
        "truncated": truncated,
        "chars": len(text),
        "backend": engine.name,
    }


def prepare_speech(raw_text: str) -> tuple[str, bool]:
    text = clean_text_for_tts(raw_text)
    if not text:
        raise VoiceError("没有可朗读的内容（清理后文本为空）", status_code=400)
    return truncate_for_tts(text)


def tts_status() -> dict[str, Any]:
    """能力状态（供 /api/voice/status 与启动日志）"""
    backend = settings.VOICE_TTS_BACKEND.strip().lower()
    info: dict[str, Any] = {"backend": backend}
    if backend == "dashscope":
        info["model"] = settings.VOICE_TTS_MODEL
        info["voice"] = settings.VOICE_TTS_VOICE
        info["streaming"] = True
        info["available"] = bool(settings.LLM_API_KEY)
        if not info["available"]:
            info["reason"] = "未配置 LLM_API_KEY"
    elif backend == "edge":
        info["model"] = "edge-tts"
        info["streaming"] = False
        info["voice"] = settings.VOICE_TTS_VOICE_EDGE
        try:
            import edge_tts  # noqa: F401

            info["available"] = True
        except ImportError:
            info["available"] = False
            info["reason"] = "edge-tts 未安装（pip install edge-tts）"
    else:
        info["available"] = False
        info["reason"] = f"未知后端 '{backend}'（可选: dashscope / edge）"
    return info
