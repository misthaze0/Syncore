"""
语言模型设置接口 —— 查看 / 切换默认回答模型 / 自检

★ 与 `GET /api/models`（顶栏下拉）的分工

两个接口都在回答「用哪个模型」，但问题的**时态**不同：

- `GET /api/models`：**现在能选什么**（只读候选清单，供顶栏渲染下拉框）；
- `GET /api/llm/models`：**默认是哪个、怎么改**（读配置 + 写覆写）。

所以这里多了 PUT / reset / probe，而那边永远不会失败、也永远不写文件。
把两者合成一个会造成「打开顶栏下拉 = 打开一个能改配置的接口」的错觉。

★ 为什么校验不在这里重复

取值合法性（非空、长度）与「不在候选清单里」这条警告都集中在
`app/llm/model_settings._validate()` 一处；接口层只做一件事：
把 `LlmModelSettingsError` 译成 400，消息原样透传给人看。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.llm import model_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm", tags=["llm"])


class LlmModelsUpdate(BaseModel):
    """
    部分更新：字段可选，只覆盖显式给出的（与 `RagModelsUpdate` 同一约定）。

    ⚠️ `extra="forbid"` 不能省：调用方是「前端把整个表单发过来」，
    而 Pydantic 默认**静默丢弃**未声明的字段 —— 键名打错（写成 `LLM_MODLE`）
    会变成「返回 200 但什么都没改」，界面上看起来是保存成功了。
    直接 422 才是这个场景该有的反馈。同样地，这也挡住了「顺手把
    `LLM_TEMPERATURE` 发过来」——它不在白名单，会被明确拒绝而不是静默忽略。
    """

    model_config = ConfigDict(extra="forbid")

    LLM_MODEL: Optional[str] = Field(None, description="默认回答模型名（写入运行时覆写）")
    LLM_BASE_URL: Optional[str] = Field(None, description="供应商 Base URL（OpenAI 兼容端点）")
    LLM_API_KEY: Optional[str] = Field(None, description="供应商 API Key（明文落运行时配置，GET 只回掩码）")
    LLM_AVAILABLE_MODELS: Optional[str] = Field(None, description="候选清单（逗号分隔）")
    profile_id: Optional[str] = Field(
        None, description="表单属于哪个 profile（缺省 = 后端按 Base URL 匹配/新建）"
    )


class LlmModelTest(BaseModel):
    """
    连通性测试的草稿参数 —— 全部可选，缺省回退当前生效值。

    与 `LlmModelsUpdate` 同样 `extra="forbid"`：键名打错应该 422 而不是
    「看起来测了但实际用的是生效值」—— 那会给用户假的绿灯。
    """

    model_config = ConfigDict(extra="forbid")

    LLM_MODEL: Optional[str] = Field(None, description="草稿模型名（缺省 = 当前生效）")
    LLM_BASE_URL: Optional[str] = Field(None, description="草稿 Base URL（缺省 = 当前生效）")
    LLM_API_KEY: Optional[str] = Field(None, description="草稿 API Key（缺省 = 当前生效；不会落盘）")


class LlmModelsListRequest(BaseModel):
    """拉取供应商模型清单的草稿参数（`/models/list`）—— 不需要模型名。"""

    model_config = ConfigDict(extra="forbid")

    LLM_BASE_URL: Optional[str] = Field(None, description="草稿 Base URL（缺省 = 当前生效）")
    LLM_API_KEY: Optional[str] = Field(None, description="草稿 API Key（缺省 = 目录里该地址的 Key）")


def _fail(e: model_settings.LlmModelSettingsError) -> HTTPException:
    """把「配置不合法」译成 400 —— 消息是给人看的，原样透传"""
    return HTTPException(status_code=400, detail=str(e))


@router.get("/models")
async def get_llm_model() -> Dict[str, Any]:
    """
    当前默认模型 + 候选清单 + 覆写现状 + 只读运行参数。

    只读、不发任何模型请求（自检是 `POST /llm/probe` 的事），
    所以面板可以在任何环境（网关不通、没配密钥）下打开。
    """
    try:
        return model_settings.state()
    except Exception as e:  # noqa: BLE001
        logger.error("读取语言模型状态失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"读取状态失败: {e}") from e


@router.put("/models")
async def update_llm_model(payload: LlmModelsUpdate) -> Dict[str, Any]:
    """
    切换默认回答模型（部分更新，写运行时覆写，不碰 `.env`）。

    返回切换**之后**的完整状态，外加：
    - `applied`：本次实际写入的键（不是「你传了什么」）
    - `warnings`：可疑但合法的配置（例如模型名不在 `.env` 的候选清单里）
    """
    patch = payload.model_dump(exclude_none=True)
    try:
        result = model_settings.update(patch)
    except model_settings.LlmModelSettingsError as e:
        raise _fail(e) from e
    except Exception as e:  # noqa: BLE001
        logger.error("切换语言模型失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"切换失败: {e}") from e
    logger.info("语言模型已更新: %s → %s", result.get("applied"), result.get("model"))
    return result


@router.post("/models/reset")
async def reset_llm_model() -> Dict[str, Any]:
    """
    清空**语言模型域**的运行时覆写 → 回到 `backend/.env` 的 `LLM_MODEL`。

    ⚠️ 只摘 `LLM_MODEL`：检索模型的覆写（向量召回 / 精排）原样保留 ——
    覆写文件是两个面板共用的，在「语言模型」页点一下不该改掉「检索模型」页的设置。
    """
    try:
        return model_settings.reset()
    except Exception as e:  # noqa: BLE001
        logger.error("恢复默认语言模型失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"恢复默认失败: {e}") from e


@router.post("/test")
async def test_llm_model(payload: LlmModelTest) -> Dict[str, Any]:
    """
    用**草稿参数**真打一次模型 —— 「测试连接」按钮的后端，**绝不落盘**。

    ★ 为什么必须有这个接口（与 `/llm/probe` 的分工）

    probe 只能测「已保存」的配置；而「Key 填了并且连接成功才允许保存」
    这条规则要求测试发生在保存**之前** —— 测的是表单里还没落盘的参数。
    让前端先 PUT 再 probe 是错误设计：坏的 Key 会先污染运行时配置。

    缺省字段回退当前生效值：只改模型名 / 只换 Base URL 时不必重填 Key。

    永久返回 200：失败原因（401 / 模型名不认识 / 超时）在 `ok=false` 与
    `error` 里原样透传，与 probe() 同一约定。参数结构性非法（空模型名、
    非 http(s) 的 URL）才返回 400。
    """
    patch = payload.model_dump(exclude_none=True)
    try:
        return await model_settings.test_connection(patch)
    except model_settings.LlmModelSettingsError as e:
        raise _fail(e) from e
    except Exception as e:  # noqa: BLE001
        logger.error("连通性测试失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"连通性测试失败: {e}") from e


@router.post("/models/list")
async def list_remote_models(payload: LlmModelsListRequest) -> Dict[str, Any]:
    """
    用草稿参数问供应商「你家有哪些模型」（`GET {base_url}/models`）—— 不落盘。

    设置页「默认模型」下拉的数据源。与 /test 同一约定：拉不到也返回 200，
    原因在 `ok=false` 与 `error` 里 —— 没有 `/models` 端点的网关（部分本地
    服务）不算错误场景，界面回退到手填模型名即可。
    """
    patch = payload.model_dump(exclude_none=True)
    try:
        return await model_settings.list_remote_models(patch)
    except model_settings.LlmModelSettingsError as e:
        raise _fail(e) from e
    except Exception as e:  # noqa: BLE001
        logger.error("拉取模型清单失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"拉取模型清单失败: {e}") from e


@router.post("/probe")
async def probe_llm_model() -> Dict[str, Any]:
    """
    用当前配置真打一次模型，回报实测事实（通不通 / 多快 / 回没回正文）。

    永久返回 200：**调用失败本身就是答案**（在 `ok=false` 与 `error` 里）。
    用 5xx 表达会让界面只能显示「请求失败」，把真正有用的原因丢掉
    （网关 401 / 模型名不认识 / 超时，三者的处理方式完全不同）。

    ⚠️ 这是一个会**真实产生 token 消耗**的动作，但代价极小（一问一答），
    且它证明的正是「这个模型名网关认不认」—— 不点它就只能靠下一次提问撞出来。
    """
    return await model_settings.probe()
