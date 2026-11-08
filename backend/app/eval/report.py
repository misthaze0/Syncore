"""
评估报告渲染 —— 把指标变成一份能直接读的 Markdown

★ 报告里最重要的不是那张指标表

指标表谁都能生成。真正决定一份评估报告有没有用的，是最后那一段结论：
**这一轮该先改哪里**。所以 `render_markdown()` 会把归因结果收敛成一句可执行的判断，
并且只在**有证据支撑时**才下结论 —— 样本太少的类别不给建议，避免用噪声指挥改动。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from app.eval.attribution import (
    CATEGORY_GENERATION_MISS,
    CATEGORY_OK,
    CATEGORY_RANKING_MISS,
    CATEGORY_RETRIEVAL_MISS,
    STAGE_LABELS,
    STAGE_SUGGESTIONS,
)
from app.eval.runner import EvalReport

#: 少于这个样本数的类别不下强结论 —— 3 条以内的分布差异基本是噪声
MIN_SAMPLES_FOR_CONCLUSION = 3


def _esc(text: Any) -> str:
    """表格单元格转义：竖线会把 Markdown 表格结构打断"""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_markdown(report: EvalReport) -> str:
    lines: List[str] = []
    add = lines.append

    add(f"# 检索与答案质量评估报告 · {report.golden_name}")
    add("")
    add(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
    add(f"> Golden Set：`{report.golden_name}`（scope=`{report.scope}`，"
        f"{report.item_count} 条查询）  ")
    add(f"> 判定口径：Recall@{report.k} / MRR / Top-1 / nDCG@{report.k}  ")
    add(f"> 总耗时：{report.duration_ms / 1000:.1f}s")
    add("")

    if report.error:
        add(f"> ⚠️ **本次评估未正常完成**：`{report.error}`")
        add("")

    # ---------- 环境 ----------
    add("## 一、运行环境")
    add("")
    env = report.environment
    rerank = env.get("rerank") or {}
    add("| 项 | 值 |")
    add("| :--- | :--- |")
    add(f"| LLM | `{_esc(env.get('llm'))}` |")
    add(f"| Embedding | `{_esc(env.get('embedding'))}` |")
    add(f"| Milvus | `{_esc(env.get('milvus'))}` |")
    add(f"| 关键词路（ES） | {'启用' if env.get('es_enabled') else '停用'} |")
    add(f"| 单路召回条数 | 向量 {env.get('vector_top_k')} / BM25 {env.get('bm25_top_k')} |")
    add(f"| RRF k | {env.get('rrf_k')} |")
    add(f"| 精排 | {rerank.get('active') or '未加载'}（配置 {rerank.get('configured')}） |")
    add(f"| 答案判定后端 | `{_esc(env.get('judge'))}` |")
    add("")
    add(f"查询构成：{', '.join(f'{k} {v} 条' for k, v in sorted(report.kind_counts.items()))}")
    add("")

    # ---------- 对照实验 ----------
    add("## 二、对照实验（ablation）")
    add("")
    kinds = sorted(report.kind_counts.keys())
    header = "| 阶段 | " + " | ".join(f"Recall@3（{k}）" for k in kinds) + " | 总体 Recall | MRR | Top-1 | 平均耗时 |"
    add(header)
    add("| :--- | " + " | ".join(":---" for _ in kinds) + " | :--- | :--- | :--- | :--- |")

    for stage in report.stages:
        overall = stage.metrics()
        cells = []
        for kind in kinds:
            m = stage.metrics(kind)
            cells.append(f'{m["recall"]}/{m["total"]}')
        add(
            f'| {_esc(stage.label)} | ' + " | ".join(cells) +
            f' | **{overall["recall"]}/{overall["total"]}**'
            f' | {overall["mrr"]:.3f}'
            f' | {overall["top1"]}/{overall["total"]}'
            f' | {overall["avg_ms"]:.0f}ms |'
        )
    add("")

    if report.stages:
        final = report.stages[-1]
        add("**读法**：各检索路的**主责场景**是否 100% 达标，比总体均值更有信息量 —— "
            "总体均值会把不同场景混在一起，覆盖掉真实的变化方向。")
        add("")
        p95 = final.latency_percentile(0.95)
        add(f"最终链路检索耗时：平均 {final.metrics()['avg_ms']:.0f}ms，P95 {p95:.0f}ms")
        add("")

    # ---------- 归因 ----------
    add("## 三、Badcase 归因")
    add("")
    summary = report.attribution_summary
    if not summary or not summary.get("total"):
        add("（本轮未采集归因数据）")
        add("")
    else:
        add(f'共 {summary["total"]} 条样本（归因口径 **Top-{report.attribution_k}**），'
            f'其中 Badcase **{summary["badcases"]} 条**'
            f'（通过率 {_pct(summary.get("pass_rate"))}）。')
        add("")

        by_category = summary.get("by_category") or {}
        if by_category:
            add("### 3.1 失败分布")
            add("")
            add("| 类别 | 条数 | 含义 |")
            add("| :--- | :--- | :--- |")
            meaning = {
                CATEGORY_OK: "检索与排序均正常",
                CATEGORY_RETRIEVAL_MISS: "两条召回路都没捞到——检索层问题",
                CATEGORY_RANKING_MISS: "捞到了但被融合或精排挤出 top-k——排序层问题",
                CATEGORY_GENERATION_MISS: "检索正确但回答没利用上下文——生成层问题",
            }
            for label, count in by_category.items():
                add(f"| {_esc(label)} | {count} | {_esc(meaning.get(label, ''))} |")
            add("")

        by_stage = summary.get("by_stage") or {}
        if by_stage:
            add("最集中的失手环节：")
            add("")
            for stage_label, count in sorted(by_stage.items(), key=lambda kv: -kv[1]):
                add(f"- **{_esc(stage_label)}**：{count} 条")
            add("")

        # 明细（只列 Badcase，最多 20 条）
        badcases = [a for a in report.attributions if a.category != CATEGORY_OK]
        if badcases:
            add("### 3.2 Badcase 明细")
            add("")
            add("| 查询 | 类型 | 归因 | 漏斗（向量/BM25/融合/精排） |")
            add("| :--- | :--- | :--- | :--- |")
            for attr in badcases[:20]:
                add(
                    f"| {_esc(attr.query[:38])} | {_esc(attr.category_label)} "
                    f"| {_esc((attr.reason or '')[:70])} "
                    f"| {_esc(attr.stages.describe() if attr.stages else '')} |"
                )
            if len(badcases) > 20:
                add(f"| … | 其余 {len(badcases) - 20} 条见 JSON 报告的 badcases 字段 | | |")
            add("")

        # ---------- 结论 ----------
        add("### 3.3 结论")
        add("")
        add(_conclusion(report))
        add("")

    # ---------- 答案质量 ----------
    add("## 四、答案质量")
    add("")
    ans = report.answer_summary
    if not ans or not ans.get("total"):
        add("（本轮未执行答案质量评估。加 `--answer` 参数启用；"
            "它会打真实模型，34 条约需 2~4 分钟。）")
        add("")
    else:
        add(f'样本 {ans["total"]} 条，成功判定 {ans["evaluated"]} 条。')
        add("")
        add("| 指标 | 已判定 | 平均分 | 最低 | 最高 | 低于 0.7 |")
        add("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for name, m in (ans.get("metrics") or {}).items():
            if not m.get("evaluated"):
                add(f"| {name} | 0 | — | — | — | — |")
                continue
            add(f'| {name} | {m["evaluated"]} | {m["avg"]:.3f} | {m["min"]:.3f} '
                f'| {m["max"]:.3f} | {m["below_threshold"]} |')
        add("")
        if ans.get("failures"):
            add("判定失败的样本（**不按 0 分计入平均**，否则指标会随网关稳定性波动）：")
            add("")
            for failure in ans["failures"][:5]:
                add(f'- `{_esc(failure["question"][:50])}`：{_esc(failure["errors"])}')
            add("")

    # ---------- 备注 ----------
    if report.notes:
        add("## 五、备注")
        add("")
        for note in report.notes:
            add(f"- {_esc(note)}")
        add("")

    add("---")
    add("")
    add("> 本报告由 `scripts/run_eval.py` 生成。"
        "评估口径与 Phase 3 对照实验（`verify_hybrid_search.py`）保持一致，"
        "因此历史数值可直接对比。")
    add("")
    return "\n".join(lines)


def _conclusion(report: EvalReport) -> str:
    """
    生成「该先改哪里」的结论。

    刻意保守：样本不足时明确说「不下结论」，而不是给出一个看起来专业的建议。
    用 2 条样本的分布去指导改动，是这类报告最常见的误用方式。
    """
    summary = report.attribution_summary or {}
    total = summary.get("total") or 0
    badcases = summary.get("badcases") or 0

    if not total:
        return "本轮没有可归因的样本。"
    if badcases == 0:
        hint = ""
        if report.attribution_k == report.k:
            hint = (
                f"\n\n若想在「当前已满分」的前提下继续找优化点，"
                f"可以把判定口径收紧到 **Top-1** 重跑一次"
                f"（`--attribution-k 1`）—— 那会暴露「召回了但没排在第 1 位」的样本，"
                f"这是 Recall@{report.k} 满分时唯一还有提升空间的维度。"
            )
        return (
            f"**全部 {total} 条样本均正常**（归因口径 Top-{report.attribution_k}），"
            f"无 Badcase 可归因。\n\n"
            "此时下一轮的调参方向应该来自**扩大 Golden Set 的覆盖面**"
            "（增加更长的文档、更口语化的提问、更多标识符风格），"
            "而不是继续在当前这 34 条上做微调 —— 在满分集合上调参只会过拟合。"
            + hint
        )

    stage = report.dominant_stage
    by_stage = summary.get("by_stage") or {}
    top_count = max(by_stage.values()) if by_stage else 0
    stage_label = STAGE_LABELS.get(stage or "", stage or "未知")

    lines: List[str] = []
    lines.append(
        f"共 {badcases}/{total} 条 Badcase，最集中的失手环节是 "
        f"**{stage_label}**（{top_count} 条）。"
    )
    lines.append("")

    if top_count < MIN_SAMPLES_FOR_CONCLUSION:
        lines.append(
            f"⚠️ 该环节样本仅 {top_count} 条（不足 {MIN_SAMPLES_FOR_CONCLUSION} 条），"
            "分布差异可能来自随机性，**本轮不下强结论**。"
            "建议先扩充 Golden Set 再判断改动方向。"
        )
        return "\n".join(lines)

    if stage and stage in STAGE_SUGGESTIONS:
        lines.append(f"**排查方向**：{STAGE_SUGGESTIONS[stage]}")
        lines.append("")

    # 指向性更强的补充判断
    raw = summary.get("by_category_raw") or {}
    retrieval = raw.get(CATEGORY_RETRIEVAL_MISS, 0)
    ranking = raw.get(CATEGORY_RANKING_MISS, 0)
    generation = raw.get(CATEGORY_GENERATION_MISS, 0)

    if ranking and ranking >= retrieval:
        lines.append(
            "值得注意的是**排序层失手不少于检索层** —— 说明候选池里其实找得到答案，"
            "瓶颈在排序而非召回。这种情况下加大召回条数的收益有限，"
            "应优先看融合参数（RRF k / 两路权重）与精排表现。"
        )
    elif retrieval and retrieval >= ranking:
        lines.append(
            "**检索层失手多于排序层** —— 答案压根没进候选池，"
            "调排序不会有改善，应优先查分块策略、嵌入模型与分词覆盖。"
        )
    if generation:
        lines.append(
            f"另有 {generation} 条属于「检索对了但回答没用上」，"
            "这类问题的修法在提示词与上下文组织，与检索参数无关。"
        )

    return "\n".join(lines)


__all__ = ["render_markdown", "MIN_SAMPLES_FOR_CONCLUSION"]
