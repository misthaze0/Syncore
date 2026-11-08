"""
技能接口 —— 设置页「技能」的数据源

只做四件事：列清单、看一份技能的正文、开关一个技能、重新扫描目录。

★ 为什么没有"新建/删除技能"的写接口

一个技能就是磁盘上的一个文件夹（`SKILL.md` + 材料）。在界面上做"新建技能"
意味着要在后端造一套编辑器 + 校验 + 落盘路径管理，而用户真正做的事是
**把别人写的技能文件夹拷进 `data/skills/`**。所以这里只暴露读与开关；
清单里带上目录路径，用户想知道"该放哪儿"时看得见。

★ 有问题的技能也要出现在清单里

`SKILL.md` 写坏了（缺 frontmatter、`name` 不合规）的技能不参与注入，
但接口必须把它列出来并带上 `error` —— 否则用户改坏一个字母后，
界面上技能直接消失，只剩下"她为什么说没装"这一个谜。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.skills import get_skill_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/skills", tags=["skills"])


class SkillEnabledPayload(BaseModel):
    """开关一个技能"""

    enabled: bool


@router.get("")
async def list_skills() -> dict:
    """
    全部技能（生效的在前，被同名用户技能遮住的内置技能标 `shadowed`）。

    带上两个目录路径：界面上要告诉用户"把技能文件夹放这儿"，
    而路径是配置算出来的（可能是绝对路径），不该在前端再抄一遍。
    """
    store = get_skill_store()
    items = store.list_dicts()
    return {
        "skills": items,
        "total": len(items),
        "active": len([i for i in items if i["usable"] and i["enabled"] and not i["shadowed"]]),
        "dirs": {
            "user": str(settings.skills_dir),
            "bundled": str(settings.skills_bundled_dir),
        },
        "skills_enabled": settings.SKILLS_ENABLED,
        "inject_max_chars": settings.SKILLS_INJECT_MAX_CHARS,
    }


@router.post("/rescan")
async def rescan_skills() -> dict:
    """重扫目录（用户在 `data/skills/` 里拷了新技能后点一下）。清单本身按目录指纹自动刷新。"""
    store = get_skill_store()
    items = [s.to_dict(enabled=store.is_enabled(s.name)) for s in store.scan(force=True)]
    return {"ok": True, "skills": items, "total": len(items)}


@router.get("/{name}")
async def get_skill(name: str) -> dict:
    """一份技能的元数据 + 正文（界面上的"看看这个技能写了什么"）"""
    store = get_skill_store()
    skill = store.get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能不存在: {name}")
    return {
        **skill.to_dict(enabled=store.is_enabled(skill.name)),
        "instructions": store.load_instructions(skill.name),
    }


@router.put("/{name}/enabled")
async def set_skill_enabled(name: str, payload: SkillEnabledPayload) -> dict:
    """开 / 关一个技能（关闭 = 不再注入，也不会被 `use_skill` 加载）"""
    try:
        return {"ok": True, **get_skill_store().set_enabled(name, payload.enabled)}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
