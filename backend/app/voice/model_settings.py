"""
语音模型的运行时设置 —— STT / TTS 的模型与音色

★ 为什么语音是第三个域（与 llm / rag 并列）

覆写文件是所有域共用的，按域收窄后「恢复默认」只摘自己那一组键 ——
在语音页点恢复，不该把用户换的回答模型或精排配置一起带回去。

★ 生效路径

TTS 引擎与两个 STT 识别器（云端 / 本地）都是**惰性单例**
（构造时捏住 model / voice / api_key / 模型目录），所以 update / reset
在落盘后必须让缓存失效 —— 下一次合成/识别按新配置重建。漏了这步会出现
「界面显示已生效、实际还在用旧配置」的静默漂移。

★ STT 与 TTS 的密钥都来自 `LLM_API_KEY`（DashScope 同一账号体系），
所以本域不提供独立的 Key 把手 —— 换供应商去「对话模型」页，
本页只管「用哪个模型、哪个音色」。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List

from app.config import (
    RUNTIME_OVERRIDE_SPEC,
    apply_persisted_overrides,
    load_runtime_overrides,
    override_status,
    remove_runtime_overrides,
    save_runtime_overrides,
    settings,
)

logger = logging.getLogger(__name__)

#: 语音域可运行时覆写的键。语速（VOICE_TTS_RATE）只在 edge 后端有意义，
#: 一并给出来——界面上按后端切换显示。
VOICE_OVERRIDE_KEYS = frozenset(
    {
        "VOICE_STT_BACKEND",
        "VOICE_STT_MODEL",
        "VOICE_TTS_BACKEND",
        "VOICE_TTS_MODEL",
        "VOICE_TTS_VOICE",
        "VOICE_TTS_VOICE_EDGE",
        "VOICE_TTS_RATE",
    }
)

VALID_STT_BACKENDS = ("local", "dashscope")
VALID_TTS_BACKENDS = ("dashscope", "edge")

_MAX_MODEL_CHARS = 200


class VoiceSettingsError(ValueError):
    """切换参数不合法（接口层转成 400，消息直接给人看）"""


def _coerce_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    from app.config import _coerce

    out: Dict[str, Any] = {}
    for key, raw in patch.items():
        want = _VOICE_SPEC.get(key)
        if want is None:
            raise VoiceSettingsError(
                f"不支持在运行时修改 '{key}'。可改的项：{', '.join(sorted(VOICE_OVERRIDE_KEYS))}"
            )
        try:
            out[key] = _coerce(raw, want)
        except (TypeError, ValueError) as e:
            raise VoiceSettingsError(f"'{key}' 的值不合法: {e}") from e
    return out


#: 本域的强转表，直接取 config 的总表（避免两份键清单漂移）
_VOICE_SPEC: Dict[str, type] = {
    key: RUNTIME_OVERRIDE_SPEC[key] for key in VOICE_OVERRIDE_KEYS if key in RUNTIME_OVERRIDE_SPEC
}


def _local_status() -> Dict[str, Any]:
    from app.voice.local_stt import status as local_status

    return local_status()


def _validate(effective: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []

    stt_backend = str(effective.get("VOICE_STT_BACKEND", "") or "").strip().lower()
    if stt_backend not in VALID_STT_BACKENDS:
        raise VoiceSettingsError(
            f"VOICE_STT_BACKEND 只能是 {' / '.join(VALID_STT_BACKENDS)}，收到 '{stt_backend}'"
        )

    # 云端模型名只在切到 dashscope 时需要；切回本地后留空也不该拦住保存
    stt_model = str(effective.get("VOICE_STT_MODEL", "") or "").strip()
    if stt_backend == "dashscope" and not stt_model:
        raise VoiceSettingsError("VOICE_STT_MODEL 不能为空（dashscope 后端）")
    if len(stt_model) > _MAX_MODEL_CHARS:
        raise VoiceSettingsError(f"VOICE_STT_MODEL 过长（>{_MAX_MODEL_CHARS} 字符）")

    if stt_backend == "local":
        local = _local_status()
        if not local.get("available"):
            warnings.append(
                f"本地识别当前不可用：{local.get('reason')}。"
                "可切换到云端，或安装模型后重新保存。"
            )

    tts_model = str(effective.get("VOICE_TTS_MODEL", "") or "").strip()
    if not tts_model:
        raise VoiceSettingsError("VOICE_TTS_MODEL 不能为空")
    if len(tts_model) > _MAX_MODEL_CHARS:
        raise VoiceSettingsError(f"VOICE_TTS_MODEL 过长（>{_MAX_MODEL_CHARS} 字符）")

    backend = str(effective.get("VOICE_TTS_BACKEND", "") or "").strip().lower()
    if backend not in VALID_TTS_BACKENDS:
        raise VoiceSettingsError(
            f"VOICE_TTS_BACKEND 只能是 {' / '.join(VALID_TTS_BACKENDS)}，收到 '{backend}'"
        )

    if backend == "dashscope":
        if not str(effective.get("VOICE_TTS_VOICE", "") or "").strip():
            raise VoiceSettingsError("VOICE_TTS_VOICE 不能为空（dashscope 后端）")
    else:
        if not str(effective.get("VOICE_TTS_VOICE_EDGE", "") or "").strip():
            raise VoiceSettingsError("VOICE_TTS_VOICE_EDGE 不能为空（edge 后端）")

    needs_key = stt_backend == "dashscope" or backend == "dashscope"
    if needs_key and (
        not settings.LLM_API_KEY or settings.LLM_API_KEY == "sk-placeholder"
    ):
        warnings.append("云端识别 / DashScope TTS 需要 LLM_API_KEY，当前未配置。")

    rate = str(effective.get("VOICE_TTS_RATE", "") or "").strip()
    if rate and not (rate.startswith(("+", "-")) and rate.endswith("%")):
        raise VoiceSettingsError(f"VOICE_TTS_RATE 形如 '+0%' / '-10%'，收到 '{rate}'")

    return warnings


def state() -> Dict[str, Any]:
    """当前生效配置 + 覆写现状（只读，不发请求）"""
    store = override_status(VOICE_OVERRIDE_KEYS)
    return {
        "config": {k: getattr(settings, k, None) for k in sorted(VOICE_OVERRIDE_KEYS)},
        "store": store,
        "enabled": settings.VOICE_ENABLED,
        # TTS 可用性依赖 Key（dashscope）；edge 永远可用 —— 界面据此提示
        "api_key_set": bool(settings.LLM_API_KEY) and settings.LLM_API_KEY != "sk-placeholder",
        # 本地模型是否就绪：界面据此提示「先装模型再切本地」
        "local_stt": _local_status(),
    }


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    """部分更新语音配置：校验 → 落盘 → 生效（重建引擎/识别器单例）→ 重新算状态"""
    patch = {k: v for k, v in patch.items() if v is not None}
    if not patch:
        raise VoiceSettingsError("没有需要修改的配置项")

    coerced = _coerce_patch(patch)
    persisted = {**load_runtime_overrides(), **coerced}
    effective = {k: getattr(settings, k, None) for k in VOICE_OVERRIDE_KEYS}
    effective.update(coerced)

    warnings = _validate(effective)
    save_runtime_overrides(persisted)
    apply_persisted_overrides()
    _reset_singletons()

    result = state()
    result["applied"] = sorted(coerced)
    result["warnings"] = warnings
    return result


def reset() -> Dict[str, Any]:
    """清空语音域的覆写 → 回到 backend/.env 的值（不动 llm / rag 域）"""
    remove_runtime_overrides(VOICE_OVERRIDE_KEYS)
    apply_persisted_overrides()
    _reset_singletons()
    result = state()
    result["applied"] = []
    result["warnings"] = []
    return result


def _reset_singletons() -> None:
    """配置变了就丢弃引擎/识别器缓存 —— 下次合成/识别按新配置重建"""
    from app.voice.local_stt import reset_local_recognizer
    from app.voice.stt import reset_recognizer
    from app.voice.tts import reset_engine

    reset_engine()
    reset_recognizer()
    reset_local_recognizer()


# ---------------------------------------------------------------- 探测


async def probe(timeout: float = 60.0) -> Dict[str, Any]:
    """
    用当前配置真合成一小段语音，回报能不能出声、多快、多大。

    STT 没有探测 —— 它需要真实音频输入，「配置对不对」要用户按住麦克风说一句才知道；
    界面如实只给 TTS 的探测。失败不抛异常：失败本身就是答案。
    """
    from app.voice.tts import DashscopeTTS, get_engine, synthesize, tts_status

    started = time.perf_counter()
    first_audio_ms: float | None = None
    byte_count = 0
    info = tts_status()

    async def measure():
        nonlocal first_audio_ms, byte_count
        engine = get_engine()
        if isinstance(engine, DashscopeTTS):
            async for chunk in engine.stream("你好，这是语音探测。", use_cache=False):
                if first_audio_ms is None:
                    first_audio_ms = round((time.perf_counter() - started) * 1000, 1)
                byte_count += len(chunk)
        else:
            result = await synthesize("你好，这是语音探测。")
            byte_count = len(result.get("audio") or b"")
            first_audio_ms = round((time.perf_counter() - started) * 1000, 1)

    try:
        await asyncio.wait_for(measure(), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        ms = round((time.perf_counter() - started) * 1000, 1)
        logger.warning("TTS 探测失败: %s: %s", type(e).__name__, e)
        return {
            "ok": False,
            "backend": settings.VOICE_TTS_BACKEND,
            "model": info.get("model", settings.VOICE_TTS_MODEL),
            "ms": ms,
            "error": f"{type(e).__name__}: {e}",
        }

    ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "ok": byte_count > 0,
        "backend": info["backend"],
        "model": info.get("model", settings.VOICE_TTS_MODEL),
        "voice": info.get("voice"),
        "bytes": byte_count,
        "first_audio_ms": first_audio_ms,
        "streaming": bool(info.get("streaming")),
        "ms": ms,
    }


__all__ = [
    "VOICE_OVERRIDE_KEYS",
    "VoiceSettingsError",
    "probe",
    "reset",
    "state",
    "update",
]
