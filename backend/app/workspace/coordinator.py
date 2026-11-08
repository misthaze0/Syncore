"""
协调者 —— 治理层：把一件事拆成任务卡、摊卡、跑认领轮。

★ 只有两处打模型（施工图 §5.6）

| 处 | 函数 | 用途 |
|---|---|---|
| 1 | `decompose()` | 拆解成卡片（**不输出 owner**） |
| 2 | `claim_turn()` | 问一名成员「这几张卡你要不要、同事这几句话你要不要回」 |

项目摘要那处不在这里 —— 它复用 `orchestrator` 已有的汇总结论，别写第三份。
其余全部是确定性代码：批次判定、依赖检查、负载过滤、摊卡上限、
定向消息的收件箱与三条护栏（预算 / 环 / 交接深度）。

★ 「协调者没有自己的循环」是边界

这里每个函数都由 run 生命周期上的**一个事件**驱动（用户发起、某张卡到终态）。
不要加「每 30 秒让模型看一眼板」—— 那会把认领轮的开销变成没有上限的常驻开销，
并把单进程假设压成不可实现（§17）。

★ 它从不写 `owner_employee_id`

协调者治理（判依赖、开批次、检测停滞、写摘要），**派活由员工自己在认领轮里说**。
`decompose()` 的输出里没有成员 id 这一项，不是「暂时没实现」，是它拿不到这个字段
（见 `test_coordinator.py::test_decompose_output_has_no_place_to_put_an_owner`）。

★ 认领轮的输入为什么这么窄

一次短上下文、非流式调用：卡片摘要（≤200 字/张、≤4 张）+ 我的角色卡 + 我在飞任务数
+ **指名我的、还没读过的话**（≤`INBOX_MAX` 条成品句，不是我和谁的对话记录）。
**永不含任何 transcript 全文** —— 那不是省钱，是护栏：把同事的私聊全文喂进来
就等于绕过了「禁止 A 直接读 B 的私聊全文」这条产品级禁令。

★ 「同事互相交流直到做完」靠的是三道上界，不是靠放行

§5.7 把认领轮接上了定向消息，于是模型调用多了一个维度：每人每轮最多读到 `INBOX_MAX`
条、整场最多 `NEGOTIATION_MAX_MESSAGES_PER_RUN` 条、一张卡最多转手 `HANDOFF_MAX_DEPTH` 次。
护栏 1（预算）/ 4（环）/ 5（交接深度）与它同批落地，**不许拆开**：少了任何一道，
「更真实的群聊」就等于一个会自己转到超时的聊天室（施工图 §7 的 ★ 段）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.workspace import events
from app.workspace.board import CARD_TODO, Board, new_card_id
from app.workspace.llm_io import llm_text as _llm_text, parse_json as _parse_json
from app.workspace.models import normalize_base_agent
from app.workspace.run_store import RunStore

logger = logging.getLogger(__name__)

#: 拆解产出的卡片数上限。它不是审美：卡片数直接乘认领轮的 token
#: （轮数 × 成员数 × 卡片摘要），而 ADR 0020 已经为并行拆解接受了约 15× 的开销。
#: 超了会**丢弃多余的后半**，但丢弃必须说出来（见 `Decomposition.dropped`）。
CARDS_MAX = 8

#: 模型给卡片起的本地代号（用来表达批内依赖），不是入库 id。
LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")

#: 认领轮的五种回答。多一个就有一个是界面渲染不出来的。
ACTION_CLAIM = "claim"
ACTION_ASK = "ask"
ACTION_ANSWER = "answer"
ACTION_HANDOFF = "handoff"
ACTION_DECLINE = "decline"
CLAIM_ACTIONS = (ACTION_CLAIM, ACTION_ASK, ACTION_ANSWER, ACTION_HANDOFF, ACTION_DECLINE)

#: `answer` 能用的 kind：回答一条定向消息时，内容性质只有这三种。
#: （`request` 是「问」，不能用来「答」；`goal/decision/summary/system` 是广播。）
ANSWER_KINDS = ("commitment", "deliverable", "blocker")

#: 卡片摘要进输入的字符上限之外的字段截断
REASON_CHARS = 200
#: `ask` 里「要什么」那句的长度。它是要进同事收件箱的一句话，不是一个字段。
NEED_CHARS = 200
#: `answer` 正文上限。★ 它必须小于 `GROUP_SUMMARY_CHARS`（800）——
#: 否则一条回答会在写入时被 `sanitize_group_text` 截断，而截断处正在句于半句。
ANSWER_CHARS = 600


@dataclass
class Decomposition:
    """拆解结果。`cards` 为空时 `failure` 必须是给人看的那句话，不许是空串。"""

    cards: list[dict[str, Any]] = field(default_factory=list)
    failure: str = ""
    dropped: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.cards)


@dataclass
class Offer:
    """摊给一名成员的一张卡。"""

    card_id: str
    title: str
    summary: str

    def as_line(self) -> str:
        return f"- id={self.card_id} 标题={self.title} 要做的事={self.summary}"


@dataclass
class Decision:
    """
    一名成员在认领轮里的回答。

    后四个字段是 §5.7 的定向消息用的，按动作各用其中几个：
    `ask` 用 `ask_to` + `need`，`answer` 用 `reply_to` + `kind`，`handoff` 用 `ask_to` + `card_id`。
    """

    action: str
    card_id: str = ""
    reason: str = ""
    ask_to: str = ""
    need: str = ""
    content: str = ""
    reply_to: str = ""
    kind: str = ""


def inbox_line(item: dict[str, Any], name_of: dict[str, str]) -> str:
    """
    把一条指名我的消息压成认领轮输入里的一行。

    ★ 只给「谁说的 / 哪一类话 / 那成品句」，不给 thread、不给 meta
    输入面窄是这里的护栏而不是省 token 的技巧：把同事的私聊全文喂进认领轮
    就等于从治理层绕过了「禁止 A 直接读 B 的私聊」那条产品级禁令。
    """
    who = name_of.get(str(item.get("employee_id") or ""), "") or "同事"
    text = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
    return f"- 来自 {who}（{item.get('kind')}，id={item.get('id')}）：{text}"



def _brief_summary(brief: Any, title: str) -> str:
    """卡片摘要 = goal（没有 goal 就用 title），单行，便于进短上下文。"""
    if isinstance(brief, dict):
        goal = str(brief.get("goal") or "").strip()
    else:
        goal = str(brief or "").strip()
    text = goal or title
    return re.sub(r"\s+", " ", text).strip()


def card_key(card: dict[str, Any]) -> str:
    """
    一张卡的 id，两种形状都认。

    ★ 为什么要兼容两个键名：`decompose()` 返回的是**还没入库**的卡（键 `card_id`，
    因为 id 要在建卡前就存在，才能表达「c2 依赖 c1」），
    而 `board.list_cards()` 给的是入库后的行（键 `id`，与 `run_tasks` 列名一致）。
    认领轮拿到的可能是其中之一，界面两者都要拿得到 —— 在这里认两种，
    比在调用方各写一份 `card.get("id") or card.get("card_id")` 少一处漂移。
    """
    return str(card.get("id") or card.get("card_id") or "")


def offer_cards(cards: list[dict[str, Any]]) -> tuple[list[Offer], list[dict[str, Any]]]:
    """
    把卡片摊成认领轮的输入：最多 `OFFER_BATCH_MAX` 张，且摘要必须装得下。

    ★ 摘要超上限的卡是**整张不参与本轮**，不是砍尾（施工图 §5.5）。
    砍尾会让模型读到半句话的验收标准，然后据此认领 ——
    那比「这张卡这轮没摊出去」糟糕得多，因为它看起来像一次正常的拒绝。
    被跳过的卡原样返回给调用方，界面要能数出「有几张卡因为太长没被问」。
    """
    limit = settings.CLAIM_CARD_BRIEF_CHARS
    cap = settings.OFFER_BATCH_MAX
    offers: list[Offer] = []
    skipped: list[dict[str, Any]] = []
    for card in cards:
        summary = _brief_summary(card.get("brief"), str(card.get("title") or ""))
        if len(summary) > limit:
            skipped.append(card)
            continue
        offers.append(
            Offer(
                card_id=card_key(card),
                title=str(card.get("title") or ""),
                summary=summary,
            )
        )
        if len(offers) >= cap:
            break
    return offers, skipped


def eligible_members(
    members: list[dict[str, Any]],
    *,
    inflight_of,
    max_inflight: Optional[int] = None,
) -> list[dict[str, Any]]:
    """
    这一轮可以邀请谁：`can_request` 为真、且当前在飞负载为 0。

    Args:
        inflight_of: `employee_id -> 在飞张数` 的取值函数（由调用方接 `board.inflight_count`）。
            传函数而不是直接 import `board`：认领轮问的是「这一刻」的负载，
            中间隔着 await，读一次旧值就会让两个人同时接两张卡。

    负载 0 才邀请 —— 与 `claim()` 里的 CAS 是两道独立的闸：
    邀请条件拦掉绝大多数，原子写拦住剩下那几个真并发到的。
    """
    cap = settings.BOARD_MAX_INFLIGHT_PER_EMPLOYEE if max_inflight is None else max_inflight
    out: list[dict[str, Any]] = []
    for member in members:
        role_card = member.get("role_card")
        role_card = role_card if isinstance(role_card, dict) else {}
        if not bool(role_card.get("can_request", True)):
            continue
        if inflight_of(str(member.get("id") or "")) >= cap:
            continue
        out.append(member)
    return out


def _roster_line(member: dict[str, Any]) -> str:
    return (
        f"- id={member.get('id')} 名字={member.get('name') or '未命名'} "
        f"定位={member.get('tagline') or '—'} 能力={normalize_base_agent(member.get('base_agent'))} "
        f"范围={member.get('scope') or '—'}"
    )


async def decompose(instruction: str, members: list[dict[str, Any]]) -> Decomposition:
    """
    拆解成卡片（打模型 #1）。返回的每张卡**没有 owner** —— 建卡 ≠ 指派。

    卡片形状与 `board.create_cards()` 一致：
    `{title, brief:{goal, boundaries}, acceptance, batch, depends_on:[card_id]}`。

    ★ 依赖指向不存在的代号时，整份拆解一起作废，而不是丢掉那条依赖。
    丢掉 = 这张卡跑在它的前提之前，产出看着像真的但基于的是旧事实；
    作废 = 回落到「未拆分的原始请求」那一张卡，用户读得懂发生了什么。
    """
    roster = "\n".join(_roster_line(m) for m in members)
    system = (
        "你是工作空间的协调者，负责把用户交代的一件事拆成若干张**任务卡**。"
        "任务卡还没有负责人：你只描述「要做什么」，绝不指定「谁做」——"
        "谁做由成员自己在认领轮里决定。只输出 JSON，不要任何解释。"
    )
    user = (
        f"团队有这些成员（仅供你判断这件事值不值得拆，不要指派给任何人）：\n{roster}\n\n"
        f"用户要求：{instruction}\n\n"
        "请拆成 1~8 张可独立完成的卡片。字段：\n"
        '  id            本轮内唯一的短代号，如 "c1"\n'
        "  title         不超过 30 字的短标题\n"
        "  goal          这张卡要做的事（一段话）\n"
        "  boundaries    明确不要做的事（字符串数组，可留空）\n"
        "  acceptance    验收标准（可留空）\n"
        "  batch         批次号，从 0 起；同一批并行，后一批只能依赖前一批已完成的卡\n"
        "  depends_on    依赖的卡片 id 数组；同一批内不得互相依赖\n"
        '输出格式：{"cards":[{"id":"c1","title":"...","goal":"...",'
        '"boundaries":[],"acceptance":"","batch":0,"depends_on":[]}]}'
    )
    text = await _llm_text(system, user)
    data = _parse_json(text)
    raw_cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(raw_cards, list) or not raw_cards:
        # ★ 三种「没拆出来」对用户是三种不同的处置，所以 failure 必须区分它们：
        # 模型超时（等一下重试可能好）/ 模型答非所问（换说法再问）/
        # 模型给了空数组（这件事大概率根本不需要拆，单张卡是对的）。
        # 这里若留空串，界面就只能显示「拆解失败」四个字，用户什么也判断不了。
        if not text:
            failure = "模型超时或不可用"
        elif not isinstance(raw_cards, list):
            failure = "模型返回的内容里没有 cards 字段"
        else:
            failure = "模型没有拆出任何卡片"
        return Decomposition(failure=failure)

    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_cards):
        if not isinstance(item, dict):
            continue
        local = str(item.get("id") or f"c{index + 1}").strip()
        goal = str(item.get("goal") or "").strip()
        title = str(item.get("title") or "").strip() or _truncate_one(goal, 30)
        if not LOCAL_ID_RE.match(local) or local in seen or not goal:
            # 没有 goal 的卡片在认领轮里摊不出去（摘要就是它），等于一张空卡
            continue
        seen.add(local)
        try:
            batch = max(0, int(item.get("batch") or 0))
        except (TypeError, ValueError):
            batch = 0
        cards.append(
            {
                "card_id": new_card_id(),
                "_local": local,
                "title": title,
                "brief": {
                    "goal": goal,
                    "boundaries": [str(b) for b in (item.get("boundaries") or []) if str(b).strip()],
                },
                "acceptance": str(item.get("acceptance") or "").strip(),
                "batch": batch,
                "_depends_local": [str(d).strip() for d in (item.get("depends_on") or [])],
            }
        )

    if not cards:
        return Decomposition(failure="模型给出的卡片都不可用（缺 goal 或代号重复）")

    dropped = max(0, len(raw_cards) - len(cards))

    # 先削到上限、再解析依赖。顺序反过来的话，被削掉的卡会留下一条指向空气的依赖。
    # 真留下这种依赖时**整份作废**而不是丢掉那条边：丢掉 = 这张卡跑在它的前提之前，
    # 产出看着像真的、基于的却是旧事实；退回一张未拆分的原始卡，用户在时间线上读得懂。
    if len(cards) > CARDS_MAX:
        # 按批次升序取，同批保持模型给的顺序（sort 稳定，不按标题字典序随便排）
        kept = sorted(cards, key=lambda c: c["batch"])[:CARDS_MAX]
        dropped += len(cards) - len(kept)
        cards = kept

    known = {c["_local"]: c["card_id"] for c in cards}
    for card in cards:
        deps: list[str] = []
        for local in card.pop("_depends_local", []):
            if not local:
                continue
            if local not in known or local == card["_local"]:
                return Decomposition(
                    failure="模型给出的依赖指向不存在的卡片（代号没被给出，或卡片数超上限后被削掉）",
                    dropped=dropped,
                )
            if known[local] not in deps:
                deps.append(known[local])
        card["depends_on"] = deps
    for card in cards:
        card.pop("_local", None)

    return Decomposition(cards=cards, dropped=dropped)


def _truncate_one(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)] + "…"


async def claim_turn(
    member: dict[str, Any],
    offers: list[Offer],
    *,
    inflight: int,
    inbox: list[dict[str, Any]],
    roster: list[dict[str, Any]],
) -> tuple[Optional[Decision], str]:
    """
    问一名成员：这几张卡里有没有你要认领的、同事这几句话有没有要回的（打模型 #2）。

    返回 `(decision, failure)`：
    - `(Decision(claim), "")` —— 他要了这张卡，**能不能拿到还得看 `board.claim()` 的 CAS**；
    - `(None, "模型超时或不可用")` —— 没问到话。调用方必须把这一句与
      「成员看完了但不要」区分开显示：对用户是两种完全不同的处置
      （等一下再问 / 换个人做 / 他自己不想做）。

    ★ 输入里没有 transcript，也不该有（见模块 docstring）
    `inbox` 是**指名给他**的成品句（≤`INBOX_MAX` 条，每条已经在写入时按 §4.2 截过），
    不是「他看过的那些对话」。这条区别是护栏：认领轮一问就是一次模型调用，
    输入面每宽一次，「同事之间不许互读全文」那条禁令就薄一次。

    ★ 收件箱为空时不许提问 `answer`
    模型没有可回的东西时，它会编一个 `reply_to`。那种编造在库里是一条指向空气的边，
    护栏 4 的图上会多出一个假节点 —— 所以校验放在调用方（它才知道本轮真正摊出去了什么）。
    """
    if not offers and not inbox:
        return None, "本轮既没有摊出去的卡片，也没有指名我的话"
    limit = settings.CLAIM_CARD_BRIEF_CHARS
    system = (
        "你是一名数字员工，正在看团队任务板上摊给你的几张任务卡，"
        "以及同事指名你的几句话。只输出 JSON，不要任何解释。"
    )
    identity = (
        f"我是：{member.get('name') or '未命名'}（定位：{member.get('tagline') or '—'}；"
        f"能力：{normalize_base_agent(member.get('base_agent'))}；范围：{member.get('scope') or '—'}）"
    )
    inbox_block = (
        "\n".join(
            inbox_line(item, {
                str(m.get("id") or ""): str(m.get("name") or "") for m in roster
            })
            for item in inbox
        )
        if inbox
        else "（没有指名我的话）"
    )
    cards_block = (
        "\n".join(offer.as_line() for offer in offers)
        if offers
        else "（这一批的卡已经都被人接走了，本轮只需要回同事的话）"
    )
    user = (
        f"{identity}\n"
        f"我手上正在做的事：{inflight} 件（上限 {settings.BOARD_MAX_INFLIGHT_PER_EMPLOYEE} 件）\n\n"
        f"摊给我的任务卡（每张摘要不超过 {limit} 字，超出上限的卡本轮不会出现在这里）：\n"
        + cards_block
        + f"\n\n指名我、我还没读过的话：\n{inbox_block}\n\n"
        "团队名册（要问同事或建议转手时，收件人只能填这里的 id）：\n"
        + "\n".join(_roster_line(m) for m in roster)
        + "\n\n"
        "请判断该做哪一个动作：\n"
        '  claim     这张卡我来做（填 card_id）\n'
        "  ask       我需要某位同事给我一件**可指名**的东西才能判断或开工"
        "（填 ask_to=同事 id、need=要什么、because=为什么）\n"
        "  answer    回答上面某一条指名我的话（填 reply_to=那条话的 id、"
        f"kind={'|'.join(ANSWER_KINDS)} 之一、content=你的答复）\n"
        "  handoff   这张卡不该我做、该交给某位同事（填 card_id + ask_to；"
        "这只是建议，卡仍然开放、由他自己认领）\n"
        "  decline   都不接\n"
        '输出 {"action":"claim|ask|answer|handoff|decline",'
        '"card_id":"","ask_to":"","reply_to":"","kind":"",'
        '"need":"","content":"","reason":"一句话理由（会显示给团队看）"}。'
        "规则：手上已经有在做的事就 decline；只能选上面列出的 id，不许自己编；"
        "「再讲讲」这种说不出名的东西不算 ask。"
    )
    from app.workspace.employee_runtime import persona_fragment

    system = persona_fragment(member) + "\n\n" + system
    text = await _llm_text(
        system, user, model=str(member.get("model") or "") or None,
        profile_id=str(member.get("model_profile_id") or "") or None,
    )
    if not text:
        return None, "模型超时或不可用"
    data = _parse_json(text)
    if not isinstance(data, dict):
        return None, "成员的回答无法解析"
    action = str(data.get("action") or "").strip().lower()
    if action not in CLAIM_ACTIONS:
        return None, "成员的回答不在可识别的动作里"
    reason = _truncate_one(str(data.get("reason") or ""), REASON_CHARS)
    card_id = str(data.get("card_id") or "").strip()

    if action == ACTION_CLAIM:
        if card_id not in {offer.card_id for offer in offers}:
            # 模型编了一个没摊出去的 id：当成 decline 并记下真实原因，
            # 而不是「认领失败」—— 后者会让界面显示一张根本没被问过的卡。
            logger.info("认领轮给出的卡片 id 不在本轮摊出的卡里: %s", card_id)
            return (
                Decision(ACTION_DECLINE, reason=f"提到的卡片不在本轮摊出的卡里（{card_id}）"),
                "",
            )
        return Decision(ACTION_CLAIM, card_id=card_id, reason=reason), ""

    if action == ACTION_DECLINE:
        return Decision(ACTION_DECLINE, reason=reason), ""

    return Decision(
        action,
        card_id=card_id,
        reason=reason,
        ask_to=str(data.get("ask_to") or "").strip(),
        need=_truncate_one(str(data.get("need") or ""), NEED_CHARS),
        content=_truncate_one(str(data.get("content") or ""), ANSWER_CHARS),
        reply_to=str(data.get("reply_to") or "").strip(),
        kind=str(data.get("kind") or "").strip(),
    ), ""


@dataclass
class Claimed:
    """一张卡被一名成员抢到**并且派出去了**。只抢到没派出去不算（见 `RoundOutcome.released`）。"""

    card_id: str
    title: str
    employee_id: str
    task_id: str


@dataclass
class RoundOutcome:
    """
    认领循环跑完的结果。

    ★ `notes` 不是日志，是**给人读的成品句子**
    调用方会把它们写进群组时间线（施工图 §16.1：每条护栏/每个岔口都要能被界面读出来）。
    所以这里刻意写成「张三想接『整理合同』，但没抢到」而不是 `claim_failed reason=cap`：
    后者到了界面上还得再翻译一遍，而翻译处总会漏掉几种情形。

    `stalled` 只报告、不判死 —— 判死要改 run 状态，而 run 状态只有
    `run_store.advance()` 能改（那是编排器的事）。
    """

    rounds_run: int = 0
    claimed: list[Claimed] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    stalled: bool = False


def would_create_cycle(edges: list[dict[str, Any]], src: str, dst: str) -> bool:
    """
    护栏 4：再加一条「src 问 dst」，协商图上会不会成环（施工图 §7 第 4 行）。

    `edges` 是 `store.negotiation_edges(run_id)` 的行（作者 → 收件人）。
    判据是「从 dst 出发能不能走回 src」：能，则这条边一加上就是 A→B→A。

    ★ 为什么要拦这一种，而不是只拦「完全重复的问话」
    A 问 B、B 回头问 A，两个人都觉得自己已经在推进，实际是在原地花 token ——
    这是 MAST 那张失效表里「交流而不收口」的最短形态。成环时不写 `request`，
    改写一条 `blocker`：这个分歧需要**人**来定，同事之间已经没有了新信息。

    行数已被护栏 1 限死（默认 ≤24），所以这里用一次 DFS 就够，不需要更强的算法。
    """
    if not src or not dst:
        return False
    if src == dst:
        return True
    adj: dict[str, set[str]] = {}
    for edge in edges:
        a = str(edge.get("employee_id") or "")
        b = str(edge.get("recipient_employee_id") or "")
        if a and b:
            adj.setdefault(a, set()).add(b)
    seen: set[str] = set()
    stack = [dst]
    while stack:
        cur = stack.pop()
        if cur == src:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj.get(cur, ()))
    return False


#: 收件箱 / 预算 / 协商图这几个查询由调用方注入（`workspace_store` 或测试里的替身）。
#: 不写死类型：这一层只认这几个方法名，绑死一个类会让「替身跑认领轮」变成「替身继承真存储」。
_REQUIRED_MSG_METHODS = ("pending_inbox", "mark_inbox_seen", "mark_answered", "count_directed", "negotiation_edges")


async def run_claim_rounds(
    *,
    run_id: str,
    members: list[dict[str, Any]],
    board: Board,
    runs: RunStore,
    msgs: Any,
    dispatch,
    rounds: Optional[int] = None,
    stall_limit: Optional[int] = None,
) -> RoundOutcome:
    """
    认领轮循环（§5.5 / §16.1）+ 同事协商回路（§5.7）：
    摊卡与「指名我、我还没读过的话」一起问 → 他答 claim / ask / answer / handoff / decline
    → CAS 抢 → 派活 → 再摊。

    Args:
        dispatch: `(member, card) -> task_id`，由编排器给（它内部就是 `executor.start`）。
            ★ 传函数而不是 import 执行器：协调者**不派活**。它只在 CAS 已经成功之后
            被叫一声「这人真拿到卡了，你可以开工了」——
            一旦这里直接 import executor，治理层就变成了工头，产品地基那条
            「不做主 Agent 统一派活中心」当场失效。
        msgs: 群组消息存储（需要 `_REQUIRED_MSG_METHODS` 那几个方法）。定向消息是
            **落库**的，不是内存里传来传去的字符串：这样用户刷新界面看得见、
            审计数得清，而护栏 1 的预算才有唯一可信的计数处。
        rounds / stall_limit: 单测里压小好用例跑到收口；默认取 settings。

    ★ 上界为什么是「轮数」而不是「直到没有可认领的卡」
    后者在「没人愿意接」时永久转，而那正是被否决的常驻循环（§5.6）。
    轮数上限同时是 token 上限：每轮 = 每名被邀请成员一次短上下文调用。

    ★ 「交流直到完成」的收口条件是三条，缺一都会变成烧 token 的聊天室（§5.7）
    ① 看板收口（没有可摊的卡、且没人等着回话）；② 护栏 1 的协商预算；
    ③ 轮数上界。三者各自触发时都要在 `notes` 里留下一句人话。
    """
    rounds_max = settings.CLAIM_ROUNDS_MAX if rounds is None else max(0, int(rounds))
    stall_max = settings.BOARD_STALL_LIMIT if stall_limit is None else max(1, int(stall_limit))
    name_of = {
        str(m.get("id") or ""): str(m.get("name") or m.get("id") or "未命名") for m in members
    }
    out = RoundOutcome()
    group_id = str((runs.get(run_id) or {}).get("group_id") or "")
    missing = [m for m in _REQUIRED_MSG_METHODS if not hasattr(msgs, m)]
    if group_id and missing:  # pragma: no cover - 只在接线写错时响
        raise TypeError(f"msgs 缺少定向消息要用的方法: {missing}")
    #: 护栏 1 触顶那句话只能说一次。之后每次 ask 都再写一条「已触顶」，
    #: 时间线会被同一句话刷满，而用户读不出「什么时候开始触顶的」。
    budget_said = False

    def budget_left() -> bool:
        nonlocal budget_said
        if msgs.count_directed(run_id) < settings.NEGOTIATION_MAX_MESSAGES_PER_RUN:
            return True
        if not budget_said:
            budget_said = True
            out.notes.append(
                f"同事之间的往返已到上限 {settings.NEGOTIATION_MAX_MESSAGES_PER_RUN} 条，"
                "剩下的分歧要由你来定 —— 他们不会再问了"
            )
            events.guardrail_triggered(
                group_id,
                guardrail=1,
                code="negotiation_budget",
                message=(
                    f"本次协作的同事往返已到 {settings.NEGOTIATION_MAX_MESSAGES_PER_RUN} 条上限，"
                    "后续「向同事要信息」已被冻结（任务照常继续，只是不再互问）"
                ),
                run_id=run_id,
            )
        return False

    def ask(member_id: str, who: str, decision: Decision) -> None:
        target = decision.ask_to
        if not target or target == member_id or target not in name_of:
            # 编造/漏填收件人按 decline 同一条纪律处置（§5.7）：
            # 留下去就是一条永远没人回答的定向消息 —— 它占收件箱、花预算，还谁也看不见。
            out.notes.append(
                f"{who} 想问同事，但收件人不在本群名册里（{target or '没填'}）—— 这句话没有发出去"
            )
            return
        if not budget_left():
            return
        if would_create_cycle(msgs.negotiation_edges(run_id), member_id, target):
            msgs.append_message(
                group_id,
                employee_id=member_id,
                kind="blocker",
                content=f"我和{name_of[target]}之间已经绕回来了，这件事需要用户定一个方向。",
                recipient_employee_id=target,
                run_id=run_id,
            )
            out.notes.append(f"{who} 问 {name_of[target]}，但两人已经问过一轮（环）—— 由用户定夺")
            events.guardrail_triggered(
                group_id,
                guardrail=4,
                code="negotiation_cycle",
                message=f"{who} ↔ {name_of[target]} 的互相提问已成环，这条 request 被拒",
                run_id=run_id,
            )
            return
        msgs.append_message(
            group_id,
            employee_id=member_id,
            kind="request",
            content=decision.need or decision.reason,
            recipient_employee_id=target,
            run_id=run_id,
        )

    def answer(member_id: str, who: str, decision: Decision, inbox: list[dict[str, Any]]) -> None:
        asked = next((i for i in inbox if str(i.get("id") or "") == decision.reply_to), None)
        if asked is None:
            # ★ 校验「这条话真的在本轮摊给他过」放在调用方，不放 `claim_turn`
            # 模型没有可回的东西时会编一个 reply_to（见那里的 docstring）。
            # 编造的值落库 = 协商图上多一个假节点，护栏 4 之后会据此误判环。
            out.notes.append(
                f"{who} 想回的那条话不在他还没读过的指名消息里（{decision.reply_to or '没填'}）"
            )
            return
        text = (decision.content or decision.reason or "").strip()
        if not text:
            out.notes.append(f"{who} 说要回答，但没给出任何内容")
            return
        if not budget_left():
            return
        if not msgs.mark_answered(decision.reply_to):
            out.notes.append(f"{who} 想回的那条话已经被回过了 —— 同一条话只答一次")
            return
        msgs.append_message(
            group_id,
            employee_id=member_id,
            kind=decision.kind if decision.kind in ANSWER_KINDS else "commitment",
            content=text,
            recipient_employee_id=str(asked.get("employee_id") or ""),
            reply_to=decision.reply_to,
            run_id=run_id,
        )

    def handoff(member_id: str, who: str, decision: Decision, offers: list[Offer]) -> None:
        target = decision.ask_to
        title = next((o.title for o in offers if o.card_id == decision.card_id), "")
        if not decision.card_id or title == "":
            out.notes.append(
                f"{who} 想转手一张不在本轮摊出的卡（{decision.card_id or '没填'}）—— 没记录"
            )
            return
        if not target or target == member_id or target not in name_of:
            out.notes.append(
                f"{who} 建议把「{title}」转给别人，但收件人不在本群名册里（{target or '没填'}）"
            )
            return
        depth = board.handoff_depth(decision.card_id)
        if depth >= settings.HANDOFF_MAX_DEPTH:
            # ★ 超限之后这张卡照旧开放 —— 拦的是「继续往下递」，不是「这张卡不许做」。
            out.notes.append(
                f"「{title}」已经转手 {depth} 次，不再往下递了"
                "（要它继续动，得重新拆解这一批，而不是继续转手）"
            )
            events.guardrail_triggered(
                group_id,
                guardrail=5,
                code="handoff_depth",
                message=f"「{title}」的转手次数已到上限 {settings.HANDOFF_MAX_DEPTH}，停止继续传递",
                run_id=run_id,
                card_id=decision.card_id,
            )
            return
        if not budget_left():
            return
        board.handoff(decision.card_id)
        msgs.append_message(
            group_id,
            employee_id=member_id,
            kind="request",
            content=(
                f"「{title}」我不太合适{f'：{decision.reason}' if decision.reason else ''}。"
                f"建议 {name_of[target]} 来做 —— 这张卡仍然是开放的，要不要接由你自己决定。"
            ),
            recipient_employee_id=target,
            run_id=run_id,
            meta={"card_id": decision.card_id},
        )

    for _ in range(rounds_max):
        offers, skipped = offer_cards(board.ready_cards(run_id))
        if skipped:
            out.notes.append(
                f"{len(skipped)} 张卡的摘要超过 {settings.CLAIM_CARD_BRIEF_CHARS} 字，"
                "本轮没有摊出去（是没被问过，不是被拒绝）"
            )
        invited = eligible_members(members, inflight_of=lambda eid: board.inflight_count(eid))
        if not invited:
            out.notes.append("本轮没有可邀请的成员：他们要么手上都有活，要么不允许发起请求")
            # ★ 这**不算空转**，所以不累加 stall_count：没人接活不是因为没人愿意，
            # 而是所有人都在干活（或者这个角色压根不允许发起请求）。把它计进停滞，
            # 一个满载的团队会在两轮之内被误判成「这件事推不动了」—— 而那正是
            # 护栏 3 唯一存在的意义要避免的谎。剩下的卡等在职务务完成后自然会再摊。
            break
        # 收件箱在这一轮开头读一次（不在每个成员_turn_里反复读）：
        # 同一次「互相交流」里前一个人刚问出的话，要等下一轮才轮到后一个人看，
        # 于是一轮的对话量有界 = `成员数` 条，而不是滚成一串递归。
        inboxes = {
            str(m.get("id") or ""): msgs.pending_inbox(
                group_id, employee_id=str(m.get("id") or ""), run_id=run_id
            )
            for m in invited
        }
        only_replies = not offers
        if only_replies:
            if not any(inboxes.values()):
                # 手里没有可摊的卡、也没人等着回话：要么全在飞/全到终态，要么整批被依赖挡在门外。
                # 这**不算空转**（不是在等人接活却没等到，而是在等已经接活的人干活），
                # 所以不累加 stall_count —— 否则成员正经干活的那几轮会把 run 误判成停滞。
                break
            out.notes.append(
                "现在没有新的卡可摊，先让同事把指名彼此的话回完（"
                "下一次摊卡要等这一批交出去）"
            )

        out.rounds_run += 1
        runs.note_claim_round(run_id)

        won_this_round = 0
        for member in invited:
            if not offers and not inboxes.get(str(member.get("id") or "")):
                # 本轮摊出的卡已被前面的人接完，而他自己也没有要回的话。
                # 继续问下去只会让后面那几个成员各记一条「他没参加过的轮次」，白打模型。
                break
            eid = str(member.get("id") or "")
            who = name_of.get(eid, eid)
            inbox = inboxes.get(eid, [])
            decision, failure = await claim_turn(
                member,
                offers,
                inflight=board.inflight_count(eid),
                inbox=inbox,
                roster=members,
            )
            if decision is None:
                # 没问到话与「问到了但他不要」是两种完全不同的处置
                # （等一下再问 / 换个人做），所以这句必须进时间线。
                # ★ 这种时候**不**标已读：模型压根没读到那几条话，标了就等于
                # 让一句没人回答的提问永远沉进收件箱 —— 提问的人会一直等。
                out.notes.append(f"{who}：{failure}")
                continue
            if inbox:
                # 进过一轮输入就算读过（§5.7）。这是「同一条话每轮被重答一遍」的闸，
                # 而它代价是对等交换：他没答的那条，之后也不会再提醒他。
                msgs.mark_inbox_seen([str(i.get("id") or "") for i in inbox])

            if decision.action == ACTION_DECLINE:
                continue
            if decision.action == ACTION_ASK:
                ask(eid, who, decision)
                continue
            if decision.action == ACTION_ANSWER:
                answer(eid, who, decision, inbox)
                continue
            if decision.action == ACTION_HANDOFF:
                handoff(eid, who, decision, offers)
                continue

            card_id = decision.card_id
            title = next((o.title for o in offers if o.card_id == card_id), "")
            won = board.claim(card_id, employee_id=eid, task_id="")
            if won is None:
                fresh = board.get(card_id)
                owner = str((fresh or {}).get("owner_employee_id") or "")
                # 陈的 offers 里查不到标题时（员工在等待期间从接口直接抢了别的卡），
                # 用库里的真标题 —— 时间线上出现「「」已被认领」是读不出来的。
                title = title or str((fresh or {}).get("title") or card_id)
                if owner or str((fresh or {}).get("status") or "") != CARD_TODO:
                    # 卡已经不属于这个池子了：本轮别再把它摊给下一个人（会被反复抢）。
                    offers = [o for o in offers if o.card_id != card_id]
                    out.notes.append(f"「{title}」已被{owner or '别人'}认领，不再生效")
                else:
                    # 卡还开放，是他自己的负载满了 —— 留在池子里给下一个人。
                    out.notes.append(f"{who} 想接「{title}」，但他手上已经有一张在飞")
                continue

            task_id = ""
            try:
                task_id = str(dispatch(member, won) or "")
            except Exception as e:  # noqa: BLE001
                logger.warning("派发失败（卡片退回待认领）: card=%s emp=%s %s", card_id, eid, e)
            if not task_id:
                # ★ 抢到却没派出去 → 必须**释放**，否则是一张永远 in_progress 的死卡：
                # 界面上转到天荒地老，背后什么也没跑（ADR 0020 点名的症状）。
                # `advance_card(..., todo)` 会把 owner 与 task_id 一起归零。
                released = board.advance_card(card_id, CARD_TODO)
                if released is not None:
                    out.released.append(card_id)
                    out.notes.append(f"{who} 接到「{title}」但没能开工，卡片已退回待认领")
                else:
                    out.notes.append(f"{who} 接到「{title}」但没能开工，卡片状态已变，未强行回收")
                continue

            # 挂上执行真相的指针（`task_id` 是两份真相唯一的缝合键）
            board.attach_task(card_id, employee_id=eid, task_id=task_id)
            msgs.append_message(
                group_id, employee_id=eid, kind="commitment",
                content=f"这部分我来负责：{title}。{decision.reason}",
                run_id=run_id, task_id=task_id,
            )
            out.claimed.append(Claimed(card_id=card_id, title=title, employee_id=eid, task_id=task_id))
            offers = [o for o in offers if o.card_id != card_id]
            won_this_round += 1

        if only_replies:
            # 「只回话」那一轮跑完就结束：它不累加 stall（同事在互相回答，不是推不动），
            # 也不会有第二轮 —— 第二轮要的解锁条件是「有卡重新可摊」，而那是 `_settle_run`
            # 的续摊路径（§5.3），不是这里的循环。留在这儿继续转就是 §5.6 否决的常驻循环。
            # ★ 判据用 `only_replies` 而不是重新看 `offers`：
            # 正常那一轮里如果最后一个人把卡接完了，`offers` 也会变空，
            # 而那**必须**继续走到下面的 `reset_stall` —— 在那儿 break 会把
            # 「有人接上活了」记成「这轮白跑」，护栏 3 就靠这个数活着。
            break

        if won_this_round:
            runs.reset_stall(run_id)
            continue
        if runs.bump_stall(run_id) >= stall_max:
            out.stalled = True
            out.notes.append(f"连续 {stall_max} 轮没有人接上活 —— 这件事推不动了")
            break

    return out


__all__ = [
    "ACTION_ANSWER",
    "ACTION_ASK",
    "ACTION_CLAIM",
    "ACTION_DECLINE",
    "ACTION_HANDOFF",
    "ANSWER_KINDS",
    "CARDS_MAX",
    "CLAIM_ACTIONS",
    "Claimed",
    "Decomposition",
    "Decision",
    "Offer",
    "RoundOutcome",
    "card_key",
    "claim_turn",
    "decompose",
    "eligible_members",
    "inbox_line",
    "offer_cards",
    "run_claim_rounds",
    "would_create_cycle",
]
