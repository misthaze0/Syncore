"""
LLM profile 目录 —— 供应商预设与各自的 API Key（ADR 0021 切片 1）

★ 为什么要有这个目录

覆写层（`data/runtime_config.json`）回答的是「**当前**这一套连接是什么」，
全局只有一份 `LLM_BASE_URL` / `LLM_API_KEY`。设置页的供应商 chip 曾经只是
前端表单预设：切到 DeepSeek 时 Key 行显示的仍是百炼那把 Key 的掩码，
空 Key 保存还会把旧 Key 配对新网关落盘 —— 用户看到的就是「每个供应商都填了
同一把 Key」。目录把「每家各自的 Key」记下来，切换才名副其实。

★ 与覆写层的分工

目录记**所有**供应商的凭据；覆写层记**当前生效**的那一套（`create_llm()` 只读
`settings`）。保存 = 先写目录、再把选中 profile 的值同步进覆写层 ——
运行时链路（llm_factory / 顶栏 / 各 Agent）零改动。

★ 密钥边界（ADR 0021 四条硬保证）

明文落 `backend/data/llm_profiles.json`（与 `.env` / 覆写层同一风险面，
单用户本机桌面应用），gitignore 覆盖；本模块**任何日志不含 Key**；
出参只给掩码（掩码实现唯一份在 `config.mask_secret`）。

★ 语音不从这里取 Key（2026-10-17 用户口径）

语音接入将来是 `VOICE_BASE_URL` / `VOICE_API_KEY` 独立把手、由用户如实填写；
本目录不做「按域名推导百炼 Key」之类的写死逻辑。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

#: 目录文件结构版本。v1 = {version, active_id, profiles[]}
#:
#: v2 起 `models` 的含义收窄为「**这家连接保存成功的模型**」：
#: 预设清单（PRESET_PROFILES.models）只是填表建议，不再落进目录。
#: 否则一家供应商的 chip 会混进另一家的模型名，对话下拉也无从区分
#: 「能用」与「只是见过」。
CATALOG_VERSION = 2

#: 供应商预设目录 —— 前端 `CHAT_PROVIDERS` 挪入（ADR 0021 跟进项：目录唯一源在后端）。
#: 只放 OpenAI 兼容（/v1/chat/completions）的供应商：后端链路是 ChatOpenAI，
#: 不支持的协议摆出来也是填了用不了的假选项。
PRESET_PROFILES: List[Dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    {
        "id": "moonshot",
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k2.7-code", "kimi-latest", "moonshot-v1-8k"],
        "key_url": "https://platform.moonshot.cn/console/api-keys",
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.7-flash", "qwen-plus", "qwen-max", "qwen3.7-text-rerank"],
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash"],
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini"],
        "key_url": "https://platform.openai.com/api-keys",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4", "google/gemini-2.0-flash-001"],
        "key_url": "https://openrouter.ai/settings/keys",
    },
    {
        "id": "siliconflow",
        "name": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "key_url": "https://cloud.siliconflow.cn/account/ak",
    },
    {
        "id": "ollama",
        "name": "Ollama（本机）",
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
        "key_url": "",
    },
]


def custom_id(base_url: str) -> str:
    """自定义端点的稳定 id：同一 Base URL 永远映射到同一个 profile"""
    return "custom-" + hashlib.sha1(base_url.encode("utf-8")).hexdigest()[:8]


def _normalize_profile(raw: Any) -> Optional[Dict[str, Any]]:
    """目录里的一条记录 → 形状固定的 dict；结构不对返回 None（跳过而非炸掉）"""
    if not isinstance(raw, dict):
        return None
    base_url = str(raw.get("base_url") or "").strip()
    pid = str(raw.get("id") or "").strip()
    if not pid:
        return None
    models = raw.get("models")
    if not isinstance(models, list):
        models = []
    return {
        "id": pid,
        "name": str(raw.get("name") or "").strip() or pid,
        "base_url": base_url,
        "api_key": str(raw.get("api_key") or ""),
        "models": [str(m).strip() for m in models if str(m).strip()],
        "key_url": str(raw.get("key_url") or ""),
        "builtin": bool(raw.get("builtin")),
    }


def load_catalog(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """
    读目录；文件不存在返回 None（调用方 seed）。

    ★ 解析失败 = 备份后重新 seed，而不是忽略

    覆写层损坏的后果是「回到 .env」（能跑），这里损坏丢的是**密钥** ——
    静默重 seed 会让用户以为 Key 还在、界面却显示未配置。所以先把坏文件
    改名留证（`llm_profiles.json.corrupt-<ts>`），再当不存在处理。
    """
    p = path or settings.profiles_path
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        backup = p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}")
        try:
            os.replace(p, backup)
            logger.warning("profile 目录不可读，已备份为 %s 并将重新 seed: %s", backup.name, e)
        except OSError as re:  # pragma: no cover - 权限异常
            logger.warning("profile 目录不可读且备份失败: %s / %s", e, re)
        return None

    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, list):
        backup = p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}")
        try:
            os.replace(p, backup)
            logger.warning("profile 目录结构异常，已备份为 %s 并将重新 seed", backup.name)
        except OSError:  # pragma: no cover
            logger.warning("profile 目录结构异常且备份失败")
        return None

    clean = [x for x in (_normalize_profile(r) for r in profiles) if x]
    active_id = data.get("active_id") if isinstance(data, dict) else None
    try:
        version = int(data.get("version") or 1) if isinstance(data, dict) else 1
    except (TypeError, ValueError):
        version = 1
    return {
        "version": version,
        "active_id": str(active_id) if isinstance(active_id, str) else None,
        "profiles": clean,
    }


def save_catalog(catalog: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """原子写（先 tmp 再 os.replace）—— 与覆写层同一套做法：写一半被杀不能留半截 JSON"""
    p = path or settings.profiles_path
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CATALOG_VERSION,
        "note": (
            "LLM profile 目录：各供应商的 Base URL 与 API Key（明文，本机单用户边界）。"
            "由设置页写入，不要手工编辑。"
        ),
        "active_id": catalog.get("active_id"),
        "profiles": sorted(catalog.get("profiles") or [], key=lambda x: (not x.get("builtin"), x.get("id") or "")),
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def _migrate(catalog: Dict[str, Any], path: Optional[Path]) -> Dict[str, Any]:
    """
    v1 → v2：`models` 从「预设建议 + 用户保存」混合，收窄为「保存成功的模型」。

    - 没有 Key 的 profile：`models` 只可能是预设建议，清空（填表建议仍在
      `PRESET_PROFILES`，不落盘）；
    - 有 Key 的 profile：保留原清单 —— 那是用户实际接入过的凭据范围，
      删掉等于替他忘记；
    - 生效连接当前正在用的模型一定算「保存过」。
    """
    if catalog.get("version", 1) >= CATALOG_VERSION:
        return catalog

    for p in catalog["profiles"]:
        if not p["api_key"]:
            p["models"] = []
    active = find_by_id(catalog, catalog.get("active_id") or "")
    model = str(settings.LLM_MODEL or "").strip()
    if (
        active
        and model
        and active["base_url"] == str(settings.LLM_BASE_URL or "").strip()
        and model not in active["models"]
    ):
        active["models"].append(model)
    catalog["version"] = CATALOG_VERSION
    save_catalog(catalog, path)
    return catalog


def ensure_seeded(path: Optional[Path] = None) -> Dict[str, Any]:
    """
    目录不存在时 seed：八个预设（空 Key、空模型清单）+ 迁移当前生效连接。

    ★ 预设的 `models` 不落盘

    它们是「填表时可以去拉/去填的提示」，不是「这家连接保存成功的模型」。
    落盘会让「已保存」清单凭空多出几十个从没连过的模型名。

    ★ 迁移只认「Key 非占位符且 Base URL 非空」

    命中预设（Base URL 逐字相等）就把 Key 写进它；没命中建 custom profile。
    两者都没有（.env 没配 Key）→ `active_id = null`，目录是纯预设。
    seed 仅在文件不存在时触发，重复启动不会重复迁移。
    """
    existing = load_catalog(path)
    if existing is not None:
        return _migrate(existing, path)

    profiles = [
        {**preset, "api_key": "", "models": [], "builtin": True}
        for preset in (dict(p) for p in PRESET_PROFILES)
    ]
    active_id: Optional[str] = None
    key = str(settings.LLM_API_KEY or "").strip()
    base_url = str(settings.LLM_BASE_URL or "").strip()
    if key and key != "sk-placeholder" and base_url:
        target = next((p for p in profiles if p["base_url"] == base_url), None)
        if target is None:
            target = {
                "id": custom_id(base_url),
                "name": "自定义端点",
                "base_url": base_url,
                "models": [],
                "key_url": "",
                "builtin": False,
            }
            profiles.append(target)
        target["api_key"] = key
        active_id = target["id"]

    catalog = {"version": CATALOG_VERSION, "active_id": active_id, "profiles": profiles}
    save_catalog(catalog, path)
    return catalog


def find_by_base_url(catalog: Dict[str, Any], base_url: str) -> Optional[Dict[str, Any]]:
    return next((p for p in catalog["profiles"] if p["base_url"] == base_url), None)


def find_by_id(catalog: Dict[str, Any], profile_id: str) -> Optional[Dict[str, Any]]:
    return next((p for p in catalog["profiles"] if p["id"] == profile_id), None)


def add_saved_model(
    profile_id: str, model: str, path: Optional[Path] = None
) -> Dict[str, Any]:
    """把一个模型记到该供应商名下（幂等）——「保存连接」与「切模型」都经过这里"""
    catalog = ensure_seeded(path)
    profile = find_by_id(catalog, profile_id)
    if profile is None:
        raise KeyError(f"找不到供应商配置：{profile_id}")
    name = model.strip()
    if name and name not in profile["models"]:
        profile["models"].append(name)
        save_catalog(catalog, path)
    return catalog


def all_saved_models(catalog: Dict[str, Any]) -> List[str]:
    """
    目录里所有「保存成功的模型」，按目录顺序去重。

    这是运行时 `LLM_AVAILABLE_MODELS` 的唯一来源：它以前是一个全局手写清单，
    谁都能往里加；现在只有「某个供应商连接保存过它」才算数。
    """
    out: List[str] = []
    for p in catalog["profiles"]:
        for name in p["models"]:
            if name not in out:
                out.append(name)
    return out


def upsert_profile(
    base_url: str,
    *,
    profile_id: Optional[str] = None,
    name: Optional[str] = None,
    api_key: Optional[str] = None,
    models: Optional[List[str]] = None,
    path: Optional[Path] = None,
) -> tuple[Dict[str, Any], str]:
    """
    写入一个 profile 的表单值，返回 `(目录, profile id)`。

    ★ `api_key=None` = 保持目录原值（可能是空）

    「空 Key = 保留**全局**旧 Key」是串 Key 隐患的根源；新语义是空 Key 根本
    不会到达这里（update() 只在草稿非空时传值），目录里没 Key 就是没 Key。
    """
    catalog = ensure_seeded(path)
    base_url = base_url.strip()
    profile = find_by_id(catalog, profile_id) if profile_id else None
    if profile is None:
        profile = find_by_base_url(catalog, base_url)
    if profile is None:
        profile = {
            "id": profile_id or custom_id(base_url),
            "name": name or "自定义端点",
            "base_url": base_url,
            "api_key": "",
            "models": [],
            "key_url": "",
            "builtin": False,
        }
        catalog["profiles"].append(profile)

    profile["base_url"] = base_url
    if name:
        profile["name"] = name
    if api_key:
        profile["api_key"] = api_key
    if models is not None:
        profile["models"] = [m.strip() for m in models if m.strip()]
    save_catalog(catalog, path)
    return catalog, profile["id"]


def set_active_id(profile_id: Optional[str], path: Optional[Path] = None) -> None:
    """标记当前驱动全局覆写的 profile；`None` = 生效连接不在目录里（如恢复默认后）"""
    catalog = ensure_seeded(path)
    catalog["active_id"] = profile_id
    save_catalog(catalog, path)


def key_for_base_url(base_url: str, path: Optional[Path] = None) -> str:
    """该 Base URL 在目录里的 Key；没有就是空串（连通性测试的回退链用）"""
    catalog = ensure_seeded(path)
    profile = find_by_base_url(catalog, base_url.strip())
    return str(profile["api_key"]) if profile else ""


__all__ = [
    "CATALOG_VERSION",
    "PRESET_PROFILES",
    "add_saved_model",
    "all_saved_models",
    "custom_id",
    "ensure_seeded",
    "find_by_base_url",
    "find_by_id",
    "key_for_base_url",
    "load_catalog",
    "save_catalog",
    "set_active_id",
    "upsert_profile",
]
