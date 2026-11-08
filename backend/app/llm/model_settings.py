"""
语言模型的运行时设置 —— 查看、切换、自检

★ 它和「输入框那个模型下拉」现在是同一件事

模型下拉（2026-10-20 改版后）走的是 `PUT /api/llm/models` —— 点谁 = 谁成为
**全局默认**，不再有「本轮覆盖」那种只影响一句话的语义。
后端 `POST /api/chat` 的 `model` 字段仍保留（员工私聊等入口还在用），
但主对话前端已经不再发送它。

两者的差别因此只剩**入口**，不再有**影响面**：

    对话里选：立刻生效，标题生成、记忆抽取、图谱抽取、评估裁判都跟着换
    设置页改：同一份 `settings.LLM_MODEL`，同一影响面

所以界面上必须写清「影响所有默认调用」，否则用户会以为换了模型但发现
侧栏标题、记忆抽取还是老样子。

★ 为什么复用检索模型那套覆写层，而不是自己写一个文件

`data/runtime_config.json` 已经回答了「谁在什么时候把哪个值改掉了」，
再开一份就是第二个事实来源。两边写同一个文件时有三条硬约束（都在 `app/config.py`）：
白名单、`extra="forbid"` 的接口模型、写入用原子替换。
这里只需要额外做一件检索域不做的事：**按域收窄**。覆写文件是两个面板共用的，
任一面板的「恢复默认」都只该摘掉自己那一组键 —— 在语言模型页点一下
就把用户的精排候选数回归 `.env`，属于「安静地改掉了他没在看的设置」。

★ 为什么没有向量库自证（对比 `app/rag/model_settings.py`）

换 embedding 会换向量空间，库里的旧向量会变成噪声，必须自证 + 重建；
换 LLM 只是「下一轮改由另一个模型开口」—— 已入库的向量、已生成的回答
都不会因此失效，所以这里只有「切换」与「自检」，没有「重建」。

★ 为什么不把温度 / 思考开关也做成可切

两者都是「值合法但效果不可逆地影响体感」的键：思考一开，首字从 0.4 秒变成
十几秒（见 `config.LLM_ENABLE_THINKING` 的注释），用户很可能把它当成「换了模型就变慢了」。
它们仍然在 `.env` 里可配，这里只**只读展示**当前生效值，不提供把手。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Tuple

from app.config import (
    RUNTIME_OVERRIDE_SPEC,
    apply_persisted_overrides,
    env_baseline,
    load_runtime_overrides,
    mask_secret,
    override_status,
    remove_runtime_overrides,
    save_runtime_overrides,
    settings,
)
from app.core.llm_factory import LLM_HTTP_HEADERS
from app.llm import profiles, thinking

logger = logging.getLogger(__name__)

#: 语言模型域可运行时覆写的键。
#:
#: - `LLM_MODEL`：回答用哪个模型（最初的、也是最高的那个把手）；
#: - `LLM_BASE_URL` / `LLM_API_KEY`：**供应商接入**——设置页选一个预设供应商
#:   （DeepSeek / Kimi / 百炼…）填入 Key 后，这两个键随之改写，
#:   `create_llm()` 每次调用都读它们，改完下一轮即生效；
#: - `LLM_AVAILABLE_MODELS`：候选清单（逗号分隔）——「添加模型」按钮维护的就是它。
#:
#: ⚠️ `LLM_API_KEY` 会以**明文**落在 `data/runtime_config.json`（与 .env 同级的安全边界：
#: 本机单用户桌面应用）；各供应商**各自的** Key 同样明文落在 `data/llm_profiles.json`
#: （见 `app/llm/profiles.py` 模块头）。刻意不返回给前端明文——state() 只给 `api_key_set` 与掩码。
#:
#: 刻意**不含** `LLM_TEMPERATURE` / `LLM_ENABLE_THINKING`（理由见模块头）。
LLM_OVERRIDE_KEYS = frozenset(
    {"LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_AVAILABLE_MODELS"}
)

#: 模型名的长度上限。网关的模型 id 都很短，超过这个长度只可能是粘错了东西。
_MAX_MODEL_CHARS = 200

#: 自检用的提示词。要极短：自检证明的是「这条路通不通、多快」，不是模型好不好。
_PROBE_PROMPT = "请只回复两个字：可用"
#: 自检回复在界面上的展示上限（太长会把面板撑开，也没人读）
_PROBE_REPLY_CHARS = 120


class LlmModelSettingsError(ValueError):
    """切换参数不合法（接口层转成 400，消息直接给人看）"""


# ---------------------------------------------------------------- 状态


def _coerce_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    按**语言模型域**白名单强转类型。

    `_coerce` 里 `bool` 要单独处理（`bool("false") is True`）——这里目前只有字符串键，
    但共用同一个强转函数，将来加键不必再想一次这件事。
    """
    from app.config import _coerce

    out: Dict[str, Any] = {}
    for key, raw in patch.items():
        want = RUNTIME_OVERRIDE_SPEC.get(key) if key in LLM_OVERRIDE_KEYS else None
        if want is None:
            raise LlmModelSettingsError(
                f"不支持在运行时修改 '{key}'。可改的项：{', '.join(sorted(LLM_OVERRIDE_KEYS))}"
            )
        try:
            out[key] = _coerce(raw, want)
        except (TypeError, ValueError) as e:
            raise LlmModelSettingsError(f"'{key}' 的值不合法: {e}") from e
    return out


def _validate(effective: Dict[str, Any]) -> List[str]:
    """
    语义校验（不合法直接抛；可疑但合法只返回警告）。

    `effective` 是「当前生效值 + 本次改动」合并后的视图 —— 校验回答的是
    「改完之后这套配置合不合法」。

    「模型名网关认不认」只有真调一次才知道（自检按钮的事），这里只拦
    **结构上就错了**的输入：空的模型名 / 不是 http(s) 的 Base URL /
    空名字混进候选清单。
    """
    model = str(effective.get("LLM_MODEL", "")).strip()
    if not model:
        raise LlmModelSettingsError("LLM_MODEL 不能为空")
    if len(model) > _MAX_MODEL_CHARS:
        raise LlmModelSettingsError(f"LLM_MODEL 过长（>{_MAX_MODEL_CHARS} 字符）")

    base_url = str(effective.get("LLM_BASE_URL", "") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise LlmModelSettingsError(f"Base URL 必须以 http:// 或 https:// 开头：{base_url}")

    names = [
        n.strip()
        for n in str(effective.get("LLM_AVAILABLE_MODELS", "") or "").split(",")
        if n.strip()
    ]
    for name in names:
        if len(name) > _MAX_MODEL_CHARS:
            raise LlmModelSettingsError(f"候选清单里有超过 {_MAX_MODEL_CHARS} 字符的模型名：{name[:40]}…")

    warnings: List[str] = []
    # ⚠️ 用 `declared_models` 而不是 `available_models`：后者会把当前 LLM_MODEL
    # 自动插进清单（它保证「默认用的那个一定在候选里」），拿它判断等于永远为真。
    # 本次改动带了候选清单时，以改完的为准判断（否则刚添加的模型会被误报）。
    if "LLM_AVAILABLE_MODELS" in effective:
        declared = names
    else:
        declared = settings.declared_models
    if declared and model not in declared:
        warnings.append(
            f"'{model}' 不在候选清单里（已声明：{', '.join(declared[:8])}"
            f"{'…' if len(declared) > 8 else ''}）。本设置照常生效；"
            f"若不是你有意手填，请确认模型名没写错。"
        )
    return warnings


# ---------------------------------------------------------------- 连通性测试（草稿参数，不落盘）


def _mask_key(key: str) -> str:
    """API Key 的展示掩码 —— 委托 `config.mask_secret`（全项目唯一一份掩码实现）"""
    return mask_secret(key)


async def test_connection(patch: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
    """
    用**草稿参数**真打一次模型 —— 前端「测试连接」按钮的后端。

    ★ 与 `probe()` 的分工

    probe 测的是「**已保存**的配置」；这里测的是「**表单里还没保存**的参数」。
    「Key 填了并且连接成功才允许保存」这条产品规则的前提，就是测试这个动作
    绝不能先落盘 —— 所以本函数不经过 update()，不碰 runtime_config.json。

    ★ 参数语义

    `patch` 只认 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 三个键（多余键忽略）；
    缺省的键回退到**当前生效值** —— 这样「只改了模型名」或「只换 Base URL」
    也能用现有 Key 完整测一遍，不必让用户重复粘贴密钥。

    ★ 为什么不走 `create_llm` 也不发 `enable_thinking`

    create_llm 读的是全局 settings（已保存的值），草稿参数必须手动建 ChatOpenAI；
    `enable_thinking` 是 Qwen 系网关的扩展参数，草稿配置可能指向任意供应商
    （Moonshot / OpenRouter / Ollama…），测试走**纯 OpenAI 兼容基线**才公平。

    ★ 为什么温度读 `LLM_TEMPERATURE` 而不是写死 0

    写死 0 会误杀只接受单一温度的模型：kimi-k2.7-code 实测
    `temperature != 1` 直接 400（`invalid temperature: only 1 is allowed
    for this model`）。回答链路（runner）同样跟随 `LLM_TEMPERATURE`，
    测试与真实调用用同一个温度来源，测试结果才预测得准。
    用这类模型时把 `.env` 的 `LLM_TEMPERATURE` 配成它接受的值（Kimi K2.7 = 1）。

    ★ 为什么不设 max_tokens

    与 probe() 同因：本机网关在输出上限下会把预算花在思考上、正文返回空。
    提示词本身只有十几个 token，放开上限也不会烧钱。

    失败不抛异常（除参数结构性非法）：失败原因（401 / 模型名不认识 / 超时）
    原样返回给界面，与 probe() 同一约定。
    """
    model = str(patch.get("LLM_MODEL") or settings.LLM_MODEL or "").strip()
    if not model:
        raise LlmModelSettingsError("模型名不能为空")

    base_url = str(patch.get("LLM_BASE_URL") or settings.LLM_BASE_URL or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise LlmModelSettingsError(f"Base URL 必须以 http:// 或 https:// 开头：{base_url}")

    api_key = str(patch.get("LLM_API_KEY") or "").strip()
    # ★ 回退链按**草稿 Base URL** 查 Key，不再直接拿全局那把
    #
    # 旧实现回退 `settings.LLM_API_KEY`：测 DeepSeek 会拿百炼 Key 去敲 DeepSeek
    # 的门（401 还算好的，行为本身是错的）。新链：草稿 Key → profile 目录里该
    # Base URL 的 Key → （仅当草稿 Base URL 就是当前生效的）生效 Key → 占位符。
    if not api_key:
        api_key = profiles.key_for_base_url(base_url)
    if not api_key and base_url == str(settings.LLM_BASE_URL or "").strip():
        api_key = str(settings.LLM_API_KEY or "").strip()
    # OpenAI SDK 要求非空 api_key；没填时给占位符让请求照发——
    # 服务端会回 401，而 401 的错误信息正是界面上最有用的「Key 没配对」。
    if not api_key:
        api_key = "EMPTY"

    started = time.perf_counter()
    try:
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            base_url=base_url or None,
            api_key=api_key,
            model=model,
            temperature=settings.LLM_TEMPERATURE,
            timeout=timeout,
            max_retries=0,
            streaming=False,
            default_headers=LLM_HTTP_HEADERS,
        )
        message = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content=_PROBE_PROMPT)]), timeout=timeout
        )
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = str(content)
        try:
            from app.agents.think import split_think

            reasoning, answer = split_think(content)
        except Exception:  # noqa: BLE001
            reasoning, answer = "", content
    except Exception as e:  # noqa: BLE001
        ms = round((time.perf_counter() - started) * 1000, 1)
        logger.warning("连通性测试失败（%s @ %s）: %s: %s", model, base_url, type(e).__name__, e)
        return {
            "ok": False,
            "model": model,
            "base_url": base_url,
            "api_key_masked": _mask_key(api_key if api_key != "EMPTY" else ""),
            "ms": ms,
            "error": f"{type(e).__name__}: {e}",
        }

    ms = round((time.perf_counter() - started) * 1000, 1)
    out: Dict[str, Any] = {
        "ok": bool(answer.strip()),
        "model": model,
        "base_url": base_url,
        "api_key_masked": _mask_key(api_key if api_key != "EMPTY" else ""),
        "reply": answer.strip()[:_PROBE_REPLY_CHARS],
        "reasoning_chars": len(reasoning),
        "ms": ms,
    }
    if not answer.strip():
        out["error"] = (
            f"模型返回了内容但正文为空（推理正文 {len(reasoning)} 字）—— "
            f"可能是该模型只输出思考过程、或网关把预算花在了推理上。"
        )
    return out


# ---------------------------------------------------------------- 供应商模型清单（草稿参数，不落盘）


async def list_remote_models(patch: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
    """
    用草稿参数问供应商「你家有哪些模型」—— `GET {base_url}/models`。

    设置页「默认模型」下拉的数据源。Key 的回退链与 `test_connection` 完全一致
    （草稿 Key → 目录里该 Base URL 的 Key → 仅当同一条生效连接时全局 Key），
    所以「只换了供应商」不必重贴密钥就能拉清单。

    与 test_connection 同一约定：永远 200 形状返回，拉不到就把原因放进 `error`
    （Ollama 等本地网关没有 `/models` 或返回形状各异，下拉回退到手填即可，
    不值得为此报错）。
    """
    base_url = str(patch.get("LLM_BASE_URL") or settings.LLM_BASE_URL or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise LlmModelSettingsError(f"Base URL 必须以 http:// 或 https:// 开头：{base_url}")

    api_key = str(patch.get("LLM_API_KEY") or "").strip()
    if not api_key:
        api_key = profiles.key_for_base_url(base_url)
    if not api_key and base_url == str(settings.LLM_BASE_URL or "").strip():
        api_key = str(settings.LLM_API_KEY or "").strip()

    import httpx

    url = base_url.rstrip("/") + "/models"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {api_key or 'EMPTY'}"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "models": [],
            "base_url": base_url,
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"{type(e).__name__}: {e}",
        }

    items = data.get("data") if isinstance(data, dict) else None
    ids: List[str] = []
    if isinstance(items, list):
        for item in items:
            mid = str(item.get("id", "") if isinstance(item, dict) else item).strip()
            if mid and mid not in ids:
                ids.append(mid)
    return {
        "ok": True,
        "models": ids,
        "base_url": base_url,
        "ms": round((time.perf_counter() - started) * 1000, 1),
    }


# ---------------------------------------------------------------- 状态


def _profile_label(profile: Dict[str, Any]) -> str:
    """供应商在清单里显示的名字：预设用它自己的名字，自定义端点用域名"""
    if profile.get("builtin"):
        return str(profile.get("name") or profile.get("id") or "")
    from urllib.parse import urlparse

    host = urlparse(str(profile.get("base_url") or "")).hostname or ""
    return host or str(profile.get("name") or profile.get("id") or "")


def usable_models(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    对话输入框能选的模型 = **有 Key 的连接里保存过的模型**。

    没配 Key 的供应商即使目录里有名字也用不了 —— 列进下拉等于给一个
    必然 401 的选项。当前生效模型如果不在任何清单里（手改 `.env`、
    首次启动），补在最前面：这颗钮至少要能回答「现在用的是谁」。
    """
    current = settings.LLM_MODEL
    current_base = str(settings.LLM_BASE_URL or "").strip()
    out: List[Dict[str, Any]] = []
    for p in catalog["profiles"]:
        if not p["api_key"]:
            continue
        for name in p["models"]:
            out.append(
                {
                    "id": name,
                    "label": name,
                    "default": name == current and p["base_url"] == current_base,
                    "provider": _profile_label(p),
                    "profile_id": p["id"],
                    # 「深度思考」在这个连接上能不能开/关 —— 前端开关的禁用态数据源
                    "thinking": thinking.capability(name, str(p.get("base_url") or "")),
                }
            )
    if current and not any(m["default"] for m in out):
        hit = profiles.find_by_base_url(catalog, current_base)
        out.insert(
            0,
            {
                "id": current,
                "label": current,
                "default": True,
                "provider": _profile_label(hit) if hit else current_base,
                "profile_id": hit["id"] if hit else None,
                "thinking": thinking.capability(current, current_base),
            },
        )
    return out


def state() -> Dict[str, Any]:
    """
    面板需要的全部事实：当前生效模型 + 候选清单 + 覆写现状 + 只读运行参数。

    ★ `models` 与它自己的 `default` 标记会跟着覆写变

    `settings.available_models` 把 `LLM_MODEL` 插在清单首位并标 `default` ——
    所以覆写生效后，**顶栏候选的默认项也跟着换了**。这是对的：
    顶栏问的是「默认用哪个」，它必须和这里的答案一致。

    ★ API Key 只给掩码，不给明文

    设置页需要的是「配没配过、是不是这份」，不是把密钥再抄一遍。
    明文只存在于 runtime_config.json 与实际调用里 —— GET 接口不回传，
    前端的 Key 输入框永远是空的占位（「已配置 · 留空保持不变」）。

    只读、不发任何请求：自检是另一个按钮的事。
    """
    store = override_status(LLM_OVERRIDE_KEYS)
    current = settings.LLM_MODEL
    baseline = env_baseline().get("LLM_MODEL") or current
    baseline_key = env_baseline().get("LLM_API_KEY") or ""

    # ★ profile 目录：每个供应商各自的 Key（只给掩码）—— 设置页的 chip 按它渲染
    #
    # `active_id` 要与生效 Base URL 对账：不一致（「恢复默认」之后、或手改了
    # `.env`）就如实报 null —— 界面据此知道「生效连接不在目录里」，
    # 而不是把一个 Base URL 对不上的 profile 高亮成当前供应商。
    profile_catalog = profiles.ensure_seeded()
    active_profile_id = profile_catalog.get("active_id")
    active_profile = profiles.find_by_id(profile_catalog, active_profile_id or "")
    if active_profile is None or active_profile["base_url"] != str(settings.LLM_BASE_URL or ""):
        active_profile_id = None
    preset_models = {p["id"]: list(p["models"]) for p in profiles.PRESET_PROFILES}
    saved_names = profiles.all_saved_models(profile_catalog)
    usable = usable_models(profile_catalog)
    profile_views = [
        {
            "id": p["id"],
            "name": p["name"],
            "base_url": p["base_url"],
            "key_url": p["key_url"],
            # 「已保存的模型」= 这家连接实际保存过的；在对话下拉里可用
            "models": list(p["models"]),
            # 预设建议：只用于「默认模型」下拉在拉不到供应商清单时兜底，不落盘
            "preset_models": preset_models.get(p["id"], []),
            "builtin": p["builtin"],
            "api_key_present": bool(p["api_key"]),
            "api_key_masked": _mask_key(p["api_key"]),
        }
        for p in profile_catalog["profiles"]
    ]

    return {
        "model": current,
        "default": baseline,
        "overridden": "LLM_MODEL" in store["active"],
        "models": usable,
        "selectable": len(usable) > 1,
        # 供应商接入（base_url / api_key / 候选清单）—— 见 LLM_OVERRIDE_KEYS 的说明
        "base_url": settings.LLM_BASE_URL,
        "base_url_overridden": "LLM_BASE_URL" in store["active"],
        "api_key_set": bool(settings.LLM_API_KEY) and settings.LLM_API_KEY != baseline_key,
        # 与 api_key_set 的分工：set = 「运行时覆写了 key」，present = 「有 key 可用」
        # （含跟随 .env 的情形）。界面状态卡要回答「能不能用」该看 present，
        # 只看 set 会把「.env 里配好了但没覆写」误报成「尚未配置」。
        "api_key_present": bool(settings.LLM_API_KEY),
        "api_key_masked": _mask_key(settings.LLM_API_KEY),
        # 供应商目录（各自的 Key 掩码）与当前生效的 profile —— 见 `app/llm/profiles.py`
        "profiles": profile_views,
        "active_profile_id": active_profile_id,
        "available_models_declared": ", ".join(saved_names),
        "in_catalog": (not saved_names) or current in saved_names,
        "config": {k: getattr(settings, k, None) for k in sorted(LLM_OVERRIDE_KEYS) if "KEY" not in k},
        "store": store,
        # 只读运行事实：界面上要能回答「这句话最终是谁在听」，
        # 而不只是「配置里写的是什么」。
        "runtime": {
            "base_url": settings.LLM_BASE_URL,
            "temperature": settings.LLM_TEMPERATURE,
            # None = 不发 `enable_thinking` 字段（跟随网关默认），与
            # `llm_factory.create_llm` 的判断逐字一致 —— 界面不猜，只照抄。
            "thinking": settings.LLM_ENABLE_THINKING,
            "timeout_seconds": settings.LLM_TIMEOUT_SECONDS,
        },
    }


# ---------------------------------------------------------------- 切换


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    部分更新语言模型配置：校验 → 落盘 → 生效 → 重新算状态。

    `persisted` 是**当前覆写 + 本次改动**（不是只有本次改动）：检索域那几项
    必须原样留在文件里，否则「换一次回答模型」会把用户的精排设置一起抹掉。

    不需要让任何缓存失效：`create_llm()` 每次都读 `settings.LLM_MODEL`，
    这正是「换这里 = 换全局默认」能做到即时生效的原因（对比 embedding 的单例缓存）。
    """
    patch = {k: v for k, v in patch.items() if v is not None}
    if not patch:
        raise LlmModelSettingsError("没有需要修改的配置项")

    # 表单属于哪个 profile（前端 chip 送的）；缺省 = 后端按 Base URL 匹配/新建。
    # 必须先 pop：它不是覆写层的键，留在 patch 里会被白名单拒掉。
    profile_id = patch.pop("profile_id", None)
    if profile_id is not None and not str(profile_id).strip():
        profile_id = None

    coerced = _coerce_patch(patch)

    # `LLM_AVAILABLE_MODELS` 不再是一份全局手写清单 —— 它现在表示
    # 「**选中这家**连接保存了哪些模型」。先摘出来，写完目录后再由
    # 各家的并集重新生成运行时值。
    saved_models = None
    if "LLM_AVAILABLE_MODELS" in coerced:
        saved_models = [
            m.strip()
            for m in str(coerced.pop("LLM_AVAILABLE_MODELS")).split(",")
            if m.strip()
        ]

    # ★ 先写目录，再把选中 profile 的值同步进覆写层
    #
    # 只给 `profile_id` 不给 Base URL 是对话下拉的切换方式：此时必须用
    # **那家自己的**地址，不能回退到「当前生效地址」—— 否则会把选中
    # profile 的 base_url 改成另一家的，模型与连接从此错位。
    catalog = profiles.ensure_seeded()
    target = profiles.find_by_id(catalog, profile_id) if profile_id else None
    if profile_id and target is None:
        raise LlmModelSettingsError(f"找不到供应商配置：{profile_id}")

    base_url = str(
        coerced.get("LLM_BASE_URL")
        or (target["base_url"] if target else settings.LLM_BASE_URL)
        or ""
    ).strip()
    if base_url:
        catalog, profile_id = profiles.upsert_profile(
            base_url,
            profile_id=target["id"] if target else None,
            api_key=coerced.get("LLM_API_KEY"),
            models=saved_models,
        )
        profile = profiles.find_by_id(catalog, profile_id)
        coerced["LLM_BASE_URL"] = base_url
        # 空 Key 不再是「保留全局旧 Key」：这家没 Key 就是没 Key。
        # 唯一例外是它本来就是当前生效连接（.env 里的 Key 还没迁进目录）。
        key = str(profile["api_key"] or "")
        if not key and base_url == str(settings.LLM_BASE_URL or "").strip():
            key = str(settings.LLM_API_KEY or "")
        coerced["LLM_API_KEY"] = key
        profiles.set_active_id(profile_id)

        # 保存/切换到的模型 = 这家连接保存成功的模型（幂等）
        model = str(coerced.get("LLM_MODEL") or "").strip()
        if model:
            catalog = profiles.add_saved_model(profile_id, model)

        # 运行时候选 = 各连接保存过的并集。`settings.available_models`
        # 等旧消费方仍然能读到一份合理的清单。
        coerced["LLM_AVAILABLE_MODELS"] = ", ".join(profiles.all_saved_models(catalog))
    elif saved_models is not None:
        coerced["LLM_AVAILABLE_MODELS"] = ", ".join(saved_models)

    persisted = {**load_runtime_overrides(), **coerced}
    effective = {k: getattr(settings, k, None) for k in LLM_OVERRIDE_KEYS}
    effective.update(coerced)

    warnings = _validate(effective)
    save_runtime_overrides(persisted)
    applied = apply_persisted_overrides()

    result = state()
    # 契约是「本次**实际写入**的键」，不是「你传了什么」
    result["applied"] = sorted(set(coerced) & set(applied))
    result["warnings"] = warnings
    return result


def reset() -> Dict[str, Any]:
    """
    清空**语言模型域**的覆写 → 回到 `backend/.env` 的 `LLM_MODEL`。

    ⚠️ 只摘 `LLM_MODEL`，检索域的覆写原样保留（见模块头「按域收窄」）。
    """
    remove_runtime_overrides(LLM_OVERRIDE_KEYS)
    # 生效连接回到 .env，不再是「目录里某个 profile」；目录与各家 Key 原样保留
    # （「恢复默认」摘的是覆写，不是用户的凭据）。
    profiles.set_active_id(None)
    result = state()
    result["applied"] = []
    result["warnings"] = []
    return result


# ---------------------------------------------------------------- 自检


async def _ping() -> Tuple[str, str]:
    """
    真打一次模型，返回 `(给用户的回答, 推理正文)`。

    ★ 为什么不用 `max_tokens` 限制长度

    本项目网关在有输出上限时会先把预算花在**不可见的思考**上，正文返回空串而
    HTTP 仍是 200。
    自检要的恰恰是「正文有没有回来」，加限制等于自己制造假阴性。

    ★ 为什么要切思考

    本网关会把推理正文混进 `content` 一起吐出来（`app/agents/think.py`）。
    不切的话，界面上「模型回复」那一栏可能显示一大段英文内心独白，
    让人以为模型答非所问 —— 而那是网关行为，不是这次配置的问题。
    """
    from langchain_core.messages import HumanMessage

    from app.agents.think import split_think
    from app.core.llm_factory import create_llm

    llm = create_llm(streaming=False)
    message = await llm.ainvoke([HumanMessage(content=_PROBE_PROMPT)])
    content = getattr(message, "content", "")
    if not isinstance(content, str):  # 多模态返回时是 list[dict]
        content = str(content)
    reasoning, answer = split_think(content)
    return answer.strip(), reasoning.strip()


async def probe(timeout: float = 90.0) -> Dict[str, Any]:
    """
    用**当前配置**真打一次模型，回报实测事实（能不能通 / 多快 / 回没回正文）。

    ★ 为什么值得做一个「自检」按钮

    「模型名写进去了」和「这个模型名网关认」是两件事，而后者只有真调一次才知道。
    写错一个字符的表现是下一次提问直接报错 —— 那时代码看起来毫无问题，
    排查方向会跑偏到网关、网络、密钥上。自检把这条事实提前摆出来。

    失败不抛异常：**失败本身就是最有用的信息**，原样返回给界面
    （与 `rag/model_settings.probe` 同一约定）。

    Args:
        timeout: 外层预算。`create_llm` 自己还有 `LLM_TIMEOUT_SECONDS` × 重试次数，
                 这里必须**比它还宽松**，否则外层先超时、拿到的错误里没有网关的真实回复。
    """
    model = settings.LLM_MODEL
    started = time.perf_counter()
    try:
        answer, reasoning = await asyncio.wait_for(_ping(), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        ms = round((time.perf_counter() - started) * 1000, 1)
        logger.warning("LLM 自检失败（%s）: %s: %s", model, type(e).__name__, e)
        return {
            "ok": False,
            "model": model,
            "ms": ms,
            "error": f"{type(e).__name__}: {e}",
        }

    ms = round((time.perf_counter() - started) * 1000, 1)
    out: Dict[str, Any] = {
        "ok": bool(answer),
        "model": model,
        "reply": answer[:_PROBE_REPLY_CHARS],
        "reasoning_chars": len(reasoning),
        "ms": ms,
    }
    if not answer:
        # 有回复但正文是空的 —— 本网关「预算花在思考上」的典型形状。
        # 这不是网络问题，必须说清楚，否则会被当成「模型不可用」。
        out["error"] = (
            f"模型返回了内容但正文为空（推理正文 {len(reasoning)} 字）—— "
            f"可能是该模型只输出思考过程、或网关把预算花在了推理上。"
        )
    return out


__all__ = [
    "LLM_OVERRIDE_KEYS",
    "LlmModelSettingsError",
    "list_remote_models",
    "probe",
    "reset",
    "state",
    "test_connection",
    "update",
    "usable_models",
]
