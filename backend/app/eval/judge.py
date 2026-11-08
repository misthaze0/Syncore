"""
答案质量判定 —— LLM-as-judge（接口层）

★ 三个指标（口径对齐 Ragas 官方）

| 指标                | 判的是什么                          | 典型失效场景                     |
|---------------------|-------------------------------------|----------------------------------|
| `faithfulness`      | 回答是否**只依据检索到的上下文**      | 模型用自身知识「补全」出上下文里没有的数字 |
| `answer_relevancy`  | 回答是否**真的在回答这个问题**        | 检索对了但答偏、答成通用套话       |
| `context_precision` | 检索到的上下文里**有用的占比与排序**   | 关键段被噪声挤到后面               |

★ 为什么本文件只剩接口层，没有实现

历史上有两个实现：
- **BuiltinJudge**：用项目自己的 LLM 网关 + 自建 Prompt 做 LLM-as-judge，口径对齐 Ragas
- **RagasJudge**：直接调官方 Ragas

两者都已删除。原因：
1. Ragas 0.4.x 会**降级 langchain-openai / openai**（实测 1.6.7→1.1.9 / 3.24→1.109），
   装它会打挂 Agent 的 Function Calling 基线；
2. Ragas 0.4.x API 完全重构，老实现全部不兼容；
3. 自建的 BuiltinJudge 与 Ragas 官方口径可能有微小漂移，分数不可比。

**官方口径评估改走隔离环境**：在一个与后端运行时完全分开的虚拟环境里装 Ragas 并跑
对照评估（例如 `python -m venv .venv-ragas`），这样它的降级依赖只影响那个环境，
不会把主环境的 langchain-openai / openai 拖下去。评估脚本与产物不入库。

本模块保留接口（`JudgeBackend` 抽象类、`AnswerEvaluation`、`evaluate_answer`），
但 `get_judge()` 恒返回 None——答案层指标默认跳过。

★ 两条硬约束

1. **判不了就不给分**。LLM 调用失败时 `score=None`，绝不回退到词面重叠之类的
   近似值 —— 那会让「模型判定完全无关」与「判定没跑成」在报告里长得一模一样。
   与 `reranker.py` 里「不伪造 rerank_score」是同一条原则。
2. **判定要能解释**。每条分数都必须带 `reason`。没有理由的分数无法驱动任何改动，
   只能用来做排行榜。
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence

from app.config import settings

logger = logging.getLogger(__name__)

#: context_precision 无 ground truth 时最多评判的段落数。
#: 注入 Prompt 的条数由 RERANK_TOP_N 决定（默认 5），这里留出余量即可 ——
#: 判定成本随段落数线性增长，而第 20 段的相关性对结论几乎没有影响。
MAX_CONTEXTS_JUDGED = 8


# ---------- 判定结果 ----------
@dataclass
class JudgeScore:
    metric: str
    score: Optional[float] = None
    reason: str = ""
    error: Optional[str] = None
    backend: str = "builtin"

    @property
    def ok(self) -> bool:
        return self.score is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "score": self.score,
            "reason": self.reason,
            "error": self.error,
            "backend": self.backend,
        }


def _clamp(value: Any) -> Optional[float]:
    """把模型给的分数夹到 [0, 1]；无法解析时返回 None（而不是默认 0 或 1）"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(max(0.0, min(1.0, float(value))), 4)
    if isinstance(value, str):
        try:
            return round(max(0.0, min(1.0, float(value.strip()))), 4)
        except ValueError:
            return None
    return None


# ---------- 判定后端接口 ----------
class JudgeBackend(abc.ABC):
    name = "base"

    @abc.abstractmethod
    async def faithfulness(
        self, question: str, answer: str, contexts: Sequence[str]
    ) -> JudgeScore:
        raise NotImplementedError

    @abc.abstractmethod
    async def answer_relevancy(self, question: str, answer: str) -> JudgeScore:
        raise NotImplementedError

    @abc.abstractmethod
    async def context_precision(
        self, question: str, contexts: Sequence[str]
    ) -> JudgeScore:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


# ---------- 后端工厂 ----------
# 两个实现（BuiltinJudge / RagasJudge）已移除。
# get_judge() 恒返回 None，答案层指标默认跳过。
# 官方 Ragas 评估改走与主环境隔离的独立虚拟环境，评估脚本不入库。
_DEGRADED_REASON = (
    "判定后端已移除：自建 BuiltinJudge 与官方 RagasJudge 均不再维护。"
    "官方口径评估请在独立虚拟环境里跑，避免 Ragas 拖降主环境的 langchain-openai / openai。"
)

_backend: Optional[JudgeBackend] = None
_backend_error: Optional[str] = _DEGRADED_REASON


def get_judge(force_reload: bool = False) -> Optional[JudgeBackend]:
    """
    取判定后端。**构造失败返回 None 而不是降级到某个「近似」实现** ——
    答案质量的近似值比没有值更危险：它会被当成真信号写进结论。

    本模块历史上有 BuiltinJudge（自建 LLM-as-judge）和 RagasJudge（官方 Ragas）
    两个实现，均已移除。官方口径评估请在独立虚拟环境里跑。
    """
    global _backend, _backend_error

    # 永远返回 None —— 没有可用的判定后端
    if _backend is not None and force_reload:
        _backend = None
    _backend_error = _DEGRADED_REASON
    return None


def reset_judge() -> None:
    global _backend, _backend_error
    _backend = None
    _backend_error = _DEGRADED_REASON


def judge_status() -> Dict[str, Any]:
    """
    判定后端状态。

    ⚠️ 这里必须**先解析后端**再取快照。判定后端是惰性单例，不主动构造的话
    `_backend` 恒为 None，`available` 就永远是 False —— 启动日志会写着
    「答案层指标将被跳过」而实际上完全可用。这与 `environment_info()` 里
    精排状态显示 `not_loaded` 是同一类坑：**元数据说错了，比没有元数据更糟**，
    因为它会让人去排查一个不存在的问题。
    """
    if _backend is None:
        get_judge()

    return {
        "configured": settings.EVAL_JUDGE_BACKEND,
        "active": _backend.name if _backend else None,
        "model": settings.EVAL_JUDGE_MODEL or settings.LLM_MODEL,
        "available": _backend is not None,
        "degraded_reason": _backend_error,
    }


# ---------- 高层封装 ----------
@dataclass
class AnswerEvaluation:
    """一条样本的答案质量评估结果"""

    question: str = ""
    answer: str = ""
    scores: Dict[str, Optional[float]] = field(default_factory=dict)
    reasons: Dict[str, str] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    backend: str = ""

    @property
    def evaluated(self) -> bool:
        return any(v is not None for v in self.scores.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "scores": self.scores,
            "reasons": self.reasons,
            "errors": self.errors,
            "backend": self.backend,
        }


async def evaluate_answer(
    question: str,
    answer: str,
    contexts: Sequence[str],
    *,
    backend: Optional[JudgeBackend] = None,
) -> AnswerEvaluation:
    """
    对一条回答跑完三个指标。

    单条指标失败不中断其余指标 —— 评估要打十几次真实模型调用，
    一条失败就整批作废，会让评估脚本在真实使用中完全不可靠。

    当前无可用判定后端（get_judge 恒返回 None），直接返回带错误标记的空结果。
    官方 Ragas 评估改在独立虚拟环境里跑，脚本不入库。
    """
    backend = backend or get_judge()
    result = AnswerEvaluation(question=question, answer=answer)

    if backend is None:
        result.errors["_backend"] = _DEGRADED_REASON
        return result

    result.backend = backend.name

    for metric, coro in (
        ("faithfulness", backend.faithfulness(question, answer, contexts)),
        ("answer_relevancy", backend.answer_relevancy(question, answer)),
    ):
        try:
            score = await coro
        except Exception as e:  # noqa: BLE001
            score = JudgeScore(metric=metric, error=f"{type(e).__name__}: {e}")

        if score.error:
            result.errors[metric] = score.error
        else:
            result.scores[metric] = score.score
            result.reasons[metric] = score.reason

    return result
