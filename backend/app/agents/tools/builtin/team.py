"""员工按需读取同一工作空间的团队工作记录；私聊内容不在共享范围内。"""

import json

from app.agents.tools.context import ToolContext
from app.agents.tools.decorator import agent_tool
from app.tasks.store import task_store
from app.workspace.store import workspace_store


@agent_tool(
    display_name="查看团队工作记录",
    description="查看同事的团队工作记录。留空列出同事与近期任务；填 task_id 读取任务进度和结果。可用 offset 分页读取长结果。不会读取用户与同事之间的对话。",
    category="团队协作", danger_level="safe", read_only=True,
)
def team_work(task_id: str = "", offset: int = 0, ctx: ToolContext | None = None) -> str:
    """查询同工作空间的协作记录。

    Args:
        task_id: 留空查看团队工作列表；否则填列表中的任务 id
        offset: 工作结果正文的起始字符位置，默认 0
    """
    caller = workspace_store.get_employee(ctx.employee_id) if ctx and ctx.employee_id else None
    if not caller or not caller.get("enabled", True):
        return "只有当前工作空间的在职员工可以查询团队记录。"
    workspace_id = caller["workspace_id"]
    members = workspace_store.list_employees(workspace_id=workspace_id)
    member_ids = {m["id"] for m in members}

    def shared(record: dict) -> bool:
        group_id = str(record.get("group_id") or "")
        group = workspace_store.get_group(group_id) if group_id else None
        return bool(group and group["workspace_id"] == workspace_id and record.get("employee_id") in member_ids)

    try:
        if not task_store.enabled:
            return "团队工作记录暂时不可用。"
        if not task_id:
            result = []
            for member in members:
                records, _ = task_store.list(employee_id=member["id"], limit=20)
                result.append({"employee_id": member["id"], "name": member["name"],
                               "role": member.get("tagline", ""),
                               "tasks": [{"task_id": r["task_id"], "title": r.get("title"),
                                          "status": r.get("status")} for r in records if shared(r)][:5]})
            return json.dumps(result, ensure_ascii=False)
        record = task_store.get(task_id)
        if not record or not shared(record):
            return "找不到当前工作空间可共享的这条团队记录。"
        view = task_store.progress(task_id, limit=30)
        result = str(record.get("result") or "")
        start = max(0, int(offset))
        # 内容作为工具数据返回，不作为系统指令注入，也不写入群聊。
        from app.workspace.artifact_store import artifact_store
        return json.dumps({
            "task_id": task_id, "employee_id": record.get("employee_id"),
            "title": record.get("title"), "status": record.get("status"),
            "progress": [{k: str(e.get(k) or "")[:1200] for k in ("label", "detail", "text", "status")}
                         for e in (view.get("entries") or [])],
            "result": result[start:start + 12000], "total_chars": len(result),
            "next_offset": start + 12000 if start + 12000 < len(result) else None,
            "error": str(record.get("error") or "")[:1000],
            "artifacts": artifact_store.list(
                str(record.get("run_id") or ""), str(record.get("board_card_id") or "")),
        }, ensure_ascii=False)
    except Exception:
        return "团队工作记录暂时读取失败，请稍后重试。"


@agent_tool(
    display_name="联系数字同事",
    description="向本群同事请求具体资料或答复一条消息。请求异步投递，继续可做的工作；不要反复追问同一件事。回复时填写 reply_to。",
    category="团队协作", danger_level="safe", read_only=False,
)
async def team_send(content: str, recipient_employee_id: str = "", reply_to: str = "",
                    request_reply: bool = True, ctx: ToolContext | None = None) -> str:
    from app.config import settings
    from app.workspace.inbox import inbox
    from app.workspace.messaging import messenger
    from app.workspace.run_store import run_store

    caller = workspace_store.get_employee(ctx.employee_id) if ctx and ctx.employee_id else None
    if not caller or not caller.get("enabled", True):
        return "只有在职数字员工可以联系同事。"
    run = run_store.get(ctx.run_id)
    if not run or run["terminal"]:
        return "只能在当前进行中的团队任务内联系同事。"
    group = workspace_store.get_group(run["group_id"])
    if not group or group["workspace_id"] != caller["workspace_id"]:
        return "当前协作不属于你的工作空间。"
    parent = workspace_store.get_message(reply_to) if reply_to else None
    if reply_to:
        addressed = parent and inbox.db.query_one("SELECT 1 FROM deliveries WHERE message_id=? AND employee_id=?", (reply_to, caller["id"]))
        if not parent or parent["group_id"] != group["id"] or (parent.get("recipient_employee_id") != caller["id"] and not addressed):
            return "只能答复当前群中发给自己的消息。"
        recipient_employee_id = str(parent.get("employee_id") or "")
        request_reply = False
    members = {m["id"] for m in workspace_store.list_members(group["id"]) if m.get("enabled", True)}
    if caller["id"] not in members or (recipient_employee_id and recipient_employee_id not in members):
        return "收发双方必须是当前群的在职成员。"
    if request_reply and (not recipient_employee_id or recipient_employee_id == caller["id"]):
        return "请求需要指定另一位同事。"
    if workspace_store.count_directed(run["id"]) >= settings.NEGOTIATION_MAX_MESSAGES_PER_RUN:
        return "本次同事往返消息已达到上限，请汇报尚未解决的问题。"
    depth = int((parent or {}).get("meta", {}).get("handoff_depth", 0)) + 1
    if depth > settings.HANDOFF_MAX_DEPTH:
        return "这条交接已达到深度上限，请汇报阻碍。"
    if request_reply:
        for existing in inbox.pending(recipient_employee_id, run["id"]):
            if existing.get("employee_id") == caller["id"] and existing["content"] == content.strip():
                return f"相同请求仍待处理，message_id={existing['id']}"
    msg = workspace_store.append_message(group["id"], employee_id=caller["id"],
        kind="request" if request_reply else "deliverable", content=content, run_id=run["id"],
        task_id=ctx.task_id, recipient_employee_id=recipient_employee_id, reply_to=reply_to,
        meta={"handoff_depth": depth})
    if not msg:
        return "消息没有写入，未投递。"
    if reply_to:
        inbox.resolve(reply_to, caller["id"])
    if request_reply:
        messenger.wake(recipient_employee_id)
    return json.dumps({"message_id": msg["id"], "status": "pending" if request_reply else "answered"}, ensure_ascii=False)


@agent_tool(
    display_name="记录本人工作经验",
    description="保存当前员工自己的经验，不会自动共享给其他员工。项目共享事实由用户在工作空间明确发布。",
    category="团队协作", danger_level="safe", read_only=False,
)
def team_remember(key: str, value: str, evidence: str = "", ctx: ToolContext | None = None) -> str:
    from app.config import settings
    if not settings.MEMORY_ENABLED:
        return "长期记忆已关闭，没有保存本人经验。"
    from app.workspace.scoped_memory import scoped_memory
    caller = workspace_store.get_employee(ctx.employee_id) if ctx and ctx.employee_id else None
    if not caller or not caller.get("enabled", True):
        return "当前没有有效的员工身份。"
    scoped_memory.put("employee", caller["id"], key, value, evidence, caller["id"])
    return "已记录为本人的工作经验。"
