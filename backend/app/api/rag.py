"""
检索模型设置接口 —— 查看 / 切换向量召回与精排模型 / 自检 / 重建向量库

★ 为什么这组接口放在 `/api/rag/*` 而不是塞进 `/api/models`

`/api/models` 的语义是「**LLM** 能选什么」（顶栏那个下拉框，见 `api/models.py`）。
这里管的是**检索链路**用的两个模型（embedding / reranker）—— 消费者、变更代价、
出错后果都不一样：换个 LLM 只是下一轮回答的风格变了，换个 embedding 会让
**已经入库的向量全部作废**。放在一起会让人以为它们是同一类设置。

（LLM 的**运行时切换**在 `/api/llm/*`，见 `api/llm.py`：它复用了同一层覆写与
同一套「只读状态 + 局部更新 + 自检」的形状，但不涉及向量库，因此没有重建。
两者的键集按域分开，各自的「恢复默认」也只摘自己那一组。）

★ 为什么「切换」和「重建」是两个接口

切换是一个配置动作（毫秒级、可回滚），重建是一个**破坏性数据动作**（清空重灌、
按量计费、几十秒到几分钟）。合成一个接口意味着「点了下拉框就自动开始烧钱」，
而项目里所有主动产生成本的动作都是显式入口（索引监听、图构建都是这个原则）。

★ 为什么没有「删除向量库」接口

没有正当使用场景。要换模型请走重建（它自带转储与续建），
要彻底清空直接删容器数据卷 —— 那是一个应该在终端里做的事。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.rag import model_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rag", tags=["rag"])


class RagModelsUpdate(BaseModel):
    """
    部分更新：全部字段可选，只覆盖显式给出的（与 `PersonaUpdateRequest` 同一约定）。

    刻意**不**在这里写 `choices` / 取值范围：那属于检索域的语义，
    校验规则集中在 `model_settings._validate()` 一处，接口层重复一遍就会漂移。

    ⚠️ `extra="forbid"` 不能省。Pydantic 默认**静默丢弃**未声明的字段，
    而这里的调用方是「前端把整个表单发过来」—— 一旦键名写错（`RERANK_TOP` 少个 `_N`），
    默认行为会让请求**成功返回 200 但那个字段根本没被改**，
    表现为「界面说保存成功，值却弹回去了」，查起来毫无线索。
    直接 422/400 才是这个场景该有的反馈。
    """

    model_config = ConfigDict(extra="forbid")

    EMBEDDING_BACKEND: Optional[str] = Field(None, description="api | local")
    EMBEDDING_MODEL: Optional[str] = Field(None, description="api 后端的向量模型名")
    EMBEDDING_BASE_URL: Optional[str] = Field(None, description="api 后端的供应商 Base URL")
    EMBEDDING_API_KEY: Optional[str] = Field(None, description="api 后端的供应商 API Key")
    EMBEDDING_LOCAL_MODEL: Optional[str] = Field(None, description="local 后端的模型目录或 HF id")
    EMBEDDING_LOCAL_DEVICE: Optional[str] = Field(None, description="cpu | cuda")
    EMBEDDING_DIMENSION: Optional[int] = Field(None, description="向量维度；local 后端会自动探测补齐")

    RERANK_ENABLED: Optional[bool] = None
    RERANK_BACKEND: Optional[str] = Field(None, description="api | local | none")
    RERANK_MODEL: Optional[str] = Field(None, description="api 后端的精排模型名")
    RERANK_API_URL: Optional[str] = Field(None, description="api 后端的精排端点；留空沿用默认")
    RERANK_API_KEY: Optional[str] = Field(None, description="api 后端的精排 Key；留空复用对话模型 Key")
    RERANK_LOCAL_MODEL: Optional[str] = Field(None, description="local 后端的精排模型目录或 HF id")
    RERANK_LOCAL_DEVICE: Optional[str] = None
    RERANK_TOP_N: Optional[int] = Field(None, description="精排后保留并注入 Prompt 的条数")
    RERANK_CANDIDATES: Optional[int] = Field(None, description="送进精排的候选上限")


class RebuildRequest(BaseModel):
    """重建请求。`force=true` 时即使判定为一致也重灌（怀疑数据被污染时才用）"""

    force: bool = False


def _fail(e: model_settings.RagModelSettingsError) -> HTTPException:
    """把「配置不合法」译成 400 —— 消息是给人看的，原样透传"""
    return HTTPException(status_code=400, detail=str(e))


@router.get("/models")
async def get_rag_models() -> Dict[str, Any]:
    """
    当前检索链路用的两个模型 + 本机可选的本地模型 + 向量库一致性。

    ⚠️ `vector_store.state` 是**三态**，不是一个 bool：

    - `match`    库与当前配置同一个向量空间，可以直接用
    - `mismatch` 库是别的模型建的 → 检索会在错误的相似度下打转，需要重建
    - `unknown`  读不到（旧库没登记 / Milvus 连不上）→ **不下结论**，只提示

    把 `unknown` 也报成「需要重建」会让人在 Milvus 没启动的时候去重建向量库。
    """
    try:
        return model_settings.state()
    except Exception as e:  # noqa: BLE001
        # 状态查询不该因为某个依赖挂了就整个打不开 —— 面板要在最需要它的时候能用
        logger.error("读取检索模型状态失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"读取状态失败: {e}") from e


@router.put("/models")
async def update_rag_models(payload: RagModelsUpdate) -> Dict[str, Any]:
    """
    切换检索/精排模型（部分更新）。

    返回切换**之后**的完整状态，外加两样东西：
    - `applied`：本次实际写入的键（不是「你传了什么」）
    - `warnings`：可疑但合法的配置（例如模型目录还不存在）——
      这类必须说出来：它不会让接口失败，但会让下一次加载失败。
    """
    patch = payload.model_dump(exclude_none=True)
    try:
        result = model_settings.update(patch)
    except model_settings.RagModelSettingsError as e:
        raise _fail(e) from e
    except Exception as e:  # noqa: BLE001
        logger.error("切换检索模型失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"切换失败: {e}") from e
    logger.info("检索模型已更新: %s", result.get("applied"))
    return result


@router.post("/models/reset")
async def reset_rag_models() -> Dict[str, Any]:
    """清空运行时覆写，回到 `backend/.env` 的配置（并让模型缓存失效）"""
    try:
        return model_settings.reset()
    except Exception as e:  # noqa: BLE001
        logger.error("恢复默认检索模型失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"恢复默认失败: {e}") from e


@router.post("/probe")
async def probe_rag_models() -> Dict[str, Any]:
    """
    用当前配置真打一次向量化与精排，回报实测事实（维度 / 分数区分度 / 耗时）。

    「配置写进去了」和「配置能跑」是两件事，这个接口证明的是后者。
    永久返回 200：**调用失败本身就是答案**（放在 `ok=false` 与 `error` 里），
    用 500 表达会让界面只能显示「请求失败」，把真正有用的原因丢掉。
    """
    return await model_settings.probe()


@router.post("/vectors/rebuild")
async def rebuild_vectors(payload: RebuildRequest) -> Dict[str, Any]:
    """
    按当前 embedding 配置重灌向量库（清空 → 重新向量化全部块）。

    ⚠️ 破坏性操作，且耗时与块数成正比（api 后端还会按量计费）：
    - 执行前先自检一次；**新配置跑不通就什么都不会动**；
    - 块正文先转储到 `VECTOR_DUMP_PATH`，中途失败可幂等续建；
    - 判定为「同一个向量空间」时直接跳过（除非 `force=true`）。

    与 `/index/scan`、`/graph/build` 一样同步等待结果：这是个几十秒的交互动作，
    界面需要的是「灌了多少块」这个确凿答案，而不是一个任务 id。
    """
    try:
        result = await model_settings.rebuild_vectors(force=payload.force)
    except Exception as e:  # noqa: BLE001
        logger.error("重建向量库失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"重建失败: {e}") from e
    if not result.get("ok"):
        # 失败但**没把状态弄坏**（未改动 / 已转储），不需要 5xx 掩盖原因
        logger.warning("重建向量库未完成: %s", result.get("error"))
    return result
