"""
本地唤醒词 —— sherpa-onnx KWS，完全离线、无 Key。

★ 为什么唤醒放在后端而不是 Electron 原生插件

本机已经装有 `sherpa_onnx`（本地 STT 同款依赖），KWS 模型只有 ~18MB；
放到已有的 FastAPI 进程里，音频只需在「桌面助手渲染层 → 127.0.0.1」之间走一段
WebSocket，既不用打包原生 addon，也不引入第二套模型运行时。

★ 只做「听唤醒词」这一件事

唤醒后的命令收音、STT、对话、TTS 全部复用现有链路。这里不保存音频、不写
日志内容，连接断开即释放；模型和关键词缓存只在进程内。

★ 模型目录

默认与 SenseVoice 同级：`<模型根目录>/sherpa-onnx-kws-zipformer-wenetspeech-…
2024-01-01`，可用 `VOICE_WAKE_MODEL` 指到别处。ONNX Runtime 的 C++ 侧
打不开非 ASCII 路径，因此目录必须是 ASCII。
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np

from app.config import settings
from app.voice.exceptions import VoiceError
from app.voice.local_stt import model_root, num_threads

try:  # 与本地 STT 一样：缺少依赖只影响这条可选路径，不阻塞应用启动
    import sherpa_onnx
except Exception as e:  # noqa: BLE001
    sherpa_onnx = None  # type: ignore[assignment]
    _IMPORT_ERROR: str | None = f"{type(e).__name__}: {e}"
else:
    _IMPORT_ERROR = None

try:
    from pypinyin import pinyin as _pinyin
    from pypinyin.contrib.tone_convert import to_finals_tone, to_initials
except Exception as e:  # noqa: BLE001
    _pinyin = None  # type: ignore[assignment]
    _PINYIN_ERROR: str | None = f"{type(e).__name__}: {e}"
else:
    _PINYIN_ERROR = None

logger = logging.getLogger(__name__)

MODEL_DIR_NAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
MODEL_LABEL = "Zipformer-KWS-3.3M"
DEFAULT_WAKE_WORD = "你好，助手"
SAMPLE_RATE = 16000
MIN_WAKE_WORD_CHARS = 2
MAX_WAKE_WORD_CHARS = 10

# 分数越高越容易命中，阈值越低越灵敏；两者都需要结合实录评估误唤醒。
KEYWORDS_SCORE = 1.0
KEYWORDS_THRESHOLD = 0.25

_WHITESPACE = re.compile(r"[\s\u3000]+")
_PUNCTUATION = re.compile(r"[，。！？、；：,.!?;:…—\-_\"'“”‘’（）()\[\]【】《》<>/\\|·•~`]+")
_HAN_ONLY = re.compile(r"^[\u4e00-\u9fff]+$")

# 同一个 spotter 可能被新旧两条连接短暂共享（新连接顶掉旧连接时）。
# KWS 的 decode 不是可重入的，所有流的推进都串行化；正常情况下只有一条流。
_DECODE_LOCK = threading.Lock()


def model_dir() -> Path:
    """唤醒模型目录：显式配置优先，否则放在本地模型根目录下。"""
    override = str(getattr(settings, "VOICE_WAKE_MODEL", "") or "").strip()
    if override:
        return Path(override).expanduser()
    return model_root() / MODEL_DIR_NAME


def normalize_wake_word(text: str) -> str:
    """
    唤醒词的唯一归一化口径：去空白与标点后只留汉字。

    默认的「你好，助手」存的是带逗号的展示形式，送给 KWS 前必须变成
    「你好助手」。模型是中文 KWS，英文/数字没有对应的词表，直接拒绝，
    比让用户配完却永远唤不醒更好。
    """
    normalized = _PUNCTUATION.sub("", _WHITESPACE.sub("", unicodedata.normalize("NFKC", text or "")))
    if not _HAN_ONLY.fullmatch(normalized):
        raise VoiceError("唤醒词只能是汉字（2–10 个），暂不支持英文或数字。", status_code=400)
    if not (MIN_WAKE_WORD_CHARS <= len(normalized) <= MAX_WAKE_WORD_CHARS):
        raise VoiceError(
            f"唤醒词需要 {MIN_WAKE_WORD_CHARS}–{MAX_WAKE_WORD_CHARS} 个汉字。",
            status_code=400,
        )
    return normalized


def _model_files(directory: Path) -> dict[str, Path | None]:
    """按前缀发现模型文件，避免绑死某一次导出的带 epoch 文件名。"""
    def first(prefix: str) -> Path | None:
        matches = sorted(directory.glob(f"{prefix}*.onnx"))
        return matches[0] if matches else None

    return {
        "encoder": first("encoder"),
        "decoder": first("decoder"),
        "joiner": first("joiner"),
        "tokens": directory / "tokens.txt" if (directory / "tokens.txt").is_file() else None,
    }


def _missing_files(files: dict[str, Path | None]) -> list[str]:
    labels = {"encoder": "encoder*.onnx", "decoder": "decoder*.onnx", "joiner": "joiner*.onnx", "tokens": "tokens.txt"}
    return [labels[key] for key, value in files.items() if value is None]


def _tokens_path() -> Path | None:
    files = _model_files(model_dir())
    return files["tokens"]


def _tokens_table(path: Path) -> set[str]:
    table: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.strip().split()
            if len(parts) == 2:
                table.add(parts[0])
    return table


def _ppinyin(word: str) -> list[str]:
    """
    汉字 → 声母/韵母 token。

    与 `sherpa_onnx.text2token(..., 'ppinyin')` 同一算法；不用它是因为
    那个函数会无条件 import sentencepiece，而 ppinyin 路径根本不需要。
    """
    if _pinyin is None:
        raise VoiceError(f"唤醒词分词不可用：缺少 pypinyin（{_PINYIN_ERROR}）", status_code=503)

    tokens: list[str] = []
    for syllable in (item[0] for item in _pinyin(word)):
        initial = to_initials(syllable, strict=False)
        final = to_finals_tone(syllable, strict=False)
        if not initial and not final:
            tokens.append(syllable)
        else:
            if initial:
                tokens.append(initial)
            if final:
                tokens.append(final)
    return tokens


def _tokenize(word: str) -> list[str]:
    """把汉字转成 KWS 的 ppinyin token；词表缺失时返回空（交给模型加载阶段报错）。"""
    tokens_path = _tokens_path()
    if tokens_path is None:
        return []
    try:
        tokens = _ppinyin(word)
    except VoiceError:
        raise
    except Exception as e:  # noqa: BLE001
        raise VoiceError(f"唤醒词分词失败：{type(e).__name__}: {e}", status_code=400) from e

    table = _tokens_table(tokens_path)
    bad = [token for token in tokens if not token or token == "<unk>" or token not in table]
    if not tokens or bad:
        raise VoiceError("这个词里有本地模型不认识的读音，换一个词试试。", status_code=400)
    return tokens


def check_wake_word(word: str) -> dict[str, Any]:
    """
    校验并归一化唤醒词。

    模型不在时不阻止保存：字符规则仍然有效，分词校验等模型就绪后
    会在第一次启动唤醒时补上（`available/reason` 会如实说明）。
    """
    normalized = normalize_wake_word(word)
    tokens = _tokenize(normalized)
    return {
        "word": normalized,
        "tokens": tokens,
        "keyword": (" ".join(tokens) + f" @{normalized}") if tokens else "",
    }


class WakeSession:
    """一条 WebSocket 对应的 KWS 流：单线程 feed，命中后不再判定。"""

    def __init__(self, spotter: Any, word: str) -> None:
        self.word = word
        self.detected = False
        self._spotter = spotter
        self._stream = spotter.create_stream()

    def feed(self, samples: np.ndarray) -> str:
        if self.detected or samples.size == 0:
            return ""
        with _DECODE_LOCK:
            self._stream.accept_waveform(SAMPLE_RATE, samples)
            while self._spotter.is_ready(self._stream):
                self._spotter.decode_stream(self._stream)
                # 检查每个解码步：后续空结果可能覆盖这一帧中途的关键词。
                text = str(self._spotter.get_result(self._stream) or "").strip()
                if text:
                    self.detected = True
                    self._spotter.reset_stream(self._stream)
                    return text
            return ""

    def reset(self) -> None:
        with _DECODE_LOCK:
            self._spotter.reset_stream(self._stream)
            self.detected = False


class WakeEngine:
    """KWS 惰性单例：模型只在第一次开听时加载，换唤醒词时重建一次。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._spotter: Any = None
        self._word: str | None = None
        self._keywords_dir: Path | None = None
        self._load_error: str | None = None
        self._load_seconds: float | None = None

    @property
    def loaded(self) -> bool:
        return self._spotter is not None

    @property
    def loaded_word(self) -> str | None:
        return self._word

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def ensure_loaded(self, word: str) -> Any:
        normalized = normalize_wake_word(word)
        if self._spotter is not None and self._word == normalized:
            return self._spotter

        with self._lock:
            if self._spotter is not None and self._word == normalized:
                return self._spotter
            if sherpa_onnx is None:
                raise VoiceError(f"本地唤醒不可用：sherpa-onnx 未安装（{_IMPORT_ERROR}）", status_code=503)

            directory = model_dir()
            if not str(directory).isascii():
                raise VoiceError(f"唤醒模型路径必须是 ASCII：{directory}", status_code=503)

            files = _model_files(directory)
            missing = _missing_files(files)
            if missing:
                raise VoiceError(
                    f"唤醒模型未安装：{directory} 缺少 {'、'.join(missing)}",
                    status_code=503,
                )

            tokens = _tokenize(normalized)
            if not tokens:
                raise VoiceError("唤醒模型缺少 tokens.txt，无法生成关键词。", status_code=503)

            # 关键词文件本身可以含中文（@ 后面是展示名），但路径必须在 ASCII 目录下，
            # 否则 Windows 上的 ONNX Runtime 可能打不开。
            keywords_dir = Path(tempfile.mkdtemp(prefix=".xjms-kws-", dir=str(directory.parent)))
            keywords_file = keywords_dir / "keywords.txt"
            keywords_file.write_text(" ".join(tokens) + f" @{normalized}\n", encoding="utf-8")
            started = time.perf_counter()
            try:
                spotter = sherpa_onnx.KeywordSpotter(
                    tokens=str(files["tokens"]),
                    encoder=str(files["encoder"]),
                    decoder=str(files["decoder"]),
                    joiner=str(files["joiner"]),
                    keywords_file=str(keywords_file),
                    num_threads=num_threads(),
                    sample_rate=SAMPLE_RATE,
                    keywords_score=KEYWORDS_SCORE,
                    keywords_threshold=KEYWORDS_THRESHOLD,
                    provider="cpu",
                )
            except Exception as e:  # noqa: BLE001
                shutil.rmtree(keywords_dir, ignore_errors=True)
                self._load_error = f"{type(e).__name__}: {e}"
                raise VoiceError(f"唤醒模型加载失败：{self._load_error}", status_code=503) from e

            self._dispose_locked()
            self._spotter = spotter
            self._word = normalized
            self._keywords_dir = keywords_dir
            self._load_error = None
            self._load_seconds = time.perf_counter() - started
            logger.info(
                "本地唤醒就绪：%s（%s，加载 %.1fs，%s 线程）",
                MODEL_LABEL,
                normalized,
                self._load_seconds,
                num_threads(),
            )
            return spotter

    def create_session(self, word: str) -> WakeSession:
        normalized = normalize_wake_word(word)
        return WakeSession(self.ensure_loaded(normalized), normalized)

    def reset(self) -> None:
        with self._lock:
            self._dispose_locked()
            self._load_error = None

    def _dispose_locked(self) -> None:
        self._spotter = None
        self._word = None
        if self._keywords_dir is not None:
            shutil.rmtree(self._keywords_dir, ignore_errors=True)
            self._keywords_dir = None


_engine: WakeEngine | None = None


def get_engine() -> WakeEngine:
    global _engine
    if _engine is None:
        _engine = WakeEngine()
    return _engine


def reset() -> None:
    get_engine().reset()


def status() -> dict[str, Any]:
    """模型是否就绪（只查文件，不加载模型）。"""
    directory = model_dir()
    result: dict[str, Any] = {
        "model": MODEL_LABEL,
        "path": str(directory),
        "available": False,
        "reason": None,
        "default_word": DEFAULT_WAKE_WORD,
        "word": get_engine().loaded_word or "",
        "loaded": get_engine().loaded,
    }

    if sherpa_onnx is None:
        result["reason"] = f"sherpa-onnx 不可用：{_IMPORT_ERROR}"
        return result
    if not str(directory).isascii():
        result["reason"] = f"模型路径必须是 ASCII：{directory}"
        return result

    missing = _missing_files(_model_files(directory))
    if missing:
        result["reason"] = f"模型文件缺失：{'、'.join(missing)}"
        return result

    load_error = get_engine().load_error
    if load_error:
        result["reason"] = f"模型加载失败：{load_error}"
        return result

    result["available"] = True
    return result
