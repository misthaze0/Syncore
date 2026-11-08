"""
LLM 工厂 - 创建 ChatOpenAI 实例
所有 LLM 调用通过这里创建；员工可使用独立的已保存连接，不修改全局配置。
"""

from langchain_openai import ChatOpenAI

from app.config import settings

#: OpenAI SDK 请求头 —— 只保留 gzip / deflate，**不要 br**。
#:
#: openai>=3 默认走 `httpx2`，它的 BrotliDecoder 会给 `brotlicffi` 的
#: `decompress()` 传 `output_buffer_limit`（该库不认这个参数）；Moonshot 等网关
#: 对流式响应回 `Content-Encoding: br` 时，请求在解码阶段直接抛
#: `TypeError: Decompressor.decompress() got an unexpected keyword argument`。
#: 不声明 br 就不会收到 br，这条链路与压缩算法本身无关。
LLM_HTTP_HEADERS = {"Accept-Encoding": "gzip, deflate"}


def create_llm(
    model: str | None = None,
    temperature: float | None = None,
    streaming: bool = True,
    thinking: bool | None = None,
    profile_id: str | None = None,
    **kwargs,
) -> ChatOpenAI:
    """
    创建一个 ChatOpenAI 实例

    Args:
        model: 模型名，默认读 .env
        temperature: 温度参数，默认读 .env
        streaming: 是否流式输出（默认 True）
        thinking: 本轮是否开深度思考。True/False = 显式开关（本轮选项，覆盖配置）；
            None = 跟随 `.env` 的 `LLM_ENABLE_THINKING`
        **kwargs: 透传给 ChatOpenAI 的其他参数（可覆盖 timeout / max_retries）
    """
    options = {
        "timeout": settings.LLM_TIMEOUT_SECONDS,
        "max_retries": settings.LLM_MAX_RETRIES,
        **kwargs,
    }

    model_name = model or settings.LLM_MODEL
    base_url, api_key = settings.LLM_BASE_URL, settings.LLM_API_KEY
    if profile_id:
        from app.llm import profiles

        profile = profiles.find_by_id(profiles.ensure_seeded(), profile_id)
        if not profile or not profile.get("base_url") or not profile.get("api_key"):
            raise ValueError("员工的模型连接不可用，请在模型设置中检查该连接")
        base_url, api_key = profile["base_url"], profile["api_key"]

    # 「深度思考」开关（见 config.LLM_ENABLE_THINKING 的注释：本项对体感速度影响最大）。
    # 参数形态随「模型 × 网关」走策略表（app/llm/thinking.py）：Qwen 系网关发
    # `enable_thinking`，OpenAI 官方发 `reasoning_effort`，GLM 官方发 `thinking.type`，
    # 没有开关的模型不发。只在**显式得出布尔值**时才走这条路径 ——
    # 优先级：调用方（用户本轮选项）> `.env` 配置；两者都没给就不发（「不发」基线）。
    # 走 extra_body：SDK 会把它并到请求体顶层，正是网关期望的位置
    # （直接把字段塞进 JSON 的 `extra_body` 键是无效的 —— 那是 SDK 的概念，不是协议字段）。
    enable_thinking = bool(settings.LLM_ENABLE_THINKING) if thinking is None else thinking
    if enable_thinking is not None and "extra_body" not in options:
        from app.llm.thinking import resolve as resolve_thinking

        fragment = resolve_thinking(model_name, base_url, enable_thinking)
        if fragment:
            options["extra_body"] = fragment

    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model_name,
        temperature=temperature if temperature is not None else settings.LLM_TEMPERATURE,
        streaming=streaming,
        default_headers=LLM_HTTP_HEADERS,
        # 流式调用默认不带回 usage（OpenAI 协议要求显式 include_usage）。
        # 不打开的话 `done.usage` 与用量账本就是空的 —— 那不是「没花钱」，是「没记」。
        stream_usage=streaming,
        **options,
    )
