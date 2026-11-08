# -*- coding: utf-8 -*-
"""
工作流的数据契约与图校验。

★ 图是唯一真相，位置只是画布坐标

节点与边直接按前端画布的形状落盘（`nodes` / `edges`），执行层不重新推导
「谁连谁」：画布上的线就是程序的控制流。`x` / `y` 只服务渲染，改位置不算改逻辑。

★ 校验放在保存之前，而不是执行时

一张有环、有断头路、有两个开始节点的图，如果在保存时能溜过去，
用户只会在某次运行到一半时看到一个莫名其妙的错误 —— 而且错的是上次运行
已经跑过的前半段（还花了 token）。所以 `validate_graph` 是保存与执行**共用**
的唯一闸门，错误信息直接指出节点标题。

★ 为什么限制成六种节点

start / llm / knowledge / tool / condition / answer 覆盖了「取输入 → 生成 →
查知识 → 调工具 → 分支 → 回答案」这条最小闭环。节点类型越多，节点间的
变量契约越难讲清；第一版宁可控得死一点。
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Optional, TypedDict

# ---------- 节点类型 ----------

NODE_START = "start"
NODE_LLM = "llm"
NODE_KNOWLEDGE = "knowledge"
NODE_TOOL = "tool"
NODE_CONDITION = "condition"
NODE_ANSWER = "answer"

NODE_TYPES = (NODE_START, NODE_LLM, NODE_KNOWLEDGE, NODE_TOOL, NODE_CONDITION, NODE_ANSWER)

NODE_LABELS = {
    NODE_START: "开始",
    NODE_LLM: "模型",
    NODE_KNOWLEDGE: "知识检索",
    NODE_TOOL: "工具",
    NODE_CONDITION: "条件",
    NODE_ANSWER: "回答",
}

#: 画布生成的节点 id / 边 id 都必须满足它（它也会原样回传，不进文件名，但仍是注入面）
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 条件节点的两个出边句柄
HANDLE_TRUE = "true"
HANDLE_FALSE = "false"
CONDITION_HANDLES = (HANDLE_TRUE, HANDLE_FALSE)

CONDITION_OPERATORS = (
    "contains",
    "not_contains",
    "equals",
    "not_equals",
    "starts_with",
    "ends_with",
    "is_empty",
    "not_empty",
    "gt",
    "gte",
    "lt",
    "lte",
)

OPERATOR_LABELS = {
    "contains": "包含",
    "not_contains": "不包含",
    "equals": "等于",
    "not_equals": "不等于",
    "starts_with": "开头是",
    "ends_with": "结尾是",
    "is_empty": "为空",
    "not_empty": "不为空",
    "gt": "大于",
    "gte": "大于等于",
    "lt": "小于",
    "lte": "小于等于",
}

# ---------- 运行状态 ----------

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_ORPHANED = "orphaned"

RUN_ALL_STATUSES = frozenset({RUN_RUNNING, RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED, RUN_ORPHANED})
RUN_TERMINAL_STATUSES = frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED, RUN_ORPHANED})

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_ASSISTANT = "assistant"

#: 单个节点输出写进运行记录时的上限（详情页是速览，不是全文仓库）
STEP_OUTPUT_CHARS = 4000
#: 整次运行的最终答案上限（会显示给用户，但不该无限制撑爆 JSON）
RUN_OUTPUT_CHARS = 20000


def make_workflow_id() -> str:
    return uuid.uuid4().hex[:16]


def make_run_id() -> str:
    return uuid.uuid4().hex[:16]


def is_valid_workflow_id(value: str) -> bool:
    from app.conversations.store import is_valid_thread_id

    return is_valid_thread_id(value)


def is_valid_node_id(value: str) -> bool:
    return bool(_NODE_ID_RE.match(value or ""))


def run_is_terminal(status: str) -> bool:
    return status in RUN_TERMINAL_STATUSES


# ---------- 记录形状 ----------


class WorkflowNode(TypedDict, total=False):
    id: str
    type: str
    title: str
    x: float
    y: float
    config: dict[str, Any]


class WorkflowEdge(TypedDict, total=False):
    id: str
    source: str
    target: str
    source_handle: str


class WorkflowRecord(TypedDict, total=False):
    workflow_id: str
    name: str
    description: str
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]
    created_at: str
    updated_at: str
    #: 最近一次运行的派生快照（列表页不必逐条翻运行文件）
    last_run_at: str
    last_status: str


class RunStep(TypedDict, total=False):
    node_id: str
    title: str
    type: str
    status: str
    started_at: str
    finished_at: str
    output: str
    error: str
    branch: str


class WorkflowRun(TypedDict, total=False):
    run_id: str
    workflow_id: str
    status: str
    trigger: str
    input: str
    output: str
    error: str
    current_node: str
    started_at: str
    finished_at: str
    steps: list[RunStep]


def _now_iso() -> str:
    from app.conversations.models import now_iso

    return now_iso()


def _clean_nodes(raw: Any) -> list[WorkflowNode]:
    nodes: list[WorkflowNode] = []
    if not isinstance(raw, list):
        return nodes
    for item in raw:
        if not isinstance(item, dict):
            continue
        node: WorkflowNode = WorkflowNode(
            id=str(item.get("id") or ""),
            type=str(item.get("type") or ""),
            title=str(item.get("title") or ""),
            config=dict(item.get("config") or {}) if isinstance(item.get("config"), dict) else {},
        )
        for key in ("x", "y"):
            try:
                node[key] = float(item.get(key) or 0)  # type: ignore[literal-required]
            except (TypeError, ValueError):
                node[key] = 0.0  # type: ignore[literal-required]
        nodes.append(node)
    return nodes


def _clean_edges(raw: Any) -> list[WorkflowEdge]:
    edges: list[WorkflowEdge] = []
    if not isinstance(raw, list):
        return edges
    for item in raw:
        if not isinstance(item, dict):
            continue
        edge = WorkflowEdge(
            id=str(item.get("id") or ""),
            source=str(item.get("source") or ""),
            target=str(item.get("target") or ""),
        )
        handle = str(item.get("source_handle") or item.get("sourceHandle") or "")
        if handle:
            edge["source_handle"] = handle
        edges.append(edge)
    return edges


def new_workflow_record(
    *,
    workflow_id: str,
    name: str,
    description: str = "",
    nodes: Any = None,
    edges: Any = None,
) -> WorkflowRecord:
    now = _now_iso()
    return WorkflowRecord(
        workflow_id=workflow_id,
        name=(name or "").strip(),
        description=(description or "").strip(),
        nodes=_clean_nodes(nodes),
        edges=_clean_edges(edges),
        created_at=now,
        updated_at=now,
    )


def workflow_from_dict(data: dict[str, Any]) -> Optional[WorkflowRecord]:
    """从磁盘还原；**不信任磁盘内容**（坏记录直接丢弃，不让它进执行）。"""
    wid = str(data.get("workflow_id") or "")
    if not is_valid_workflow_id(wid):
        return None
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    record = WorkflowRecord(
        workflow_id=wid,
        name=name,
        description=str(data.get("description") or ""),
        nodes=_clean_nodes(data.get("nodes")),
        edges=_clean_edges(data.get("edges")),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
    )
    for key in ("last_run_at", "last_status"):
        value = data.get(key)
        if isinstance(value, str) and value:
            record[key] = value  # type: ignore[literal-required]
    return record


def run_from_dict(data: dict[str, Any]) -> Optional[WorkflowRun]:
    run_id = str(data.get("run_id") or "")
    wid = str(data.get("workflow_id") or "")
    if not is_valid_workflow_id(run_id) or not is_valid_workflow_id(wid):
        return None
    status = str(data.get("status") or "")
    if status not in RUN_ALL_STATUSES:
        return None
    run = WorkflowRun(
        run_id=run_id,
        workflow_id=wid,
        status=status,
        trigger=str(data.get("trigger") or TRIGGER_MANUAL),
        input=str(data.get("input") or ""),
        started_at=str(data.get("started_at") or ""),
    )
    for key in ("output", "error", "current_node", "finished_at"):
        value = data.get(key)
        if isinstance(value, str) and value:
            run[key] = value  # type: ignore[literal-required]
    steps = data.get("steps")
    if isinstance(steps, list) and steps:
        run["steps"] = [s for s in steps if isinstance(s, dict)]  # type: ignore[literal-required]
    return run


def cap_output(text: str, limit: int = STEP_OUTPUT_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


# ---------- 图校验 ----------


def validate_graph(nodes: Any, edges: Any) -> list[str]:
    """校验一张图；返回错误清单（空 = 合法）。

    同时被保存接口与执行引擎调用 —— 两份校验必然漂移，所以只留这一份。
    """
    errors: list[str] = []
    nodes_list = nodes if isinstance(nodes, list) else []
    edges_list = edges if isinstance(edges, list) else []

    if not nodes_list:
        return ["画布上还没有节点"]

    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes_list:
        if not isinstance(node, dict):
            errors.append("节点格式非法")
            continue
        node_id = str(node.get("id") or "")
        node_type = str(node.get("type") or "")
        title = str(node.get("title") or "") or node_id or "未命名节点"
        if not is_valid_node_id(node_id):
            errors.append(f"节点 id 非法: {node_id!r}")
            continue
        if node_id in by_id:
            errors.append(f"节点 id 重复: {node_id}")
            continue
        if node_type not in NODE_TYPES:
            errors.append(f"「{title}」的类型不受支持: {node_type or '（空）'}")
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if node_type == NODE_LLM and not str(config.get("prompt") or "").strip():
            errors.append(f"「{title}」还没写提示词")
        if node_type == NODE_KNOWLEDGE and not str(config.get("query") or "").strip():
            errors.append(f"「{title}」还没写检索内容")
        if node_type == NODE_TOOL and not str(config.get("tool") or "").strip():
            errors.append(f"「{title}」还没选工具")
        if node_type == NODE_CONDITION:
            operator = str(config.get("operator") or "")
            if operator not in CONDITION_OPERATORS:
                errors.append(f"「{title}」的条件运算符不受支持")
            if not str(config.get("left") or "").strip() and operator not in ("is_empty", "not_empty"):
                errors.append(f"「{title}」左侧条件不能为空")
        if node_type == NODE_ANSWER and not str(config.get("template") or "").strip():
            errors.append(f"「{title}」还没写回答内容")
        by_id[node_id] = node

    starts = [n for n in by_id.values() if str(n.get("type")) == NODE_START]
    if len(starts) != 1:
        errors.append("必须有且只有一个开始节点" if not starts else "只能有一个开始节点")
    if not any(str(n.get("type")) == NODE_ANSWER for n in by_id.values()):
        errors.append("至少需要一个回答节点")

    out_map: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in by_id}
    in_map: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in by_id}
    edge_ids: set[str] = set()
    for edge in edges_list:
        if not isinstance(edge, dict):
            errors.append("连线格式非法")
            continue
        edge_id = str(edge.get("id") or "")
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if edge_id in edge_ids:
            errors.append(f"连线 id 重复: {edge_id}")
        edge_ids.add(edge_id)
        if source not in by_id or target not in by_id:
            errors.append("有连线指向不存在的节点")
            continue
        out_map[source].append(edge)
        in_map[target].append(edge)

    if errors:
        return errors

    start_id = str(starts[0].get("id"))

    def title_of(node_id: str) -> str:
        return str(by_id[node_id].get("title") or node_id)

    for node_id, node in by_id.items():
        node_type = str(node.get("type"))
        out = out_map[node_id]
        incoming = in_map[node_id]
        title = title_of(node_id)
        if node_type == NODE_START:
            if incoming:
                errors.append(f"开始节点「{title}」不能有上游")
            if len(out) != 1:
                errors.append("开始节点必须连出一条线")
        elif node_type == NODE_ANSWER:
            if out:
                errors.append(f"回答节点「{title}」不能再往下连")
        elif node_type == NODE_CONDITION:
            handles = [str(e.get("source_handle") or "") for e in out]
            if len(out) != 2 or sorted(handles) != sorted(CONDITION_HANDLES):
                errors.append(f"条件节点「{title}」必须分别连出「真」和「假」两条线")
        else:
            if len(out) > 1:
                errors.append(f"「{title}」只能连出一个下一步")
            if not out:
                errors.append(f"「{title}」还没连接到下一步")

    # 环检测：Kahn 拓扑排序。有环时剩余节点就是环上/环下游的节点。
    indegree = {node_id: len(in_map[node_id]) for node_id in by_id}
    queue = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for edge in out_map.get(current, []):
            target = str(edge.get("target"))
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(by_id):
        errors.append("工作流里存在环路，执行会绕不出来")

    # 可达性：任何节点都必须是开始节点能走到的（否则它只是画布上的摆设）
    reachable: set[str] = set()
    frontier = [start_id]
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        for edge in out_map.get(current, []):
            frontier.append(str(edge.get("target")))
    unreachable = [title_of(nid) for nid in by_id if nid not in reachable]
    if unreachable:
        errors.append("这些节点从开始节点走不到：" + "、".join(unreachable))
    if not any(
        str(by_id[nid].get("type")) == NODE_ANSWER for nid in reachable
    ) and not any("回答节点" in e for e in errors):
        errors.append("从开始节点走不到任何回答节点")

    return errors


def next_edges(edges: list[WorkflowEdge], node_id: str, *, handle: str = "") -> list[WorkflowEdge]:
    """取某个节点的出边；给 handle 时只取该分支（条件节点用）。"""
    out = [e for e in edges if str(e.get("source")) == node_id]
    if handle:
        out = [e for e in out if str(e.get("source_handle") or "") == handle]
    return out


__all__ = [
    "CONDITION_HANDLES",
    "CONDITION_OPERATORS",
    "HANDLE_FALSE",
    "HANDLE_TRUE",
    "NODE_ANSWER",
    "NODE_CONDITION",
    "NODE_KNOWLEDGE",
    "NODE_LABELS",
    "NODE_LLM",
    "NODE_START",
    "NODE_TOOL",
    "NODE_TYPES",
    "OPERATOR_LABELS",
    "RUN_ALL_STATUSES",
    "RUN_CANCELLED",
    "RUN_FAILED",
    "RUN_ORPHANED",
    "RUN_OUTPUT_CHARS",
    "RUN_RUNNING",
    "RUN_SUCCEEDED",
    "RUN_TERMINAL_STATUSES",
    "STEP_OUTPUT_CHARS",
    "TRIGGER_ASSISTANT",
    "TRIGGER_MANUAL",
    "TRIGGER_SCHEDULED",
    "RunStep",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowRecord",
    "WorkflowRun",
    "cap_output",
    "is_valid_node_id",
    "is_valid_workflow_id",
    "make_run_id",
    "make_workflow_id",
    "new_workflow_record",
    "next_edges",
    "run_from_dict",
    "run_is_terminal",
    "validate_graph",
    "workflow_from_dict",
]
