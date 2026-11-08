"""
语音模块的异常与纯函数工具 —— 无 IO、无依赖，方便单测。
"""

from __future__ import annotations

import re


class VoiceError(Exception):
    """
    语音链路的统一异常。

    API 层把它转成 4xx/503，而不是让 FastAPI 兜底成 500 ——
    「音频格式不支持」（调用方的错）和「网关不可用」（服务的错）
    是两种不同的语义，混在一起会让人误判问题在哪一侧。
    """

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


# ASR 支持的音频容器（DashScope 文档口径；webm 不在列表里，所以前端要转 WAV）
SUPPORTED_AUDIO_FORMATS = {"wav", "mp3", "aac", "flac", "opus", "ogg", "amr"}

# content-type → 扩展名（multipart 上传时用；没有 content-type 的浏览器极少见，兜底 wav）
_CONTENT_TYPE_EXT = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "aac",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/ogg": "ogg",
    "audio/webm": "webm",  # 会被 ext_of 之外的校验拦下并给出明确报错
}

# TTS 单次合成上限（qwen3-tts 512 token ≈ 600 字符；edge-tts 无硬限但保持一致）
TTS_MAX_TEXT_CHARS = 600

# 朗读前要清掉的「读出来很怪」的内容：Markdown 强调 / 代码块 / 链接 / 表格竖线
_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BOLD_ITALIC = re.compile(r"[*_~]{1,3}")
_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_LIST_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_TABLE_PIPE = re.compile(r"\|")
_URL = re.compile(r"https?://\S+")


def ext_of(*, filename: str | None = None, content_type: str | None = None) -> str:
    """从文件名或 content-type 推断音频扩展名（都推不出来时返回 'wav'）"""
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext in SUPPORTED_AUDIO_FORMATS:
            return ext
    if content_type:
        ext = _CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip().lower())
        if ext:
            return ext
    return "wav"


def check_audio_format(ext: str) -> None:
    """不支持的格式（如浏览器默认的 webm）要给**可行动**的报错，而不是让网关报 500"""
    if ext not in SUPPORTED_AUDIO_FORMATS:
        raise VoiceError(
            f"不支持的音频格式 '{ext}'（支持: {', '.join(sorted(SUPPORTED_AUDIO_FORMATS))}）。"
            "浏览器录音通常是 webm，请先转成 wav/mp3。",
            status_code=415,
        )


def clean_text_for_tts(text: str) -> str:
    """
    把 Markdown 回答清理成适合朗读的纯文本。

    代码块读出来是灾难（`def foo(self):` 会被逐字念出），
    链接念 URL 同样不可接受 —— 直接剔除，保留自然语言部分。
    """
    out = _CODE_BLOCK.sub("（代码）", text)
    out = _IMAGE.sub("", out)
    out = _LINK.sub(r"\1", out)
    out = _INLINE_CODE.sub("", out)
    out = _URL.sub("（链接）", out)
    out = _HEADING.sub("", out)
    out = _LIST_BULLET.sub("", out)
    out = _TABLE_PIPE.sub(" ", out)
    out = _BOLD_ITALIC.sub("", out)
    # 压掉连续空白（含换行），朗读不需要它们
    out = re.sub(r"\s+", " ", out).strip()
    return out


def truncate_for_tts(text: str, limit: int = TTS_MAX_TEXT_CHARS) -> tuple[str, bool]:
    """超长文本截断到 limit（按字符）。返回 (文本, 是否截断)。"""
    if len(text) <= limit:
        return text, False
    return text[:limit], True
