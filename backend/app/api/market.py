"""
插件市场接口 —— 侧栏「插件市场」的后端

三个面共用 `/api/market`：
- 发现：`GET /market?view=discover`（目录源条目，未安装的带一次性 `install_ref`）
- 已安装：`GET /market?view=installed`（注册表 + 活扫描，状态全是真的）
- 连接器：`kinds=mcp` 的那批 —— 目录里的连接器和本机自己接的 server 走同一条状态合并

★ 安装为什么必须先拿 `install_ref`

`POST /install` 只认凭证不认 canonical：安装必须跟在"界面读过这条目录"之后，
凭证一次性、十分钟过期。直接暴露 `POST /install {canonical}` 的话，
任何一个能碰到本机端口的页面都能往 `data/skills/` 里塞东西。

★ 目录外的东西怎么进来（上传 / 本机路径 / zip 地址 / 粘贴 JSON）

这四条都是"绕开目录清单"的口子，所以它们**共用一套落盘代码**：
`MarketStore.upload_*` / `install_*` 走的是和目录安装同一个校验链
（zip-slip、大小上限、SKILL.md 必须解析得出来、同名拒绝），
装完一律标 `custom` / 「未经审核」。口子多而不收敛，等于每个口子都要重新想一遍安全。

★ 写动作之后的对账

装了 / 卸了 / 开关了 MCP 相关的条目，收尾必须 `mcp_manager.apply()`；
技能相关则 `SkillStore.scan(force=True)`（安装器内部已做）。
对账失败不回滚写操作 —— 配置已落盘，连不上是运行期的事，
原因进响应的 `apply.failed`，界面显示红条而不是假装没保存。

★ 阻塞下载不进事件循环

zip 下载与解压是同步 IO，接口用 `asyncio.to_thread` 丢到线程池，
主循环不被一个 20MB 的包卡住。
"""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.agents.tools.mcp_adapter import mcp_manager
from app.marketplace import KINDS, MarketError, create_brief, get_market_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/market", tags=["market"])


class InstallPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    install_ref: str = Field(description="目录接口下发的一次性安装凭证")


class UninstallPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical: str


class EnabledPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class InstallLocalPayload(BaseModel):
    """目录外的技能/插件：本机路径（目录或 zip）或 zip 地址，二选一"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field("skill", description="skill / plugin")
    source: str = Field(description="绝对路径、相对 market/packs/ 的目录名，或 http(s) zip 地址")


class McpImportPayload(BaseModel):
    """粘贴一段 MCP 配置（`mcpServers` 映射 / 数组 / 单个 server 对象）"""

    model_config = ConfigDict(extra="forbid")

    config: str


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


async def _apply_if_needed(needs_apply: bool) -> Optional[Dict[str, Any]]:
    if not needs_apply:
        return None
    try:
        return await mcp_manager.apply()
    except Exception as e:  # noqa: BLE001
        logger.error("市场写操作后的 MCP 对账失败: %s", e, exc_info=True)
        return {"failed": {"apply": f"{type(e).__name__}: {e}"}}


@router.get("")
async def search(
    view: str = Query("discover", pattern="^(discover|installed)$"),
    q: str = "",
    kinds: str = "",
    category: str = "",
    featured: bool = False,
    sort: str = Query("popular", pattern="^(popular|newest)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
) -> Dict[str, Any]:
    """
    目录 / 已安装清单。

    `kinds` 是逗号分隔的 plugin/skill/mcp；界面三个 tab 各传各的。
    每条带四个独立状态位（installed / enabled / configuration_required /
    update_available）—— 不合并成一个分数，合并了界面就只能显示"1/1"。
    `categories` 带每个分类的条数（按当前 tab + 当前搜索词算，不含分类本身），
    所以 chip 上的数字点进去一定对得上，不会出现「写着 3 条、打开是空的」。
    """
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()]
    bad = [k for k in kind_list if k not in KINDS]
    if bad:
        raise _bad(f"kind 不认识：{', '.join(bad)}（可选：{' / '.join(KINDS)}）")
    store = get_market_store()
    return await asyncio.to_thread(
        store.search,
        view=view,
        kinds=kind_list or None,
        q=q,
        category=category,
        featured=featured,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@router.get("/sources")
async def sources() -> Dict[str, Any]:
    """目录源健康度：内置 / 远程各一条，读失败带原因（界面红条的数据源）"""
    _, src = get_market_store().load_index()
    return {"sources": src, "failures": [s for s in src if s.get("error")]}


@router.post("/refresh")
async def refresh() -> Dict[str, Any]:
    """重读目录源（远程源重新拉一次）"""
    src = await asyncio.to_thread(get_market_store().refresh)
    return {"ok": True, "sources": src, "failures": [s for s in src if s.get("error")]}


@router.get("/entry/{canonical}")
async def entry(canonical: str) -> Dict[str, Any]:
    """单条详情（详情抽屉用；含 `install_ref`）"""
    payload = get_market_store().get_public(canonical)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"目录里没有这条：{canonical}")
    return payload


@router.post("/install")
async def install(payload: InstallPayload) -> Dict[str, Any]:
    """凭安装凭证装一条目录条目（组合包会连带装成员）"""
    store = get_market_store()
    try:
        entry = store.consume_ref(payload.install_ref)
    except MarketError as e:
        raise _bad(str(e)) from e
    try:
        result = await asyncio.to_thread(store.install_entry, entry)
    except MarketError as e:
        raise _bad(str(e)) from e
    applied = await _apply_if_needed(result["needs_apply"])
    logger.info("市场安装完成: %s（%s）", entry.canonical, entry.kind)
    return {
        "ok": True,
        "entry": store.get_public(entry.canonical),
        # 组合包一次装好几样：界面要说清"到底落了哪些"，不能只回一句"已安装"
        "installed": result.get("installed") or [],
        "apply": applied,
    }


@router.post("/uninstall")
async def uninstall(payload: UninstallPayload) -> Dict[str, Any]:
    """卸载。组合包成员不单独卸（先卸包），内置技能不卸（只能停用）"""
    store = get_market_store()
    try:
        result = await asyncio.to_thread(store.uninstall, payload.canonical)
    except MarketError as e:
        raise _bad(str(e)) from e
    applied = await _apply_if_needed(result["needs_apply"])
    logger.info("市场卸载完成: %s", payload.canonical)
    return {"ok": True, "apply": applied}


@router.post("/entry/{canonical}/enabled")
async def set_enabled(canonical: str, payload: EnabledPayload) -> Dict[str, Any]:
    """启用 / 停用（组合包 = 连带全部成员）"""
    store = get_market_store()
    try:
        result = await asyncio.to_thread(store.set_enabled, canonical, payload.enabled)
    except MarketError as e:
        raise _bad(str(e)) from e
    applied = await _apply_if_needed(result["needs_apply"])
    return {"ok": True, "entry": store.get_public(canonical), "apply": applied}


@router.get("/create-brief")
async def brief(kind: str = Query("skill", pattern="^(skill|plugin)$")) -> Dict[str, Any]:
    """
    「通过 Syncore 创建」预填给她的那段话。

    路径和格式约束（技能目录在哪、清单叫什么）由后端拼 —— 前端抄一份就是第二份真相，
    哪天规则改了，向导会把人往错的地方带。
    """
    return create_brief(kind)


@router.post("/upload")
async def upload(file: UploadFile = File(...), kind: str = Form("skill")) -> Dict[str, Any]:
    """
    界面选本地文件装：技能 = zip 包或单个 `SKILL.md`；插件 = 带 `plugin.json` 的 zip。

    文件名是客户端可控的，这里只用来判断后缀；落盘路径全部由 `MarketStore` 拼，
    并且只可能落在 `data/skills/` 下的新目录里。
    """
    if kind not in ("skill", "plugin", "auto"):
        raise _bad(f"上传只支持 skill / plugin，收到「{kind}」")
    content = await file.read()
    if not content:
        raise _bad("这个文件是空的")
    if kind == "auto":
        if (file.filename or "").lower().endswith(".md"):
            kind = "skill"
        else:
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    names = {name.replace("\\", "/") for name in archive.namelist()}
                    # A resource named plugin.json inside a skill is not a bundle manifest.
                    root_manifest = "plugin.json" in names
                    wrapped_manifest = any(len(name.split("/")) == 2 and name.endswith("/plugin.json") for name in names)
                    kind = "plugin" if root_manifest or (wrapped_manifest and "SKILL.md" not in names) else "skill"
            except zipfile.BadZipFile as exc:
                raise _bad("请选择 ZIP 扩展包或 Markdown 技能文件") from exc
    store = get_market_store()
    try:
        result = await asyncio.to_thread(
            store.upload_plugin if kind == "plugin" else store.upload_skill,
            file.filename or "",
            content,
        )
    except MarketError as e:
        raise _bad(str(e)) from e
    applied = await _apply_if_needed(bool(result.get("needs_apply")))
    logger.info("市场上传安装完成：%s（%s）", result["canonical"], kind)
    return {"ok": True, **result, "entry": store.get_public(result["canonical"]), "apply": applied}


@router.post("/install-local")
async def install_local(payload: InstallLocalPayload) -> Dict[str, Any]:
    """
    给一个本机路径（技能目录 / 插件目录 / zip）或 zip 地址，装进本机。

    和 `/upload` 走的是同一批落盘校验 —— 两条路只是"文件怎么到她面前"不同，
    能装什么、装到哪、什么算坏，必须完全一样。
    """
    source = payload.source.strip().strip('"')
    if not source:
        raise _bad("给一个本机路径或 zip 地址")
    store = get_market_store()
    is_url = source.lower().startswith(("http://", "https://"))
    try:
        if payload.kind == "plugin":
            if is_url:
                raise MarketError("插件包只能从本机选（目录或 zip），不支持地址")
            result = await asyncio.to_thread(store.install_plugin_from_path, source)
        elif payload.kind == "skill":
            result = await asyncio.to_thread(
                store.adhoc_install_skill, url=source if is_url else "", path="" if is_url else source
            )
        else:
            raise MarketError(f"只支持 skill / plugin，收到「{payload.kind}」")
    except MarketError as e:
        raise _bad(str(e)) from e
    applied = await _apply_if_needed(bool(result.get("needs_apply")))
    logger.info("市场本机安装完成：%s", result["canonical"])
    return {"ok": True, **result, "entry": store.get_public(result["canonical"]), "apply": applied}


@router.post("/mcp-import")
async def import_mcp(payload: McpImportPayload) -> Dict[str, Any]:
    """
    粘贴一段 MCP 配置（`mcpServers` 映射 / 数组 / 单个 server 对象都收）。

    一次贴好几家是常态：逐条落、逐条报成/败，不整批回滚 ——
    配置层已经写进去的 server 没道理因为邻居少写了一个 url 而被撤掉。
    认不出的键丢进 `ignored` 回报，不静默吞掉。
    """
    store = get_market_store()
    try:
        result = await asyncio.to_thread(store.import_mcp_config, payload.config)
    except MarketError as e:
        raise _bad(str(e)) from e
    applied = await _apply_if_needed(bool(result["needs_apply"]))
    logger.info("市场导入 MCP 配置：%s", result["created"] or "没有落成的")
    return {"ok": True, **result, "apply": applied}
