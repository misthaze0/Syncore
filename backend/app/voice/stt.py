"""
STT（语音转文字）—— 双后端：本地 SenseVoice（默认）/ 云端 DashScope。

本地引擎在 `local_stt.py`（sherpa-onnx，完全离线）；本模块保留云端实现，
并作为统一入口按 `VOICE_STT_BACKEND` 分派。下面是云端路径的说明：

DashScope 原生 multimodal-generation REST + base64 内嵌。

★ 为什么是原生接口而不是 OpenAI 兼容层（实测踩坑）

官方文档称 qwen3-asr-flash 支持 OpenAI 兼容调用，但没写清楚的是：
兼容层**只接受公网可达的音频 URL**。传标准 OpenAI 的 `input_audio` base64
会得到一个误导性很强的 400 —— "The provided URL does not appear to be valid"
（说的是 URL，实际问题是 base64 根本没被支持）。

原生接口的 `input.messages[].content[].audio` 字段接受
`data:audio/<fmt>;base64,…` data URI，单次请求即完成转写，且只需要
aiohttp + 标准库 —— 延续本项目「LangFuse 走 REST 不引 SDK」的零依赖传统。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import aiohttp

from app.config import settings
from app.voice.exceptions import VoiceError, check_audio_format

logger = logging.getLogger(__name__)

#: DashScope 原生 API 根（北京区）。LLM_BASE_URL 指向的是 compatible-mode，不能混用。
DASHSCOPE_NATIVE_BASE = "https://dashscope.aliyuncs.com"


class SpeechRecognizer:
    """qwen3-asr-flash 转写器（无状态，可复用）"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        base: str = DASHSCOPE_NATIVE_BASE,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._base = base.rstrip("/")

    async def transcribe(self, audio: bytes, fmt: str) -> str:
        """
        把音频字节转成文字。

        Args:
            audio: 音频原始字节（wav/mp3/aac/flac/opus/ogg/amr）
            fmt:   音频格式（小写扩展名）

        Raises:
            VoiceError: 格式不支持（415）/ 网关错误（502）/ 转写失败（502）
        """
        check_audio_format(fmt)
        if not audio:
            raise VoiceError("音频内容为空", status_code=400)

        payload = {
            "model": self._model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "audio": (
                                    f"data:audio/{fmt};base64,"
                                    f"{base64.b64encode(audio).decode('ascii')}"
                                )
                            }
                        ],
                    }
                ]
            },
        }

        data = await self._post(payload)
        try:
            content = data["output"]["choices"][0]["message"]["content"]
            text = content[0]["text"] if isinstance(content, list) else str(content)
        except (KeyError, IndexError, TypeError) as e:
            raise VoiceError(f"ASR 响应结构异常: {e}") from e

        text = (text or "").strip()
        if not text:
            # 空白音频/纯噪声：不当作错误，如实返回空串由调用方提示
            logger.info("ASR 返回空文本（音频可能无有效语音）")
        return text

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/api/v1/services/aigc/multimodal-generation/generation"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    body = await resp.json(content_type=None)
        except TimeoutError as e:
            # aiohttp 的 ClientTimeout(total=…) 抛的是 asyncio.TimeoutError，
            # 它**不是** aiohttp.ClientError —— 不单独接住会漏成 500（长音频实测踩到）
            raise VoiceError(
                f"ASR 请求超时（{self._timeout:.0f}s）：音频可能过长或链路过慢",
                status_code=504,
            ) from e
        except aiohttp.ClientError as e:
            raise VoiceError(f"ASR 网络请求失败: {e}", status_code=502) from e

        if resp.status != 200:
            # 透出网关给的 code/message，排查时不用再抓包
            msg = body.get("message") or body.get("code") or body
            raise VoiceError(f"ASR 网关错误 {resp.status}: {msg}", status_code=502)
        return body


_recognizer: SpeechRecognizer | None = None


def get_recognizer() -> SpeechRecognizer:
    """惰性单例（与 reranker / judge 同一模式，避免 import 时读不到 .env）"""
    global _recognizer
    if _recognizer is None:
        _recognizer = SpeechRecognizer(
            api_key=settings.LLM_API_KEY,
            model=settings.VOICE_STT_MODEL,
            timeout=settings.VOICE_STT_TIMEOUT_SECONDS,
        )
    return _recognizer


def reset_recognizer() -> None:
    """
    丢弃缓存的识别器（语音配置运行时变更后调用）。

    单例在构造时捏住了 model / api_key —— 不重置的话，设置页换了模型
    界面上显示已生效，实际识别请求还打给旧配置。
    """
    global _recognizer
    _recognizer = None


async def transcribe(audio: bytes, fmt: str) -> str:
    """STT 统一入口：按 `VOICE_STT_BACKEND` 分派到本地或云端。"""
    if settings.VOICE_STT_BACKEND == "local":
        from app.voice.local_stt import transcribe as local_transcribe

        return await local_transcribe(audio, fmt)
    return await get_recognizer().transcribe(audio, fmt)


def stt_status() -> dict[str, Any]:
    """当前 STT 后端的可用性。状态查询不打网关、不加载模型。"""
    if settings.VOICE_STT_BACKEND == "local":
        from app.voice.local_stt import status as local_status

        info = local_status()
        info["backend"] = "local"
        if not settings.VOICE_ENABLED:
            info["available"] = False
            info["reason"] = "VOICE_ENABLED=false"
        return info

    available = bool(settings.LLM_API_KEY) and settings.VOICE_ENABLED
    if not settings.VOICE_ENABLED:
        reason = "VOICE_ENABLED=false"
    elif not settings.LLM_API_KEY:
        reason = "未配置 LLM_API_KEY"
    else:
        reason = None
    return {
        "backend": "dashscope",
        "available": available,
        "model": settings.VOICE_STT_MODEL,
        "reason": reason,
    }
