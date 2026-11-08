"""
群组编排器 —— 主持人 LLM 拆解 → 派发成员后台并行 → 自动汇报 → 汇总结论。

★ 可见性隔离（需求 3、4）的**唯一写入点**

群组消息（deliverable / summary / system 这些成品文本）只有两条产生路径：
1. 本模块的 `on_employee_task_done` —— 员工任务到终态后，由**该员工自己打一次模型**
   产出几句群聊汇报（施工图 §4.4）；
2. 本模块的 `start` / `_finalize` —— 主持人写的拆解计划与最终结论。

员工内部思考、逐字 token、工具调用留痕**永远不写这里**：它们只走
`employee_stream.get_employee_event_stream`（私聊 SSE）+ `conversation_store`
（`emp_<id>` 线程）。两条通道在存储、通道、页面三层物理分离。

★ 为什么汇报不再直接贴任务 `result`（2026-10-16 主动更正）

早先的 rationale 是「result 已经是干净回答，再打一次模型换不来实质收益」。
在用户口径下这条判断是错的：换不来的正是**同事的口气与长度**。
几千字全文贴进时间线，读的人拿不到「他交了什么」，只能自己再做一遍摘要 ——
那等于把协调成本退还给用户。所以交付摘要由每个员工自己打一次模型产出，
输入截到 `REPORT_INPUT_CHARS` 并明说截断过。

★ 不许退回规则化摘要

截前 N 字 / 取首段 / 正则抠结论都不许：那是把「模型没干活」伪装成「员工汇报得很简洁」，
与本项目一路在防的静默失效同形。摘要失败就如实写一条「⚠️ 汇报摘要没有生成（原因），
全文在他的私聊工作台」—— 这条失败对用户仍然是可用的信息，因为它指向了全文所在的地方。

★ 为什么 `on_employee_task_done` 是 async

摘要要在 `concluding` 之前落时间线（汇总结论读的是「成员各自说了什么」），
fire-and-forget 会让时间线上出现「先散会后发言」。所以执行器在 `_run` 的 `finally` 里
`await` 它。而**销账**（`_settle_run`）写在这段 await 外面的 `finally` 里 ——
摘要再慢、再被取消，账都必须销掉（「取消后 run 永不收口」那个坑的修法）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Optional

from app.config import settings
from app.tasks.models import SOURCE_BOARD
from app.tasks.store import task_store
from app.workspace import coordinator, events
from app.workspace.board import (
    CARD_BLOCKED,
    CARD_IN_PROGRESS,
    CREATED_BY_SYSTEM,
    CardDependencyError,
    board,
)
from app.workspace.bus import GroupEventBus, group_bus
from app.workspace.budget import run_scoped
from app.workspace.employee_runtime import persona_fragment
from app.workspace.llm_io import llm_attempt as _llm_attempt
from app.workspace.llm_io import llm_text as _llm_text
from app.workspace.run_store import (
    STATUS_COMPLETED,
    STATUS_REVIEW,
    STATUS_PARTIAL,
    STATUS_CONCLUDING,
    STATUS_FAILED,
    STATUS_RUNNING,
    run_store,
)
from app.workspace.store import workspace_store
from app.workspace.models import normalize_base_agent

logger = logging.getLogger(__name__)

#: 单条群组消息（汇报/结论）的正文上限。超长截断并**明说**（与 `PROGRESS_TEXT_CHARS`
#: 同一约定：截断必须可察觉，不能悄悄切）。
MESSAGE_CHARS = 4000

#: 送入结论汇总的单成员成果上限（防撑爆上下文）。
SUMMARY_ENTRY_CHARS = 1500


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…[已截断]"


# ---------- 主持人 LLM 辅助 ----------
# `_llm_text` / `_parse_json` 从 `app.workspace.llm_io` 引进来（别名保持原名：
# 测试是 monkeypatch `orchestrator._llm_text` 这个**模块属性**来给主持人打桩的，
# 换成函数内 import 就换不掉了）。


# ---------- 编排器 ----------


class GroupOrchestrator:
    """协调拆解、认领、执行和收尾；内存上下文可从持久化运行账恢复。

    `_task_to_run` 是路由缓存，执行记录中的 run_id 是恢复依据。
    `done_results` 按 task_id 保存成果，避免同一员工多次交付互相覆盖。
    单进程协程负责运行，recovery 模块根据派发意图、执行记录和检查点对账。
    """

    def __init__(self) -> None:
        #: run_id → 本次编排的收尾上下文（group_id / instruction / 成果）
        self._runs: dict[str, dict[str, Any]] = {}
        #: task_id → run_id（反向索引，供 `on_employee_task_done` 定位）
        self._task_to_run: dict[str, str] = {}
        #: run_id → 后台流水线协程（`pipeline_task()` 用它等人跑完，跑完自动摘掉）
        self._pipelines: dict[str, asyncio.Task] = {}
        self._finalizers: dict[str, asyncio.Task] = {}
        self._closing = False

    # ---------- 派发 ----------

    async def start(
        self,
        group_id: str,
        instruction: str,
        *,
        web: bool = True,
        graph: bool = True,
        model: str = "",
        target_employee_id: str = "",
        client_id: str = "",
        conversational: bool = False,
    ) -> str:
        """
        触发一次协作，**立即返回 run_id**（进度经群组 SSE 推送）。

        流程（施工图 §16.1）：建 run 行 → 协调者拆卡（**不带 owner**）→ 卡片落任务板 →
        认领轮（成员自己说「这张我要做」，CAS 抢卡）→ 抢到才派发到他的工作台。

        ★ 拆解与认领都在后台
        `run_store.create()` 必须同步走完：两个标签页同时点时，第二个要**立刻**拿到 409，
        而不是两个都开跑。之后那 1 + 成员数 次模型调用（拆解一次、认领轮每人一次）
        全在 `create_task` 里 —— 改之前这条 HTTP 要等 2~20s 才返回，
        而前端把返回值丢了就再也不知道那次协作的 id。
        需要等这一轮跑完（测试、或对账）用 `pipeline_task(run_id)`。

        ★ 建卡 ≠ 指派
        过去「主持人决定谁做」那条路（`_plan` 输出 `employee_id`）已经删掉：
        `coordinator.decompose()` 的结果里没有成员 id 这一项，`board.create()` 也没有
        owner 参数。拆解失败时落的是**一张未拆分的原始卡**（`created_by=system`），
        在板上等认领 —— 而不是像改之前那样把整条指令压给 `members[0]`
        （ADR 0020 明令否决的工头制单点派发，且时间线上看不出发生过）。

        ★ `web` / `graph` / `model`
        它们会落进 `TaskRecord` 并被 `sweep_orphans` 在重启后复用 —— 传错一次，错到任务结束。
        """
        members = [m for m in workspace_store.list_members(group_id) if m.get("enabled", True)]
        if target_employee_id:
            members = [m for m in members if m["id"] == target_employee_id]
        run_id = run_store.create(
            group_id=group_id,
            instruction=instruction,
            client_id=client_id,
            # 只有一个成员时不做拆解（只要有启用成员就必打一次模型分工，是纯形式主义开销）。
            # direct 是**明说**的：时间线会写「未做拆解」。
            mode="direct" if len(members) == 1 else "negotiated",
        )
        # ★ 收尾上下文要在派发**之前**建好：`executor.start` 里派出去的协程可能几毫秒就
        #   跑到终态并回调 `on_employee_task_done`，那时 `_runs[run_id]` 还不存在的话
        #   `_settle_run` 找不到账，这个 run 就永不收口（又是永久 spinner）。
        self._runs[run_id] = {
            "group_id": group_id,
            "instruction": instruction,
            "done_results": {},
            "finalized": False,
            "preparing": True,
            "conversational": conversational,
            "public_context": self._group_context(group_id),
        }
        from app.workspace.budget import run_budget
        run_budget.ensure(run_id)
        from app.workspace.db import workspace_db
        from app.permissions.policy import effective_mode
        import json
        workspace_db.execute("INSERT OR REPLACE INTO run_options(run_id,payload) VALUES(?,?)",
                             (run_id, json.dumps({"web": web, "graph": graph, "model": model,
                                "target_employee_id": target_employee_id, "permission_mode": effective_mode()})))
        if conversational:
            options = json.loads(workspace_db.query_one("SELECT payload FROM run_options WHERE run_id=?", (run_id,))["payload"])
            options["conversational"] = True
            workspace_db.execute("UPDATE run_options SET payload=? WHERE run_id=?", (json.dumps(options), run_id))
            self._runs[run_id]["preparing"] = False
            self._runs[run_id]["members"] = members
            run_store.advance(run_id, STATUS_RUNNING)
        workspace_store.append_message(
            group_id, employee_id="", kind="request" if conversational else "goal", content=instruction,
            sender_kind="user", run_id=run_id,
            meta={"client_id": client_id} if client_id else {},
            recipient_employee_id=members[0]["id"] if conversational and members else "",
        )

        if not members:
            self._abort_run(
                run_id,
                group_id,
                content="⚠️ 该群组还没有可用的数字员工，无法开始协作。请先在群组里添加成员。",
                error="没有可用成员",
            )
            return run_id

        if conversational:
            from app.workspace.messaging import messenger
            messenger.wake(members[0]["id"])
            return run_id

        self._schedule_pipeline(
            run_id, group_id=group_id, instruction=instruction, members=members,
            web=web, graph=graph, model=model,
        )
        return run_id

    def pipeline_task(self, run_id: str) -> Optional[asyncio.Task]:
        """
        这一次 run 的后台流水线句柄（可能已经跑完）。

        存在不是为了炫技：调用方（测试、重启对账）需要一条**不靠 sleep 轮询**的
        等待方式（testing.md 禁 sleep 轮询）。
        """
        return self._pipelines.get(run_id)

    def _schedule_pipeline(
        self,
        run_id: str,
        *,
        group_id: str,
        instruction: str,
        members: list[dict[str, Any]],
        web: bool,
        graph: bool,
        model: str,
    ) -> None:
        """派发流水线（fire-and-forget）。没有事件循环时直接判失败，不静默吞。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._abort_run(
                run_id,
                group_id,
                content="⚠️ 当前没有运行中的事件循环，这次协作没有开始。请重试。",
                error="无运行中的事件循环",
            )
            return
        task = loop.create_task(
            self._pipeline(run_id, group_id, instruction, members, web=web, graph=graph, model=model)
        )
        self._pipelines[run_id] = task
        task.add_done_callback(lambda done, rid=run_id: self._pipelines.pop(rid, None) if self._pipelines.get(rid) is done else None)

    async def _pipeline(
        self,
        run_id: str,
        group_id: str,
        instruction: str,
        members: list[dict[str, Any]],
        *,
        web: bool,
        graph: bool,
        model: str,
    ) -> None:
        """
        一次协作的正身：拆卡 → 落板 → 认领轮 → 派发。

        ★ 整条路包在一个 try 里：这里崩了必须把这个 run 判死，
        否则它永远停在 `running`，既转着 spinner，又占住这个群的活跃位
        （用户连重新发起都做不到）。
        """
        from app.workspace.budget import current_run
        budget_context = current_run.set(run_id)
        try:
            cards, failure = await self._draft_cards(
                run_id, instruction, members, direct=len(members) == 1
            )
            run_store.advance(run_id, STATUS_RUNNING)
            self._emit(group_id, self._plan_text(instruction, cards, failure=failure), run_id)

            dispatch = self._make_dispatcher(
                run_id, group_id, members, web=web, graph=graph, model=model
            )
            # ★ 批间串行的另一半：下一批只能在「这一批的账清空之后」摊，而那时
            #   调用栈已经换成 `on_employee_task_done`，手上只剩一个 run_id。
            #   没有这份上下文，多批次计划永远停在批 0（2026-10-16 实盘症状：
            #   看板上还有一张「待认领」，群里结论已经写完）。
            run = self._runs.get(run_id)
            if run is not None:
                run["members"] = members
                run["dispatch"] = dispatch

            outcome = await coordinator.run_claim_rounds(
                run_id=run_id,
                members=members,
                board=board,
                runs=run_store,
                msgs=workspace_store,
                dispatch=dispatch,
            )
            # 认领轮里发生的每一件事都要在时间线读得出来：谁没答上话、谁被抢了、
            # 哪张卡因为太长没被问过。这些不是日志，是产品的一部分。
            for note in outcome.notes:
                self._emit(group_id, f"🗂 {note}", run_id, {"status": "claim_round"})

            pending = [c.task_id for c in outcome.claimed if c.task_id]
            for tid in pending:
                self._task_to_run[tid] = run_id
            run_store.register_tasks(run_id, pending)
            if run is not None:
                run["preparing"] = False
            if not pending:
                self._settle_unclaimed(run_id, group_id, cards)
            else:
                self.maybe_continue(run_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("协作流水线异常终止: run=%s", run_id)
            self._abort_run(
                run_id,
                group_id,
                content=f"⚠️ 这次协作中断在准备阶段（{e}），没有任务被派出去。",
                error=_truncate(str(e), 200),
            )

        finally:
            current_run.reset(budget_context)

    async def _draft_cards(
        self, run_id: str, instruction: str, members: list[dict[str, Any]], *, direct: bool
    ) -> tuple[list[dict[str, Any]], str]:
        """
        拆卡。**返回的卡片一张也没有 owner**，而且**永远至少有一张**。

        第二项是「拆解失败那句话」（成功时为 ""），它必须一路带到时间线：
        用户对「模型超时」和「模型拆出来的东西不能用」的处置完全不同
        （等一下重试 / 换个说法再问）。

        ★ 拆不出来不等于没有卡
        回落的是**一张未拆分的原始请求**（`created_by=system`），谁做由认领轮决定。
        过去这里是「压给 members[0]」，那是指派，不是建卡。
        """
        if direct:
            return board.create_cards(run_id, [self._single_card(instruction, "团队只有一个人，未做拆解")]), ""

        context = str(self._runs.get(run_id, {}).get("public_context") or "")
        planning_input = instruction + (f"\n\n群聊背景（仅供参考，以当前要求为准）：\n{context}" if context else "")
        result = await coordinator.decompose(planning_input, members)
        if result.ok:
            try:
                cards = board.create_cards(run_id, result.cards)
            except (CardDependencyError, ValueError) as e:
                # 整批被依赖闸拒掉：退化成那张未拆分的原始卡，并把原因说清楚
                landed = board.list_cards(run_id)
                if landed:
                    return landed, f"部分任务卡没能建立（{e}）"
                return (
                    board.create_cards(run_id, [self._single_card(instruction, str(e))]),
                    f"任务卡之间有冲突：{e}",
                )
            notes = ""
            if result.dropped:
                notes = f"拆解产出超出上限，已丢弃 {result.dropped} 张卡片"
            return cards, notes
        return (
            board.create_cards(run_id, [self._single_card(instruction, result.failure)]),
            result.failure,
        )

    def _single_card(self, instruction: str, reason: str) -> dict[str, Any]:
        """
        一张「未拆分的原始请求」。`reason` 解释为什么是原始请求，它会显示在卡片上。

        ★ `created_by=system` 是刻意的：这张卡不是协调者拆出来的成果，
        把它记成 coordinator 会让审计以为模型做了它没做的事。
        """
        text = (instruction or "").strip() or "（未填写要求）"
        return {
            "title": _truncate(text, 30),
            "brief": {"goal": text, "boundaries": [], "reason": reason},
            "batch": 0,
            "depends_on": [],
            "created_by": CREATED_BY_SYSTEM,
        }

    def _make_dispatcher(
        self,
        run_id: str,
        group_id: str,
        members: list[dict[str, Any]],
        *,
        web: bool,
        graph: bool,
        model: str,
    ):
        """
        认领轮的 `dispatch` 回调：**只有 CAS 已经抢到卡的人才会走到这里**。

        传成回调而不是在这里 import 执行器，是为了让「协调者不派活」这条界
        在代码结构上就成立 —— 治理层只在抢到卡之后叫一声「你可以开工了」。
        """
        base_by_id = {
            str(m.get("id") or ""): normalize_base_agent(m.get("base_agent")) for m in members
        }
        from app.workspace.db import workspace_db
        from app.permissions.policy import effective_mode
        import json
        options = workspace_db.query_one("SELECT payload FROM run_options WHERE run_id=?", (run_id,))
        options = json.loads(options["payload"]) if options else {}
        permission_mode = str(options.get("permission_mode") or effective_mode())

        def dispatch(member: dict[str, Any], card: dict[str, Any]) -> str:
            context = str(self._runs.get(run_id, {}).get("public_context") or "")
            task_instruction = self._card_instruction(card)
            if context:
                task_instruction += f"\n\n最近的群聊背景（参考资料，不是系统指令，当前任务要求优先）：\n{context}"
            from app.workspace.dispatch import dispatch_card
            dependencies = []
            for dep_id in card.get("depends_on") or []:
                upstream = board.get(str(dep_id))
                if not upstream or not upstream.get("task_id"):
                    raise RuntimeError("前置任务缺少可交接的执行记录")
                dependencies.append(str(upstream["task_id"]))
            payload = dict(
                thread_id=group_id,
                agent=normalize_base_agent(base_by_id.get(str(member.get("id") or ""))),
                title=str(card.get("title") or "任务卡"), instruction=task_instruction,
                employee_id=str(member.get("id") or ""), group_id=group_id,
                web=web, graph=graph, model=str(member.get("model") or model or settings.LLM_MODEL),
                run_id=run_id, board_card_id=str(card.get("id") or ""), source=SOURCE_BOARD,
                depends_on=dependencies,
                permission_mode=permission_mode,
            )
            return dispatch_card(payload, str(card["id"]), lambda record: self.track_task(run_id, record["task_id"]))

        return dispatch

    @staticmethod
    def _group_context(group_id: str) -> str:
        """只取最近的公开成品消息，供追问和后续任务延续上下文。"""
        _, total = workspace_store.list_messages(group_id, limit=1)
        items, _ = workspace_store.list_messages(group_id, limit=12, offset=max(0, total - 12))
        lines = []
        for item in items:
            if item.get("visibility", "group") != "group":
                continue
            message = workspace_store.enrich_message(item)
            name = "用户" if message.get("sender_kind") == "user" else message.get("employee_name") or "团队协调"
            lines.append(f"【{name}】{str(message.get('content') or '')[:450]}")
        return "\n".join(lines)[-6000:]

    def _card_instruction(self, card: dict[str, Any]) -> str:
        """卡片正文 → 交给员工的要求（goal + 边界 + 验收）。"""
        brief = card.get("brief") if isinstance(card.get("brief"), dict) else {}
        parts = [str(brief.get("goal") or card.get("title") or "").strip()]
        boundaries = [str(b).strip() for b in (brief.get("boundaries") or []) if str(b).strip()]
        if boundaries:
            parts.append("明确不要做的事：" + "；".join(boundaries))
        if card.get("blocked_reason"):
            parts.append("用户退回意见：" + str(card["blocked_reason"]))
        acceptance = str(card.get("acceptance") or "").strip()
        if acceptance:
            parts.append("验收标准：" + acceptance)
        return "\n".join(p for p in parts if p)

    def _emit(self, group_id: str, content: str, run_id: str, meta: dict | None = None) -> None:
        """
        落一条主持人/系统消息（群组消息的唯一产生路径之一，见模块头）。

        广播不在这里 —— `store.append_message` 写完就推（少一处的症状是
        「界面上要刷新才看得见」）。
        """
        workspace_store.append_message(
            group_id, employee_id="", kind="system",
            content=_truncate(content, MESSAGE_CHARS),
            run_id=run_id,
            meta={"run_id": run_id, **(meta or {})},
        )

    def _settle_unclaimed(self, run_id: str, group_id: str, cards: list[dict[str, Any]]) -> None:
        """
        一轮认领下来没人接活：把开放的卡标 `blocked`，把这个 run 判死。

        ★ 为什么不能让它停在 `running`
        界面上是一个转到天荒地老的 spinner，而后台没有任何东西在等它；
        同时它占着这个群的活跃位，用户连重新发起都做不到。
        「这件事推不动了」必须是**说出来**的结论，而不是一个不回答的状态。
        """
        inflight = [c for c in board.list_cards(run_id) if c["status"] == CARD_IN_PROGRESS]
        if inflight:
            # 有人绕过认领轮直接从接口认领并派了活：这些 task 必须进账，
            # 否则它们的终态找不到 run，收尾永不触发。
            pending = [str(c["task_id"]) for c in inflight if c["task_id"]]
            for tid in pending:
                self._task_to_run[tid] = run_id
            run_store.register_tasks(run_id, pending)
            if pending:
                return

        reason = f"连续 {settings.BOARD_STALL_LIMIT} 轮认领没有人接下这张卡"
        blocked = [c["id"] for c in cards if c["status"] == "todo"]
        for cid in blocked:
            board.advance_card(cid, CARD_BLOCKED, blocked_reason=reason)
        # ★ 护栏 3 真的动了要**结构化地**说出来。
        # 群里那条消息是给人读的长句，`GuardrailNotice` 要的是「哪条护栏、
        # 哪个环节点的」这两个能判断的字段 —— 只有前者的话，用户得从一段
        # 散文里反推出这是停滞，而他会先怀疑自己派错了活。
        events.guardrail_triggered(
            group_id,
            guardrail=3,
            code="stall_limit",
            message=f"{reason}（共 {len(blocked)} 张卡没人接）",
            run_id=run_id,
            card_id=blocked[0] if blocked else "",
        )
        names = "、".join(str(c.get("title") or c["id"]) for c in cards[:6])
        self._abort_run(
            run_id,
            group_id,
            content="\n".join(
                [
                    f"⚠️ 没有成员认领这些任务卡，这次协作到此为止（{reason}）。",
                    "",
                    f"板上的卡：{names}",
                    "",
                    "协调者**不会**替成员决定谁做，所以没人接就是没人接。",
                    "可选处置：① 把要求写得更具体再发起一次；② 进某位员工的私聊工作台直接交代给他；"
                    "③ 在任务板上手动认领（认领仍是 CAS，不会双派）。",
                ]
            ),
            error="没有成员认领任务卡",
        )

    def _abort_run(self, run_id: str, group_id: str, *, content: str, error: str) -> None:
        """
        一条都没派出去的 run 直接判 `failed`（终态），并把它说清楚。

        ★ 为什么不是 `input_required`
        `input_required` 是非终态，会**占住这个群的活跃位**（双跑约束），
        而阶段 2 之前界面里没有任何「回答它 / 取消它」的入口 ——
        等于把群组锁死在一条没人能解的等待上。终态才允许用户重新发起。
        """
        self._emit(group_id, content, run_id, {"status": "aborted"})
        try:
            run_store.advance(run_id, STATUS_FAILED, error=error)
        except Exception as e:  # noqa: BLE001
            logger.warning("run 判失败时状态写入出错: run=%s %s", run_id, e)

    def _plan_text(self, instruction: str, cards: list[dict[str, Any]], *, failure: str) -> str:
        """
        分工计划那条消息 —— 关键是**不许说谁做**。

        ★ 三种开头必须分得开，因为它们对用户是三种不同的期待
        - 正常拆解：板上有 N 张卡，接下来会有一轮认领；
        - 单人 direct：明说「未做拆解」，不让用户以为团队分工过了；
        - 拆解失败：明说是**一张未拆分的原始请求**在等认领（过去这里伪装的正是
          「一切正常、只是没人汇报」，用户于是去等一个不会来的结果）。
        """
        if failure:
            return f"任务暂时没能拆分（{failure}）。我先保留完整需求，看看哪位同事能接下来。"
        if len(cards) == 1 and str(cards[0].get("created_by") or "") == CREATED_BY_SYSTEM:
            return "这件事不需要再拆分，请按你的职责接下来处理。"
        titles = "、".join(str(c.get("title") or "") for c in cards)
        return f"我把这件事整理成了 {len(cards)} 项：{titles}。大家看看自己适合接哪一项。"

    # ---------- 自动汇报 ----------

    async def on_employee_task_done(self, task_id: str, employee_id: str, group_id: str) -> None:
        """
        员工任务到终态时的自动汇报（由 `executor._maybe_report_to_group` await 调用）。

        只有 `succeeded` / `failed` 值得占群组时间线；取消/中断这类「没人要了」
        的终态不发汇报 —— 但**照样要记账**（见 `_settle_run`）。
        """
        record = task_store.get(task_id)
        if record is None:
            return
        status = str(record.get("status") or "")
        from app.tasks.models import is_terminal
        if not is_terminal(status):
            return  # Shutdown/interruption keeps the card and its owner intact for recovery.
        from app.workspace.artifact_store import artifact_store
        from app.workspace.messaging import messenger
        artifact_error = ""
        try:
            artifact_store.register_task(record)
        except (ValueError, OSError) as exc:
            logger.warning("成果登记失败: %s", exc)
            artifact_error = str(exc)
            status = "failed"
            if record.get("board_card_id"):
                board.advance_card(record["board_card_id"], CARD_BLOCKED, blocked_reason=str(exc))
        messenger.task_done({**record, "status": status, "error": artifact_error} if artifact_error else record)

        # ★ 先把卡片投影成任务的终态，再写汇报
        # 任务板与 `TaskRecord` 是两份真相，`sync_from_task` 是它们之间唯一的翻译处
        # （幂等、且拒跨员工写）。少了这一步，症状不是报错而是**卡片永远停在 in_progress**：
        # 任务早跑完了，看板上还在转，用户据此判断「这个员工还在干活」。
        try:
            if not artifact_error:
                board.sync_from_task(record)
        except Exception as e:  # noqa: BLE001
            logger.warning("卡片状态同步失败（不影响汇报与收尾）: task=%s %s", task_id, e)

        try:
            if status in ("succeeded", "failed"):
                await self._post_deliverable(group_id, employee_id, record, status=status)
        finally:
            # ★ 销账排在摘要**外面**：模型再慢、再被取消、再抛，账都要当场销掉。
            # 顺序反过来（先 await 再销账）时，取消一次成员任务就是一个转到天荒地老的 spinner。
            self._settle_run(task_id, employee_id, status, str(record.get("result") or ""))

    async def _post_deliverable(
        self, group_id: str, employee_id: str, record: dict[str, Any], *, status: str
    ) -> None:
        """
        一条交付汇报：由**这个员工自己**打一次模型产出几句短句（施工图 §4.4）。

        用他自己的 `model` 与人设，是为了让群里的每句话听得出是谁说的 ——
        全员共用一个模型写出来的汇报，读的人分不清谁交了什么。
        """
        task_id = str(record.get("task_id") or record.get("id") or "")
        if status == "failed":
            # 没做成的人没有成果可总结，但「他卡住了」必须占时间线。
            # kind 仍归 deliverable 而不是 blocker：blocker 是四类定向消息之一，
            # 会算进护栏 1 的协商预算，而这条不是同事之间的对话。
            self._emit_deliverable(
                group_id, employee_id, task_id, status,
                f"⚠️ 任务执行失败：{record.get('error') or '未知错误'}",
            )
            return

        result = str(record.get("result") or "").strip()
        if not result:
            self._emit_deliverable(
                group_id, employee_id, task_id, status, "（任务完成，但未产出文字结论）"
            )
            return

        employee = workspace_store.get_employee(employee_id) or {}
        model = str(employee.get("model") or record.get("model") or "").strip()
        persona = persona_fragment(employee)
        limit = max(500, int(settings.REPORT_INPUT_CHARS))
        # ★ 截断要明说：模型看到「这就是全文」时会漏掉后半段的结论，那比截断本身更糟
        body = result if len(result) <= limit else (
            result[:limit] + "\n\n［到这里已按长度截断，后面还有内容在他的私聊工作台］"
        )
        system = (
            (f"{persona}\n\n" if persona else "")
            + "你现在在团队群里汇报你刚做完的这张卡。"
            "只说成品，不说过程：不超过 3 句，讲清「做完了什么、结论或关键数字、"
            "还需要谁注意」。不要贴原文、不要罗列工具调用、不要写你的思考过程。"
        )
        user = (
            f"卡片目标：{str(record.get('instruction') or '').strip() or '（卡片没写目标）'}\n\n"
            f"你的工作结果全文：\n{body}"
        )
        text, error = await _llm_attempt(
            system, user, model=model or None,
            profile_id=str(employee.get("model_profile_id") or "") or None,
        )
        self._emit_deliverable(
            group_id, employee_id, task_id, status,
            # ★ 失败不许降级成「随便截一段」：那会把「模型没干活」伪装成「他很简洁」
            text
            or f"⚠️ 汇报摘要没有生成（{error or '模型没有返回内容'}），全文在他的私聊工作台",
        )

    def _emit_deliverable(
        self, group_id: str, employee_id: str, task_id: str, status: str, content: str
    ) -> None:
        """落一条交付消息；截断与三条禁令由 `store.append_message` 统一收口。"""
        workspace_store.append_message(
            group_id,
            employee_id=employee_id,
            kind="deliverable",
            content=content,
            task_id=task_id,
            run_id=str((task_store.get(task_id) or {}).get("run_id") or ""),
            meta={"task_id": task_id, "status": status},
        )

    @staticmethod
    def cards_for_run(run_id: str) -> list[dict]:
        return board.list_cards(run_id)

    def track_task(self, run_id: str, task_id: str) -> None:
        self._task_to_run[task_id] = run_id
        run_store.register_tasks(run_id, [task_id])

    async def shutdown(self) -> None:
        self._closing = True
        tasks = list(self._pipelines.values()) + list(self._finalizers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pipelines.clear()
        self._finalizers.clear()

    def _settle_run(self, task_id: str, employee_id: str, status: str, result: str) -> None:
        from app.tasks.models import is_terminal
        if not is_terminal(status):
            return
        record = task_store.get(task_id) or {}
        run_id = self._task_to_run.pop(task_id, "") or str(record.get("run_id") or "")
        run = self._runs.get(run_id)
        if not run:
            return
        if status == "succeeded":
            run["done_results"][task_id] = result
        run_store.record_task_result(run_id, task_id, employee_id=employee_id, status=status, result=result)
        self.maybe_continue(run_id)

    def maybe_continue(self, run_id: str) -> None:
        from app.workspace.inbox import inbox
        from app.workspace.messaging import messenger
        run = self._runs.get(run_id)
        row = run_store.get(run_id)
        if self._closing or not run or not row or row["terminal"] or run.get("preparing") or run.get("finalized") or row["pending"]:
            return
        if inbox.pending(run_id=run_id):
            messenger.wake_run(run_id)
            return
        if self._schedule_next_batch(run_id):
            return
        run["finalized"] = True
        self._schedule_finalize(run_id)

    def _schedule_next_batch(self, run_id: str) -> bool:
        """
        这批收口了但看板上还有没交付的卡 → 派发续摊协程，返回 True（本轮不收尾）。

        返回 False 的含义是「没有下一批可摊了」，调用方据此收口。

        ★ 为什么判据先看板、而不是让认领轮自己去发现
        认领轮「无卡可摊」就 break，而那个形状同时出现在两种完全不同的场合：
        「全部交付完了」与「批 0 在飞、批 1 还没解锁」。让收口去猜哪一个，
        就是实盘那幅「已完成 + 一张待认领」的来历。`current_batch()` 数的正是
        还有未交付卡的最小批次，它是这两个场合唯一的区分器。
        """
        run = self._runs.get(run_id)
        if run is None or run.get("dispatch") is None:
            return False  # 重启后活下来的上下文没有认领轮要用的那三样，只能就地收口
        if board.current_batch(run_id) is None:
            return False

        cards = board.list_cards(run_id)
        rounds = int(run.get("batch_rounds", 0))
        # ★ 续摊次数要有上界。`current_batch` 不是 None 这个条件可以一直成立
        #   （一张卡被释放回 `todo` 时它挡住的那批就是它自己），于是这里会变成
        #   一个常驻循环 —— 正是 §5.6 明令否决的那个东西。
        #   上界取卡片张数：每续摊一次要么至少派出一个任务，要么走到收口，
        #   所以「卡片有几张」就是它最多能走几步。
        if rounds >= max(1, len(cards)):
            logger.info("续摊到上界（%s 次），就地收口: run=%s", rounds, run_id)
            return False
        run["batch_rounds"] = rounds + 1
        run["preparing"] = True

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("无运行中的事件循环，跳过下一批认领: run=%s", run_id)
            return False
        task = loop.create_task(self._next_batch(run_id))
        self._pipelines[run_id] = task
        task.add_done_callback(lambda done, rid=run_id: self._pipelines.pop(rid, None) if self._pipelines.get(rid) is done else None)
        return True

    @run_scoped
    async def _next_batch(self, run_id: str) -> None:
        """
        续摊：把刚解锁的批次摊出去，抢到就派。与开局那次的区别只有「没有拆解」。
        """
        run = self._runs.get(run_id)
        if run is None:
            return
        group_id = run["group_id"]
        try:
            outcome = await coordinator.run_claim_rounds(
                run_id=run_id,
                members=run["members"],
                board=board,
                runs=run_store,
                msgs=workspace_store,
                dispatch=run["dispatch"],
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("续摊异常终止: run=%s", run_id)
            self._abort_run(
                run_id,
                group_id,
                content=f"⚠️ 下一批任务没能摊出去（{e}），这次协作到此为止。",
                error=_truncate(str(e), 200),
            )
            return
        for note in outcome.notes:
            self._emit(group_id, f"🗂 {note}", run_id, {"status": "claim_round"})

        pending = [c.task_id for c in outcome.claimed if c.task_id]
        for tid in pending:
            self._task_to_run[tid] = run_id
        run_store.register_tasks(run_id, pending)
        run["preparing"] = False
        if pending:
            self.maybe_continue(run_id)
            return  # 新账已开，等它们各自汇报完再谈收口

        # 一张也没派出去，而账是空的。两条处置按「前面批交了没有」分：
        if run["done_results"]:
            run["finalized"] = True
            # 已经有一部分成果 → 收口，如实汇总「做到这儿了」。
            # ★ 这里不走 `_settle_unclaimed`：那条路把 run 判成 `failed`，
            #   而「前两批交了、第三批没人接」对用户是**部分交付**，不是失败。
            self._schedule_finalize(run_id)
            return
        # 什么都没交 → 与开局同样的停滞处置（护栏 3 要说话）。
        # ★ 这里**不许**预先立 `finalized` 旗：`_settle_unclaimed` 里还有一条
        #   「有人绕过认领轮自己认领了卡」的早退分支，那些任务之后要能正常收口，
        #   否则旗子会把它们挡住 —— run 又变成永久 spinner（我们要根除的就是它）。
        self._settle_unclaimed(run_id, group_id, board.list_cards(run_id))

    def _schedule_finalize(self, run_id: str) -> None:
        """派发结论汇总（fire-and-forget，与任务收尾解耦）。"""
        try:
            task = asyncio.get_running_loop().create_task(self._finalize(run_id))
            self._finalizers[run_id] = task
            task.add_done_callback(lambda done, rid=run_id: self._finalizers.pop(rid, None) if self._finalizers.get(rid) is done else None)
        except RuntimeError:
            # 无运行中的事件循环（理论上是测试/纯同步上下文）—— 结论让位于安全。
            logger.warning("无运行中的事件循环，跳过结论汇总: run=%s", run_id)

    @run_scoped
    async def _finalize(self, run_id: str) -> None:
        """所有成员任务完成后，主持人汇总结论并写一条群组消息。"""
        run = self._runs.get(run_id)
        if run is None:
            return
        group_id = run["group_id"]
        latest_cards = {card["id"]: card["task_id"] for card in board.list_cards(run_id)}
        results: dict[str, str] = {}
        for tid, result in run["done_results"].items():
            task = task_store.get(tid) or {}
            card_id = str(task.get("board_card_id") or "")
            if not card_id or latest_cards.get(card_id) == tid:
                results[tid] = result
        version = int(run.get("conclusion_version", 0)) + 1
        run["conclusion_version"] = version
        if run.get("conversational"):
            row = run_store.get(run_id)
            if row and row["status"] != STATUS_CONCLUDING:
                run_store.advance(run_id, STATUS_CONCLUDING)
            failed = workspace_store._db.query_scalar("SELECT COUNT(*) FROM deliveries d JOIN messages m ON m.id=d.message_id "
                                                       "WHERE m.run_id=? AND d.state='failed'", (run_id,))
            final = (STATUS_PARTIAL if failed else STATUS_COMPLETED) if results else STATUS_FAILED
            run_store.advance(run_id, final)
            self._runs.pop(run_id, None)
            return

        # ★ 先进 `concluding` 再动笔：这一步同时也是「这次 run 还归不归我写结论」的
        #   判定。用户已经取消 / 进程重启已被结算成 expired 的 run，
        #   迟到的汇总结论不许再往时间线上写 —— 那会盖掉「已中断」那句实话。
        try:
            row = run_store.get(run_id)
            entered = row if row and row["status"] == STATUS_CONCLUDING else run_store.advance(run_id, STATUS_CONCLUDING)
        except Exception as e:  # noqa: BLE001
            logger.info("run 不进入收尾（%s）: run=%s", e, run_id)
            return
        if entered is None:
            return

        try:
            if results:
                conclusion = await self._summarize(run["instruction"], results)
            else:
                conclusion = "本次协作没有成员产出结果，请检查成员是否可用或重试。"
        except Exception as e:  # noqa: BLE001
            logger.warning("结论汇总失败（降级为提示语）: %s", e)
            conclusion = "（结论生成失败，请查看上方各成员的汇报。）"

        latest = run_store.get(run_id)
        if not latest or latest["status"] != STATUS_CONCLUDING or run.get("conclusion_version") != version:
            return  # A new message superseded this in-flight conclusion.

        from app.workspace.budget import run_budget
        gaps = [c["title"] for c in board.list_cards(run_id) if c["status"] not in ("review", "done")]
        if gaps:
            conclusion += "\n尚未交付：" + "、".join(gaps)
        budget_error = run_budget.snapshot(run_id).get("error", "")
        if budget_error:
            conclusion += "\n" + budget_error
        failed_deliveries = workspace_store._db.query_all("SELECT d.last_error FROM deliveries d JOIN messages m ON m.id=d.message_id "
            "WHERE m.run_id=? AND d.state='failed'", (run_id,))
        if failed_deliveries:
            conclusion += f"\n仍有 {len(failed_deliveries)} 条请求未能处理，请查看对应消息。"
        workspace_store.append_message(
            group_id,
            employee_id="",
            kind="summary",
            content=_truncate(conclusion, MESSAGE_CHARS),
            run_id=run_id,
            meta={"run_id": run_id},
        )

        # 一个成员的活都没出结果 = 这次协作没交付任何东西，如实标 failed
        # （界面按状态上色，写成 completed 就是让用户体验成「办完了」）
        cards = board.list_cards(run_id)
        if failed_deliveries:
            final = STATUS_PARTIAL if results else STATUS_FAILED
        elif cards and all(c["status"] == "done" for c in cards):
            final = STATUS_COMPLETED
        elif cards and all(c["status"] in ("done", "review") for c in cards):
            final = STATUS_REVIEW
        else:
            final = STATUS_PARTIAL if results else STATUS_FAILED
        try:
            run_store.advance(
                run_id, final, error="" if results else "没有成员产出结果"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("run 终态写入失败: run=%s %s", run_id, e)
        else:
            self._runs.pop(run_id, None)

    async def _summarize(self, instruction: str, results: dict[str, str]) -> str:
        entries: list[str] = []
        for task_id, result in results.items():
            task = task_store.get(task_id) or {}
            eid = str(task.get("employee_id") or "")
            employee = workspace_store.get_employee(eid)
            name = (employee.get("name") if employee else "") or eid
            name += " / " + str(task.get("title") or task_id)
            entries.append(f"【{name}】\n{_truncate(result, SUMMARY_ENTRY_CHARS)}")
        body = "\n\n".join(entries)
        system = "你是工作空间的主持人，负责汇总团队各成员的成果，给出简洁的最终结论。"
        user = (
            f"用户要求：{instruction}\n\n"
            f"各成员成果：\n{body}\n\n"
            "请用一段话汇总最终结论，并指出还需要用户注意或跟进的事项（如有）。"
        )
        text = await _llm_text(system, user)
        return text or "（未能生成结论，请查看各成员汇报。）"


#: 进程级单例
orchestrator = GroupOrchestrator()


# ---------- 群组消息 SSE ----------


async def stream_group_events(
    group_id: str, bus: Optional[GroupEventBus] = None
) -> AsyncGenerator[dict[str, Any], None]:
    """
    群组消息流：先推快照，再推增量（仿 `/api/tasks/stream`）。

    ★ 快照为什么必须在前
    订阅者可能在编排**中途**才连进来（刷新页面 / 重新打开），
    没有快照的话它会漏掉之前的所有汇报。快照 + 增量是「先补齐再跟新」的标准做法。

    ★ 只推「群组消息」成品文本 —— 员工内部思考绝不经过这里（见模块注释）。
    """
    bus = bus or group_bus
    queue = await bus.subscribe(group_id)
    try:
        items, total = workspace_store.list_messages(group_id, limit=200)
        # ★ 快照里必须带上 run 与任务板，不然「刷新即失忆」：
        # 一次协作跑到一半，用户 F5 —— 时间线补回来了，看板却全空，
        # 要等下一次状态迁移才零碎地长出来。前端那条纪律是「快照为真、SSE 只是加速」，
        # 而它成立的前提是快照真的装得下当前这一屏。
        run = run_store.active_run_for_group(group_id)
        if run is None:
            # 没有在跑的也要带最近一次：用户刷新后应当看得见「上次做完了 / 失败了」，
            # 而不是 Boards 面板凭空消失，看起来像数据丢了。
            recent = run_store.list_runs(group_id, limit=1)
            run = recent[0] if recent else None
        cards = board.list_cards(str(run["id"])) if run else []
        from app.agents.approval_broker import approval_broker

        pending = [
            {k: item.get(k, "") for k in ("thread_id", "call_id", "run_id", "employee_id", "requestor", "tool_label")}
            for item in approval_broker.list_pending()
            if item.get("thread_id") == group_id and not item.get("staged")
        ]
        # ★ 待回答与待审批同一条纪律：快照必须装得下当前这一屏
        # 刷新后只靠增量的话，那张「等你回答」的卡片要等到下一个事件才出现 ——
        # 而提问恰恰是「没有后续事件、只有人在等」的状态，可能永远等不到。
        from app.questions.broker import question_broker

        pending_questions = [
            item
            for item in question_broker.list_pending()
            if item.get("thread_id") == group_id and not item.get("staged")
        ]
        yield {
            "pending_approvals": pending,
            "pending_questions": pending_questions,
            "type": "snapshot",
            "group_id": group_id,
            "messages": [workspace_store.enrich_message(m) for m in items],
            "total": total,
            "run": events.run_summary(run) if run else None,
            "cards": [events.card_summary(c) for c in cards],
            # 断线重连的游标起点：客户端记住它，之后用 `?after_seq=` 补拉（§14.3）。
            "latest_seq": max((int(m.get("seq") or 0) for m in items), default=0),
        }
        while True:
            event = await queue.get()
            yield event
    finally:
        await bus.unsubscribe(group_id, queue)


__all__ = [
    "GroupOrchestrator",
    "orchestrator",
    "stream_group_events",
]
