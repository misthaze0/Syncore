"""
可观测与评估接口

★ 为什么「跑评估」不放在 HTTP 接口里

评估要打 34×N 次真实模型调用，耗时 1~5 分钟。放进请求-响应模型里会立刻遇到
超时、连接中断、重复触发三个问题，而「评估」这件事本来也不需要即时反馈。
所以：

    跑批  → `scripts/run_eval.py`（CLI，可以慢慢跑、可以看进度、可以重跑）
    读结果 → 本模块的只读接口（给前端面板用）

这条分工与项目其它部分一致：`index_samples.py` 也是脚本，而不是一个 HTTP 接口。

★ 只读接口

    GET  /api/eval/golden       Golden Set 概览与查询清单
    GET  /api/eval/reports      历史评估报告列表
    GET  /api/eval/reports/{f}  单份评估报告（JSON）

追踪数据仍在后端持续采集与落盘（评估与排障要用），但不再对外暴露读接口。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from app.config import settings

router = APIRouter(tags=["observability"])


# ---------- 评估 ----------
@router.get("/eval/golden")
async def get_golden_set() -> Dict[str, Any]:
    """
    Golden Set 概览与查询清单。

    查询清单全部返回（含期望文档），因为它是评估结论的**前提**，
    不看清单就无法判断某个指标变化是算法改好了还是标注变了。
    """
    from app.eval import ensure_default_golden_set

    try:
        golden = ensure_default_golden_set(settings.golden_set_path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取 Golden Set 失败: {e}") from e

    return {
        "success": True,
        "name": golden.name,
        "scope": golden.scope,
        "version": golden.version,
        "description": golden.description,
        "path": str(settings.golden_set_path),
        "counts": golden.counts_by_kind(),
        "total": len(golden.items),
        "corpus_size": len(golden.corpus),
        # 声明式图的关系条数：多跳类目靠它才有意义。
        # 看到 `multi_hop > 0` 而 `triple_count == 0` 就说明文件是被外部改过的
        # v1 集或手工集 —— 那种情况下 ⑤⑥ 两个阶段会对着空图跑，指标全零而无报错。
        "triple_count": len(golden.triples),
        "items": [
            {
                "id": item.id,
                "query": item.query,
                "kind": item.kind,
                "expected": item.expected,
                "note": item.note,
                "tags": item.tags,
                # 多跳类目专用：这条链要经过的实体，用于界面解释「为什么它需要两跳」
                "hop_chain": item.hop_chain,
            }
            for item in golden.items
        ],
    }


def _report_files() -> List[Dict[str, Any]]:
    """扫描报告目录，返回按时间倒序的清单"""
    directory = settings.eval_report_path
    out: List[Dict[str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entry: Dict[str, Any] = {
            "name": path.name,
            "stem": path.stem,
            "size_bytes": stat.st_size,
            "modified_ms": round(stat.st_mtime * 1000, 2),
        }
        # 顺带把概览读出来，前端列表页就不必逐个请求
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry["golden"] = data.get("golden")
            entry["duration_ms"] = data.get("duration_ms")
            entry["attribution"] = (data.get("attribution") or {}).get("summary")
            stages = data.get("stages") or []
            entry["final_stage"] = stages[-1] if stages else None
        except Exception:  # noqa: BLE001
            entry["broken"] = True
        out.append(entry)

    md_files = {p.stem for p in directory.glob("*.md")}
    for entry in out:
        entry["markdown_available"] = entry["stem"] in md_files

    return sorted(out, key=lambda e: e["modified_ms"], reverse=True)


@router.get("/eval/reports")
async def list_eval_reports() -> Dict[str, Any]:
    """历史评估报告列表（含每份的最终阶段指标摘要）"""
    reports = _report_files()
    return {
        "success": True,
        "count": len(reports),
        "directory": str(settings.eval_report_path),
        "reports": reports,
    }


@router.get("/eval/reports/{name}")
async def get_eval_report(name: str) -> Dict[str, Any]:
    """单份评估报告（JSON）。`name` 可以是文件名，也可以只给去掉扩展名的主干名。"""
    directory = settings.eval_report_path

    # 只接受目录内的文件名，防止 `../` 越权读取
    candidate = (directory / name).resolve()
    if not str(candidate).startswith(str(directory.resolve())):
        raise HTTPException(status_code=400, detail="非法的报告名")

    if candidate.suffix != ".json":
        candidate = candidate.with_suffix(".json")
    if not candidate.exists():
        raise HTTPException(status_code=404, detail=f"未找到报告: {name}")

    try:
        return {"success": True, "report": json.loads(candidate.read_text(encoding="utf-8"))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"报告解析失败: {e}") from e


__all__ = ["router"]
