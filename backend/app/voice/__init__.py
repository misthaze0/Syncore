"""
语音模块 —— Phase 4b：STT（语音输入）+ TTS（语音播报）

★ 选型（全部实测过，不是从文档抄的）

STT：双后端可切换（`VOICE_STT_BACKEND`）：
    - `local`（默认）：sherpa-onnx + SenseVoiceSmall int8，完全离线。
      语音输入是一段未压缩 WAV，走网络上传在慢链路上要十几秒；本地推理
      在同一台机器上只要 1–2 秒。模型放在用户目录，不进仓库。
    - `dashscope`：`qwen3-asr-flash`，走 DashScope **原生** multimodal-generation REST。
      为什么不走 OpenAI 兼容层：官方文档说兼容层「只接受公网可达的音频 URL」，
      实测传 base64 `input_audio` 直接报 400（"The provided URL does not appear
      to be valid"）。原生接口的 `audio` 字段接受 `data:audio/xxx;base64,…` URI，
      一次 POST 搞定。

TTS：双后端可插拔（与精排 `api/none` 同一模式）：
    - `dashscope`（默认）：`qwen3-tts-flash`，原生 REST 非流式合成，
      返回 24h 有效的音频 URL → 服务端立即下载转交前端（前端不碰 OSS 链接，
      也不暴露 key）。效果最好。
    - `edge`：`edge-tts`（微软 Edge 朗读接口，免费、无需密钥）。
      网关欠费 / 不想花钱时的兜底，效果略逊但完全可用。

★ 与其余链路的关系

语音是**入口与出口的增强**，不在 Agent 主链路上：STT 在请求进入 `/api/chat`
之前把音频变成文字，TTS 在回答落库之后把文字变成音频。因此本模块刻意
不接 LangGraph、不接检索 —— 它挂了就是「按钮不可用」，问答照常工作。

★ 失败语义

与记忆层同一原则（ADR 0010）：语音是增益不是依赖。
所有对外方法失败时抛 `VoiceError`，由 API 层转成 4xx/503；
启动日志如实报告能力状态，但**绝不让语音初始化失败阻塞应用**。
"""

from app.voice.exceptions import VoiceError
from app.voice.stt import SpeechRecognizer, transcribe
from app.voice.tts import DashscopeTTS, EdgeTTS, synthesize, tts_status

__all__ = [
    "VoiceError",
    "SpeechRecognizer",
    "DashscopeTTS",
    "EdgeTTS",
    "transcribe",
    "synthesize",
    "tts_status",
]
