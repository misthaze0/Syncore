"""
用量接口 —— 读账本，不改账本

★ 这里**没有** DELETE

不是没做。账本的语义是「消耗事实」，一个 HTTP 调用就能清掉的账不叫账。
界面上同样不提供删除入口，并在「说明」里写明记录只增不删。

    GET  /api/usage/overview   总量 + 按天曲线 + 按模型 + 按对话窗口（一次扫描全出）
    GET  /api/usage/records    明细记录（分页 + 按模型/窗口过滤）

★ 为什么 overview 一次返回四个视图

四个视图口径必须完全一致（曲线最后一天的高度 = 明细里当天各笔之和）。
各扫一遍磁盘 = 四次 IO，更要命的是过滤条件要在四处各写一遍 ——
写歪一处就是「表格和曲线对不上」这种最难解释的现象。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from app.usage import WINDOW_LABELS, get_ledger, window_kind_of

logger = logging.getLogger(__name__)
router = APIRouter(tags=["usage"])


def _live_titles(thread_ids: List[str]) -> Dict[str, str]:
    """
    批量取当前会话标题（只读会话索引）。

    账本里的 `title` 是**当时**的快照，会话改名或删掉之后会滞后：
    改名 → 用索引里的新名；会话已删 → 索引里查不到，回退快照。
    两级回退，所以「删掉的会话」在账上仍然认得出。
    """
    ids = [t for t in dict.fromkeys(thread_ids) if t]
    if not ids:
        return {}
    try:
        from app.conversations.store import conversation_store

        return {tid: conversation_store.title_of(tid) for tid in ids}
    except Exception as e:  # noqa: BLE001
        # 标题是增益：拿不到就退回快照，不该让用量页打不开
        logger.debug("读取会话标题失败（已回退快照）: %s", e)
        return {}


def _label(thread_id: str, snapshot: str, live: Dict[str, str]) -> str:
    """对话窗口的展示名：当前标题 > 落账时的快照 > 「类型·短 id」"""
    return live.get(thread_id) or snapshot or (
        f"{WINDOW_LABELS.get(window_kind_of(thread_id), '会话')}·{thread_id[:8]}"
        if thread_id
        else "（无会话标识）"
    )


@router.get("/usage/overview")
async def usage_overview(
    days: int = Query(30, ge=1, le=3650, description="统计跨度（自然日）"),
    model: Optional[str] = Query(None, description="按模型过滤（曲线/表格同步生效）"),
    thread_id: Optional[str] = Query(None, description="按对话窗口过滤"),
) -> Dict[str, Any]:
    """总量、按天曲线、按模型汇总、按对话窗口汇总（含各模型明细）"""
    ledger = get_ledger()
    data = ledger.summary(days=days, model=model, thread_id=thread_id)

    windows = data["per_window"]
    live = _live_titles([w["thread_id"] for w in windows])
    for window in windows:
        window["label"] = _label(window["thread_id"], window.get("title", ""), live)

    return {"success": True, **data, "ledger": ledger.stats()}


@router.get("/usage/records")
async def usage_records(
    limit: int = Query(50, ge=1, le=500, description="每页条数"),
    offset: int = Query(0, ge=0),
    days: Optional[int] = Query(None, ge=1, le=3650, description="留空 = 全部历史"),
    model: Optional[str] = Query(None, description="按模型过滤"),
    thread_id: Optional[str] = Query(None, description="按对话窗口过滤"),
    window_kind: Optional[str] = Query(None, description="按窗口类型：chat / employee / subtask / assistant"),
) -> Dict[str, Any]:
    """明细记录（按时间倒序）。`total` 是**过滤后**的总条数，供分页使用。"""
    ledger = get_ledger()
    records, total = ledger.query(
        limit=limit, offset=offset, days=days,
        model=model, thread_id=thread_id, window_kind=window_kind,
    )
    live = _live_titles([str(r.get("thread_id") or "") for r in records])
    for record in records:
        tid = str(record.get("thread_id") or "")
        record["label"] = _label(tid, str(record.get("title") or ""), live)
    return {
        "success": True,
        "count": len(records),
        "total": total,
        "limit": limit,
        "offset": offset,
        "records": records,
    }


__all__ = ["router"]
