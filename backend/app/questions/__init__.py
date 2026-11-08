"""
模型主动提问（结构化问答）—— 与宿主审批并列的第二种「停下来等人」。

审批回答的是「这一步危不危险，允不允许」；提问回答的是「信息不够，选哪个」。
两者共用 LangGraph 的挂起/恢复机制，但**不共用事件与留痕**：
审批的 `interrupt` 由宿主判定产生，提问的 `question` 由模型调用 `ask_user` 产生。
把它们混成一种事件，会让审计再也答不出「这次是授权还是澄清」。

模块分工：
    models.py   数据契约、参数归一化、回答归一化、给模型的结果文案
    records.py  问答留痕（append-only JSONL + 内存环）
    broker.py   按四元组定位的等待/唤醒协调器
    flow.py     唯一的事件/留痕/通知编排入口（两个驱动层都只调用它）
"""

from app.questions.broker import QuestionOutcome, question_broker
from app.questions.flow import question_gate, read_question_answer, run_pending_question

__all__ = [
    "QuestionOutcome",
    "question_broker",
    "question_gate",
    "read_question_answer",
    "run_pending_question",
]
