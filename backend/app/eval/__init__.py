"""
评估层 —— Golden Set + 检索指标 + 答案质量判定 + Badcase 归因

★ 这一层的核心主张

评估的价值不在于「跑出一个分数」，而在于**让下一次改动有依据**。
所以本层不是四个各管一段的脚本，而是一条闭环：

    Golden Set（标准答案）
        ↓
    跑批 → 指标（变了没有）
        ↓
    四级漏斗 → 归因（变在哪一环）
        ↓
    归因结论 → 下一轮改哪里

缺任何一环，评估都会退化成「数字看起来还不错」。

★ 模块划分

| 模块             | 职责                                   | 依赖                     |
|------------------|----------------------------------------|--------------------------|
| `golden.py`      | 评估集定义、序列化、内置受控语料         | 无                       |
| `metrics.py`     | Recall@k / MRR / nDCG / Precision      | 无（纯函数）              |
| `attribution.py` | 四级漏斗 → Badcase 三分类               | 无（纯函数）              |
| `judge.py`       | 答案质量判定**接口层**（JudgeBackend 抽象类 + evaluate_answer 封装）| 无可用后端（见下）        |
| `runner.py`      | 探针索引 + 跑批编排                     | Milvus + ES + Embedding  |
| `report.py`      | Markdown 报告 + 结论生成                | 无                       |

**历史**：本模块历史上有 BuiltinJudge（自建 LLM-as-judge）和 RagasJudge（官方 Ragas）
两个实现。BuiltinJudge 与 Ragas 官方口径可能有漂移；Ragas 会降级 langchain-openai / openai
打挂 Agent 基线。两者均已移除——官方口径评估改在与主环境完全隔离的独立虚拟环境里跑。

前四个模块里，`golden` / `metrics` / `attribution` **完全不依赖任何外部服务**，
因此可以被单测完整覆盖 —— 评估逻辑本身正确性不该靠「跑一次真实链路」来验证。

★ 零新增依赖

- 判定后端已移除，答案层指标默认跳过
- 官方 Ragas 评估在独立虚拟环境里跑，脚本与产物不入库
"""

from __future__ import annotations

from app.eval.attribution import (
    Attribution,
    FunnelStages,
    classify_generation,
    classify_retrieval,
    dominant_stage,
    summarize as summarize_attributions,
)
from app.eval.golden import (
    DeclaredTriple,
    GoldenItem,
    GoldenSet,
    KIND_MULTI_HOP,
    build_controlled_golden_set,
    ensure_default_golden_set,
    load_golden_set,
    save_golden_set,
)
from app.eval.judge import (
    AnswerEvaluation,
    evaluate_answer,
    get_judge,
    judge_status,
    reset_judge,
)
from app.eval.metrics import (
    DEFAULT_K,
    QueryOutcome,
    StageResult,
    evaluate_query,
    first_relevant_rank,
    format_line,
)
from app.eval.report import render_markdown
from app.eval.runner import (
    EvalConfig,
    EvalReport,
    default_stages,
    environment_info,
    run_answer_eval,
    run_eval,
)

__all__ = [
    "AnswerEvaluation",
    "Attribution",
    "DEFAULT_K",
    "DeclaredTriple",
    "EvalConfig",
    "EvalReport",
    "FunnelStages",
    "GoldenItem",
    "GoldenSet",
    "KIND_MULTI_HOP",
    "QueryOutcome",
    "StageResult",
    "build_controlled_golden_set",
    "classify_generation",
    "classify_retrieval",
    "default_stages",
    "dominant_stage",
    "ensure_default_golden_set",
    "environment_info",
    "evaluate_answer",
    "evaluate_query",
    "first_relevant_rank",
    "format_line",
    "get_judge",
    "judge_status",
    "load_golden_set",
    "render_markdown",
    "reset_judge",
    "run_answer_eval",
    "run_eval",
    "save_golden_set",
    "summarize_attributions",
]
