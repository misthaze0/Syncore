"""
本地 STT —— sherpa-onnx + SenseVoiceSmall（int8），完全离线。

★ 为什么默认走本地

云端 qwen3-asr-flash 的准确率更好，但语音输入是一段**未压缩的 16kHz WAV**：
10 秒音频 base64 后约 430KB。实测在一条绕境外的链路上只有 20–36KB/s，
上传就要十几秒；而同一台机器上本地推理只要 1–2 秒。语音是入口增强，
先把入口做顺，云端保留为可切换项。

★ 输入边界

只接受 16bit PCM WAV（前端 `lib/audio.ts` 已经统一转成 16kHz 单声道）。
其他容器（mp3/webm/…）不在这里解码 —— 要么在前端转好，要么切到云端。
这是刻意的：不为边缘格式引入 ffmpeg / 解码依赖。

★ 模型从哪来

默认放在本地模型根目录（与 Embedding / 精排同一个约定）下的
`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` —— 可用
`VOICE_STT_LOCAL_MODEL` 直接指到别处。目录里需要 `model.int8.onnx` + `tokens.txt`。
模型不随仓库提交；缺失时 `/voice/status` 如实报 unavailable，识别请求返回 503。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from app.config import settings
from app.voice.exceptions import VoiceError

try:  # 本地识别是可选能力：缺少依赖只影响这一条路径，不阻塞应用启动
    import sherpa_onnx
except Exception as e:  # noqa: BLE001
    sherpa_onnx = None  # type: ignore[assignment]
    _IMPORT_ERROR: str | None = f"{type(e).__name__}: {e}"
else:
    _IMPORT_ERROR = None

logger = logging.getLogger(__name__)

MODEL_DIR_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
MODEL_FILE = "model.int8.onnx"
TOKENS_FILE = "tokens.txt"
MODEL_LABEL = "SenseVoiceSmall-int8"

# SenseVoice 会带上 <|zh|> / <|NEUTRAL|> / <|Speech|> 这类控制标记，不能读进输入框
_SPECIAL_TAG = re.compile(r"<\|[^|]*\|>")


def model_root() -> Path:
    """
    本地模型根目录：与检索模型同一个约定（显式 `LOCAL_MODEL_DIR` 优先，
    否则从两个本地检索模型的路径推断）。

    ⚠️ 根目录必须是 ASCII 路径：ONNX Runtime 的 C++ 侧遇到中文路径会打不开模型。
    """
    explicit = str(getattr(settings, "LOCAL_MODEL_DIR", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    for ref in (settings.EMBEDDING_LOCAL_MODEL, settings.RERANK_LOCAL_MODEL):
        parent = Path(str(ref)).expanduser().parent
        if str(parent) in ("", ".") or not parent.is_dir():
            continue
        return parent

    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "xjms" / "models"


def model_dir() -> Path:
    """模型目录：显式配置优先，否则放在本地模型根目录下。"""
    override = str(getattr(settings, "VOICE_STT_LOCAL_MODEL", "") or "").strip()
    if override:
        return Path(override).expanduser()
    return model_root() / MODEL_DIR_NAME


def num_threads() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def decode_wav(audio: bytes) -> tuple[int, np.ndarray]:
    """
    把 WAV 字节解成 (采样率, float32 单声道样本)。

    Raises:
        VoiceError: 不是合法 WAV / 不是 16bit PCM / 没有采样点
    """
    try:
        with wave.open(io.BytesIO(audio), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError) as e:
        raise VoiceError(f"无法解析 WAV: {e}", status_code=415) from e

    if sampwidth != 2:
        raise VoiceError(
            f"本地识别只支持 16bit PCM WAV（收到 {sampwidth * 8}bit）；"
            "请在前端转成 WAV，或切换到云端识别。",
            status_code=415,
        )

    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size == 0:
        raise VoiceError("音频内容为空", status_code=400)

    if channels > 1:
        usable = samples.size - samples.size % channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)

    return rate, samples


def _clean_text(text: str) -> str:
    return _SPECIAL_TAG.sub("", text or "").strip()


class LocalRecognizer:
    """SenseVoice 识别器：模型惰性加载、单实例串行解码。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recognizer: Any = None
        self._load_error: str | None = None
        self._load_seconds: float | None = None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    @property
    def loaded(self) -> bool:
        return self._recognizer is not None

    def ensure_loaded(self) -> Any:
        if self._recognizer is not None:
            return self._recognizer

        with self._lock:
            if self._recognizer is not None:
                return self._recognizer
            if sherpa_onnx is None:
                raise VoiceError(
                    f"本地识别不可用：sherpa-onnx 未安装（{_IMPORT_ERROR}）",
                    status_code=503,
                )

            directory = model_dir()
            model = directory / MODEL_FILE
            tokens = directory / TOKENS_FILE
            missing = [str(p) for p in (model, tokens) if not p.is_file()]
            if missing:
                raise VoiceError(
                    "本地识别模型未安装：缺少 "
                    + "、".join(missing)
                    + "。请安装 SenseVoice 模型，或在设置里切换到云端识别。",
                    status_code=503,
                )

            started = time.perf_counter()
            try:
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(model),
                    tokens=str(tokens),
                    num_threads=num_threads(),
                    language="auto",
                    use_itn=True,
                    provider="cpu",
                    debug=False,
                )
            except Exception as e:  # noqa: BLE001
                self._load_error = f"{type(e).__name__}: {e}"
                raise VoiceError(f"本地识别模型加载失败：{self._load_error}", status_code=503) from e

            self._load_error = None
            self._load_seconds = time.perf_counter() - started
            logger.info(
                "本地 STT 就绪：%s（加载 %.1fs，%s 线程）",
                MODEL_LABEL,
                self._load_seconds,
                num_threads(),
            )
            return self._recognizer

    def transcribe_bytes(self, audio: bytes) -> str:
        recognizer = self.ensure_loaded()
        rate, samples = decode_wav(audio)
        # 单模型实例串行解码：桌面端同时只会有一轮语音输入，批处理不值得引入
        with self._lock:
            stream = recognizer.create_stream()
            stream.accept_waveform(rate, samples)
            recognizer.decode_stream(stream)
            text = stream.result.text
        return _clean_text(text)


_recognizer: LocalRecognizer | None = None


def get_local_recognizer() -> LocalRecognizer:
    global _recognizer
    if _recognizer is None:
        _recognizer = LocalRecognizer()
    return _recognizer


def reset_local_recognizer() -> None:
    """配置或模型路径变更后丢弃缓存，下次识别按新配置重建。"""
    global _recognizer
    _recognizer = None


async def transcribe(audio: bytes, fmt: str) -> str:
    if fmt != "wav":
        raise VoiceError(
            f"本地识别只支持 WAV（收到 {fmt}）；请在前端转成 WAV，或切换到云端识别。",
            status_code=415,
        )
    if not audio:
        raise VoiceError("音频内容为空", status_code=400)
    return await asyncio.to_thread(get_local_recognizer().transcribe_bytes, audio)


def status() -> dict[str, Any]:
    """模型是否就绪（只查文件，不加载模型，不阻塞状态接口）。"""
    directory = model_dir()
    result: dict[str, Any] = {
        "model": MODEL_LABEL,
        "path": str(directory),
        "available": False,
        "reason": None,
    }

    if sherpa_onnx is None:
        result["reason"] = f"sherpa-onnx 不可用：{_IMPORT_ERROR}"
        return result

    missing = [name for name in (MODEL_FILE, TOKENS_FILE) if not (directory / name).is_file()]
    if missing:
        result["reason"] = f"模型文件缺失：{'、'.join(missing)}"
        return result

    load_error = _recognizer.load_error if _recognizer else None
    if load_error:
        result["reason"] = f"模型加载失败：{load_error}"
        return result

    result["available"] = True
    return result


def warmup() -> None:
    get_local_recognizer().ensure_loaded()


async def warmup_async() -> str | None:
    """供 `main.spawn_background` 使用：返回 None 表示成功，字符串表示原因。"""
    try:
        await asyncio.to_thread(warmup)
    except VoiceError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None
