"""
会话历史的存储层 —— 文件是唯一事实来源

★ 为什么不复用 Redis Checkpointer

Checkpointer 里确实已经有一份「会话状态」，但它有两个不能当作会话历史的原因：

1. **它存的是给模型看的东西**。LangGraph 的 `messages` 只有文本与 tool_calls，
   拿不回前端要渲染的引用来源、工具卡片、审批留痕、记忆回执 ——
   用它恢复出来的界面会和用户当时看到的**不一样**。
2. **它是可降级依赖**。Redis 挂了会自动退化成 InMemorySaver（不变量 14），
   于是「重启一次聊天记录全没了」—— 而聊天记录恰恰是最不能丢的那类数据。
   这与长期记忆的选择同源（`decisions/0010`）。

★ 目录布局：索引 + 每会话一个轮次文件

    data/conversations/
      index.json           会话索引（标题/时间/轮次数/摘要），原子整文件写
      <thread_id>.jsonl    该会话的轮次，append-only，一行一轮

为什么不是「一个大 JSONL 装全部会话」（审批留痕是那种做法）：

- **删除**。审批记录只增不删（审计证据）；会话历史是用户会主动删的东西。
  按会话分文件后，删除就是 `unlink` 一次 —— 换成单一大文件，每次删除都要
  重写整个文件，而重写窗口里进程被杀就会留下一份不完整的全量历史。
- **读取**。打开一个会话要读的只是它自己的轮次；单一大文件得扫全量。
- **损坏半径**。一个会话的文件坏掉只影响它自己。

代价是文件数随会话数增长 —— 由 `CONVERSATION_MAX_ITEMS` 兜住（按最后活跃时间淘汰）。

★ 索引是**缓存**，不是事实来源

`index.json` 里的每个字段都能从轮次文件重新算出来，所以它在缺失/损坏时
可以整体重建（`_rebuild_index`）。这条性质是有意的：索引写坏了只是慢一次，
而不是「聊天记录不见了」。同理，索引里的条目若对应不到文件，会把 `exists=False`
如实返回给界面 —— 而不是装作还在。

★ 并发

单进程 uvicorn，用一把进程内锁（与 `MemoryStore` / `ApprovalStore` 同样的取舍）。
索引的读改写是复合操作，必须在锁内；轮次追加也走同一把锁，
保证「文件里已有这一轮」⇒「索引已更新」的顺序。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app import artifacts as artifact_store
from app.config import settings
from app.conversations.files import collect_written_files
from app.conversations.models import (
    INITIATED_BY_USER,
    STATUS_ABORTED,
    STATUS_ERROR,
    STATUS_LABELS,
    STATUS_OK,
    ConversationSummary,
    ConversationTurn,
    default_thread_title,
    derive_title,
    events_for_replay,
    infer_status,
    is_trivial_turn,
    now_iso,
    preview_of,
    summarize_turns,
)

logger = logging.getLogger(__name__)

INDEX_VERSION = 1
TURN_VERSION = 1

#: 会话 id 的合法形态。
#:
#: ⚠️ `thread_id` 会**直接出现在文件路径与 URL 路径**里（`<thread_id>.jsonl`、
#: `/api/conversations/{thread_id}`），所以它必须被严格校验 ——
#: 否则一个 `../../backend/.env` 就能借删除接口删掉任意文件、
#: 借详情接口读取任意文件。这是本模块唯一的对外攻击面。
#: 前端生成的是 uuid4（含 `-`），验收脚本会用 `verify-hitl-xxx` 这类名字，
#: 所以允许字母数字与 `-` / `_`，其余一律拒绝。宁窄勿宽。
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_valid_thread_id(thread_id: str) -> bool:
    return bool(thread_id) and bool(_THREAD_ID_RE.match(thread_id))


#: 助理会话 thread 的前缀（不变量 58 的第四根前缀）。
#:
#: - 用户主会话：uuid4
#: - 后台子任务：`subtask_<task_id>`（`agents/subgraph.py`）
#: - 员工私聊：`emp_<employee_id>`（`workspace/__init__.py`）
#: - 助理会话：`assistant_<uuid>`（本模块）—— 侧栏「助理」入口的独立命名空间，
#:   与普通对话列表互相不可见；主对话入口（`graph.get_agent_event_stream`）
#:   对它放行，因为助理对话走的就是主链路，只是归属不同。
ASSISTANT_THREAD_PREFIX = "assistant_"

#: 助理只有这一条会话，id 固定（与前端 `ASSISTANT_THREAD_ID` 对齐）。
#: 微信通道把消息送进这里，用户在桌面端打开「助理」就能看到同一段对话。
ASSISTANT_MAIN_THREAD_ID = f"{ASSISTANT_THREAD_PREFIX}main"


def is_assistant_thread_id(thread_id: str) -> bool:
    """这个 thread_id 属于助理命名空间（普通对话列表必须过滤掉它）。"""
    return bool(thread_id) and thread_id.startswith(ASSISTANT_THREAD_PREFIX)


def make_snippet(text: str, needle: str, width: int) -> Optional[Dict[str, str]]:
    """
    在压平空白后的文本里定位第一处命中，拆成「前文 / 命中 / 后文」三段。

    ★ 为什么把片段拆成三段、而不是给一个带下标的整串

    高亮是**呈现**决策，该由前端做；但「命中在哪」只有搜过的人知道。
    后端给出三段，前端只要把它们按顺序拼起来、给中间那段套一层 `<mark>` ——
    不需要在前端再做一次 `indexOf`（那会在两处各维护一份「怎么算命中」的规则，
    而它们在大小写、空白压平等细节上必然漂移）。

    ★ 为什么先压平空白

    轮次正文里全是换行与缩进。原样截出来放进侧栏，一行会被折成七八行，
    用户反而看不出命中在哪。压平之后再定位，片段与命中位置自然一致。
    """
    flat = " ".join((text or "").split())
    if not flat:
        return None
    idx = flat.lower().find(needle.lower())
    if idx < 0:
        return None
    start = max(0, idx - width)
    end = min(len(flat), idx + len(needle) + width)
    return {
        "prefix": ("…" if start > 0 else "") + flat[start:idx],
        "match": flat[idx : idx + len(needle)],
        "suffix": flat[idx + len(needle) : end] + ("…" if end < len(flat) else ""),
    }


class ConversationStore:
    """会话历史的持久化（索引 + 每会话一个 append-only 轮次文件）"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root
        # 解析一次就记住：`root` 属性每次访问都会 mkdir，别放进热路径
        self._resolved: Optional[Path] = None
        self._lock = threading.RLock()
        self._loaded = False
        #: thread_id → ConversationSummary（列表顺序由 updated_at 排序决定）
        self._index: Dict[str, ConversationSummary] = {}

    # ---------- 路径与开关 ----------
    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "CONVERSATION_ENABLED", True))

    @property
    def root(self) -> Path:
        if self._resolved is None:
            self._resolved = self._root or settings.conversation_dir
        return self._resolved

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _turn_path(self, thread_id: str) -> Path:
        return self.root / f"{thread_id}.jsonl"

    @property
    def max_items(self) -> int:
        return max(0, int(getattr(settings, "CONVERSATION_MAX_ITEMS", 200)))

    @property
    def title_chars(self) -> int:
        return max(8, int(getattr(settings, "CONVERSATION_TITLE_CHARS", 40)))

    # ---------- 索引 ----------
    def _ensure_loaded(self) -> None:
        """惰性加载索引；缺失或损坏时从轮次文件重建"""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._index = self._read_index() or self._rebuild_index()
            self._loaded = True

    def _read_index(self) -> Optional[Dict[str, ConversationSummary]]:
        """读索引；文件不存在/损坏时返回 None（由调用方决定是否重建）"""
        path = self.index_path
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("conversations") or []
            index: Dict[str, ConversationSummary] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                summary = ConversationSummary.from_dict(item)
                if is_valid_thread_id(summary.thread_id):
                    index[summary.thread_id] = summary
            return index
        except Exception as e:  # noqa: BLE001
            # 索引是可再生的缓存：坏了就重建，绝不让它挡住建一个功能可用
            logger.warning("会话索引读取失败，将从轮次文件重建: %s", e)
            return None

    def _rebuild_index(self) -> Dict[str, ConversationSummary]:
        """
        扫目录重建索引。

        走的是「逐文件折叠」这条最慢但最可靠的路径 —— 它同时也是
        `verify_conversations.py` 用来证明「索引与文件一致」的同一个实现。
        """
        index: Dict[str, ConversationSummary] = {}
        try:
            paths = sorted(self.root.glob("*.jsonl"))
        except OSError as e:  # noqa: BLE001
            logger.warning("会话目录不可读（按空处理）: %s", e)
            return index

        for path in paths:
            thread_id = path.stem
            if not is_valid_thread_id(thread_id):
                continue
            turns = self._read_turns(path)
            if not turns:
                continue
            index[thread_id] = summarize_turns(thread_id, turns)
        if index:
            logger.info("会话索引已从 %s 个轮次文件重建", len(index))
        return index

    def _save_index(self) -> None:
        """
        原子整文件写索引（临时文件 + `os.replace`）。

        与轮次文件的 append-only 是**相反取舍**：索引是派生数据，
        要的是「要么是旧的完整版、要么是新的完整版」，不能出现半截文件。
        """
        payload = {
            "version": INDEX_VERSION,
            "updated_at": now_iso(),
            "conversations": [
                s.to_dict() for s in self._sorted_summaries()
            ],
        }
        tmp = self.index_path.with_suffix(".json.tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except Exception as e:  # noqa: BLE001
            # 索引写失败**不影响**本轮已经落盘的轮次（文件才是事实来源），
            # 只是列表少一条 —— 下次写入会重试。
            logger.warning("会话索引写入失败（轮次文件不受影响）: %s", e)

    def _sorted_summaries(self) -> List[ConversationSummary]:
        """按最后活跃时间倒序（最近聊的排最前）"""
        return sorted(
            self._index.values(),
            key=lambda s: (s.updated_at or "", s.created_at or ""),
            reverse=True,
        )

    # ---------- 轮次文件读写 ----------
    def _read_turns(self, path: Path) -> List[ConversationTurn]:
        """
        读一个会话的全部轮次。

        坏行跳过（追加写的崩溃安全前提）：进程被杀时最多留下半截最后一行，
        跳过它就能拿到之前所有完整的轮次。整个文件读不出来时返回空列表，
        而不是抛异常 —— 会话历史是增益能力，它坏掉不该让接口 500。
        """
        turns: List[ConversationTurn] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict) or data.get("event") != "turn":
                        continue
                    turn = ConversationTurn.from_dict(data)
                    turn.thread_id = turn.thread_id or path.stem
                    turn.status = infer_status(turn)
                    turns.append(turn)
        except OSError as e:
            logger.warning("会话轮次文件读取失败，按空处理: %s (%s)", path, e)
        return turns

    def _append_line(self, path: Path, payload: Dict[str, Any]) -> bool:
        """追加一行 JSON。任何 IO 失败只告警不抛（旁路能力）"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("会话历史写入失败（不影响本轮问答）: %s", e)
            return False

    # ---------- 写入 ----------
    def append_turn(
        self,
        thread_id: str,
        *,
        user_input: str,
        answer: str,
        events: Optional[List[Dict[str, Any]]] = None,
        status: str = STATUS_OK,
        error: str = "",
        attachments: Optional[List[Dict[str, str]]] = None,
        usage: Optional[Dict[str, int]] = None,
        reasoning: str = "",
        files: Optional[List[Dict[str, str]]] = None,
        turn_id: str = "",
        initiated_by: str = INITIATED_BY_USER,
    ) -> Optional[ConversationSummary]:
        """
        记录一轮对话（会话不存在则同时建档）。

        返回更新后的索引项；`enabled=False` / 非法 id / 空轮次返回 None。
        非法 id **必须**拒绝：它会变成文件路径（见 `_THREAD_ID_RE` 的说明）。

        `turn_id` 由调用方传入时以它为准 —— 主对话要用**流开始时**生成的那个值，
        因为本轮用户消息在 Checkpointer 里的 id 是由它派生的
        （`state.turn_message_id`），两处必须是同一个身份才能按轮删上下文。
        """
        if not self.enabled:
            return None
        if not is_valid_thread_id(thread_id):
            logger.warning("拒绝写入非法 thread_id（疑似路径穿越）: %r", thread_id)
            return None

        replay_events = events_for_replay(events or [])
        if is_trivial_turn(user_input, answer, replay_events):
            # 连图都没跑起来的一轮（降级、断连、空回答）不落盘：
            # 否则列表里会堆一批点进去什么都没有的空壳会话
            return None

        turn = ConversationTurn(
            turn_id=turn_id or uuid.uuid4().hex[:16],
            thread_id=thread_id,
            created_at=now_iso(),
            user_input=user_input or "",
            answer=answer or "",
            events=replay_events,
            status=status if status in STATUS_LABELS else STATUS_OK,
            error=error or "",
            # ★ 派生而不是让调用方传：写入侧只有一个派生点，就不会出现
            # 「某个调用方忘了传 files」这种只在某条路径上发生的漏记。
            # 唯一的例外是 `files=` 显式传入：后台任务（含定时运行）的产物
            # 在**它自己的记录**里已经派生过一次（`TaskRecord.files`），
            # 这里的事件流是折叠后的进度条目、不是原始 tool_call，
            # 再派生一遍只会得到空列表 —— 传进来才是那份已经算对的真相。
            files=files if files is not None else collect_written_files(replay_events),
            attachments=attachments or [],
            # 用量不能从 `replay_events` 派生 —— `done` 事件本身不进重放
            # （不变量 45），所以它必须由调用方显式传入。
            usage=usage or {},
            reasoning=reasoning or "",
            initiated_by=initiated_by,
        )

        with self._lock:
            self._ensure_loaded()
            path = self._turn_path(thread_id)
            if not path.is_file():
                # 首轮：写一条 meta 头（人可读 + 便于手工排查），再写轮次
                self._append_line(
                    path,
                    {
                        "v": TURN_VERSION,
                        "event": "meta",
                        "thread_id": thread_id,
                        "created_at": turn.created_at,
                        "title": self._title_for(turn.user_input, thread_id),
                    },
                )
            if not self._append_line(path, {"v": TURN_VERSION, "event": "turn", **turn.to_dict()}):
                return None

            prev = self._index.get(thread_id)
            self._index[thread_id] = ConversationSummary(
                thread_id=thread_id,
                title=(
                    prev.title
                    if prev and prev.title
                    else self._title_for(turn.user_input, thread_id)
                ),
                created_at=prev.created_at if prev and prev.created_at else turn.created_at,
                updated_at=turn.created_at,
                turns=(prev.turns if prev else 0) + 1,
                preview=preview_of(turn),
                last_status=turn.status,
                exists=True,
            )
            self._prune_locked()
            self._save_index()
            summary = self._index.get(thread_id)

        # 版本快照放在锁外：会话索引锁不该被大文件复制拖住
        try:
            artifact_store.record_turn(thread_id, turn.files, turn.turn_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("产物版本快照失败（不影响本轮落盘）: %s %s", thread_id, e)
        return summary

    def _title_for(self, user_input: str, thread_id: str = "") -> str:
        return derive_title(
            user_input,
            self.title_chars,
            fallback=default_thread_title(thread_id),
        )

    def _prune_locked(self) -> None:
        """超过条数上限时淘汰最旧的会话（索引与轮次文件一起删）"""
        limit = self.max_items
        if limit <= 0 or len(self._index) <= limit:
            return
        ordered = self._sorted_summaries()
        for stale in ordered[limit:]:
            self._index.pop(stale.thread_id, None)
            self._delete_file(stale.thread_id)
            logger.info("会话历史超上限（%s），已淘汰最旧会话: %s", limit, stale.thread_id)

    def _delete_file(self, thread_id: str) -> bool:
        """删除轮次文件；返回**是否真的删掉了**（文件本来就不在时为 False）"""
        path = self._turn_path(thread_id)
        try:
            if not path.is_file():
                return False
            path.unlink(missing_ok=True)
            return True
        except OSError as e:
            logger.warning("会话轮次文件删除失败: %s (%s)", thread_id, e)
            return False

    # ---------- 查询 ----------
    def list_conversations(
        self, *, limit: int = 50, offset: int = 0
    ) -> Tuple[List[ConversationSummary], int]:
        """按最后活跃时间倒序列出会话（返回 `(本页, 总数)`）"""
        with self._lock:
            self._ensure_loaded()
            ordered = self._sorted_summaries()
            # 每次列都刷一遍文件存在性：索引说「有」但文件被手工删掉时，
            # 如实标成 exists=False，而不是让用户点进去看到空会话
            for summary in ordered:
                summary.exists = self._turn_path(summary.thread_id).is_file()
            offset = max(0, offset)
            limit = max(1, limit)
            return ordered[offset : offset + limit], len(ordered)

    def get_conversation(self, thread_id: str) -> Optional[Tuple[ConversationSummary, List[ConversationTurn]]]:
        """读一个会话的摘要 + 全部轮次（不存在返回 None）"""
        if not is_valid_thread_id(thread_id):
            return None
        with self._lock:
            self._ensure_loaded()
            path = self._turn_path(thread_id)
            if not path.is_file():
                return None
            turns = self._read_turns(path)
            summary = self._index.get(thread_id) or summarize_turns(thread_id, turns)
            summary.exists = True
            summary.turns = len(turns)
            if turns:
                summary.updated_at = turns[-1].created_at
                summary.preview = preview_of(turns[-1])
                summary.last_status = turns[-1].status
            return summary, turns

    def title_of(self, thread_id: str) -> str:
        """
        取会话标题（**只读索引，不读轮次文件**）。

        给用量账本用：账本记录只有 thread_id，界面要认得出「哪个对话窗口」。
        走 `get_conversation()` 会把整个轮次文件读进来（一轮几十 KB 事件流），
        为了一行标题付这个代价不值 —— 索引里本来就有标题。
        会话被删掉后索引里查不到，返回空串，由调用方回退到自己的标题快照。
        """
        if not is_valid_thread_id(thread_id):
            return ""
        with self._lock:
            self._ensure_loaded()
            summary = self._index.get(thread_id)
            return summary.title if summary else ""

    def seed_messages(
        self, thread_id: str, *, max_turns: int = 20
    ) -> List[Tuple[str, str, str]]:
        """
        取出用于**播种 Checkpointer** 的 (用户输入, 回答, turn_id) 序列。

        只取最近 `max_turns` 轮：播种的目的是让模型「记得刚才聊了什么」，
        而这个目的是随轮次距离衰减的。把半年前的几十轮全塞回去，
        只会撑爆上下文、并让久远的内容与当前问题争夺注意力
        （同长期记忆的注入上限，不变量 27）。

        `turn_id` 一并带出：播种方用它给用户消息补回 `turn:<turn_id>` 锚点，
        否则一次 Redis 重启就抹掉所有轮次的身份，删单轮只能整段重建。
        """
        loaded = self.get_conversation(thread_id)
        if loaded is None:
            return []
        _, turns = loaded
        recent = turns[-max_turns:] if max_turns > 0 else turns
        return [
            (t.user_input, t.answer, t.turn_id)
            for t in recent
            if (t.user_input or "").strip() and (t.answer or "").strip()
        ]

    def stats(self) -> Dict[str, Any]:
        """供启动日志 / 列表接口的元信息"""
        with self._lock:
            self._ensure_loaded()
            turns = sum(s.turns for s in self._index.values())
            return {
                "enabled": self.enabled,
                "dir": str(self.root),
                "conversations": len(self._index),
                "turns": turns,
                "max_items": self.max_items,
                "labels": {"status": STATUS_LABELS},
            }

    # ---------- 变更 ----------
    def fork_conversation(
        self, source_thread_id: str, turns: int
    ) -> Optional[ConversationSummary]:
        """
        把一个会话的**前 N 轮**复制成一个新会话（「从这里分支」）。

        ★ 为什么是服务端复制而不是前端重发

        前端「分支」的语义是「保留当时的问与答，从那里另起一条线」——
        重发消息会让模型再跑一遍（工具副作用、token 全部重来），那不是分支，
        是重播。复制轮次文件才是把「已经发生过的事实」原样搬过去。

        ★ 新会话用新的 turn_id

        轮次 id 在新会话里是**新的身份**：后台任务的 `parent_turn_id`、
        链路追踪的关联都指向旧会话里的那一轮，沿用旧 id 会让两个会话
        「共享」同一轮的痕迹 —— 分支恰恰要切断这种共享。

        `created_at` 原样保留：分支复制的是历史，历史发生在过去；
        列表按 `updated_at`（由轮次时间派生）排序，旧时间就是它的真实位置。
        """
        if not self.enabled or not is_valid_thread_id(source_thread_id):
            return None
        loaded = self.get_conversation(source_thread_id)
        if loaded is None:
            return None
        _, all_turns = loaded
        keep = all_turns[: max(0, turns)]
        if not keep:
            return None

        # 分支留在原命名空间：助理会话分支出来还是助理会话，
        # 否则它会「越狱」到普通对话列表里（前缀即归属）。
        new_uuid = str(uuid.uuid4())
        new_thread_id = (
            f"{ASSISTANT_THREAD_PREFIX}{uuid.uuid4().hex}"
            if is_assistant_thread_id(source_thread_id)
            else new_uuid
        )
        with self._lock:
            self._ensure_loaded()
            path = self._turn_path(new_thread_id)
            if path.exists():  # uuid4 撞上已存在的会话 —— 理论不可能，防手滑
                return None
            self._append_line(
                path,
                {
                    "v": TURN_VERSION,
                    "event": "meta",
                    "thread_id": new_thread_id,
                    "created_at": now_iso(),
                    "title": self._title_for(keep[0].user_input, new_thread_id),
                },
            )
            for turn in keep:
                data = turn.to_dict()
                data["thread_id"] = new_thread_id
                data["turn_id"] = uuid.uuid4().hex[:16]
                self._append_line(path, {"v": TURN_VERSION, "event": "turn", **data})

            summary = summarize_turns(new_thread_id, keep)
            summary.exists = True
            self._index[new_thread_id] = summary
            self._prune_locked()
            self._save_index()
            return summary

    def remove_turn(
        self, thread_id: str, turn_index: int
    ) -> Tuple[bool, Optional[ConversationSummary], Optional[ConversationTurn]]:
        """
        删除一个会话的第 `turn_index` 轮（0 基）。

        ★ 删除的粒度是「一轮」（一问一答），不是单条消息

        轮次文件的最小单元就是一轮 —— 拆开存半轮会让重放路径
        （`openConversation` 按轮重建 user+assistant 消息对）出现孤儿。

        ★ 重写策略：按行过滤，其余行字节原样保留

        meta 头（标题、建档时间）不在索引派生范围内时会被 `_rebuild_index`
        用到，逐行过滤而不是「反序列化再序列化」可以保证留下的内容一个
        字符都不动。原子性靠临时文件 + `os.replace`（与索引写同一套做法）。

        ★ 轮次全删光时连会话一起删

        只剩一个 meta 头的文件是一个点进去空空如也的死壳 ——
        与其让它在列表里挂着，不如走会话删除的同一条路。

        ★ 返回被删掉的那一轮（第三个值）

        模型上下文（Checkpointer）在另一处，本模块不碰它。
        调用方需要 `turn_id` 才能在那边把这一轮的消息摘掉
        （`graph.remove_turn_messages_from_context`）；只删文件而不同步，
        下一轮问答模型仍然记得用户删掉的内容。
        """
        if not self.enabled or not is_valid_thread_id(thread_id):
            return False, None, None
        with self._lock:
            self._ensure_loaded()
            path = self._turn_path(thread_id)
            if not path.is_file():
                return False, None, None
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as e:  # noqa: BLE001
                logger.warning("读取轮次文件失败，放弃删除: %s (%s)", thread_id, e)
                return False, None, None

            kept: List[str] = []
            seen_turns = 0
            removed: Optional[ConversationTurn] = None
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    # 半截行（进程被杀的残留）：原样保留，交给 _read_turns 跳过
                    kept.append(line)
                    continue
                if isinstance(data, dict) and data.get("event") == "turn":
                    if seen_turns == turn_index:
                        seen_turns += 1
                        removed = ConversationTurn.from_dict(data)
                        removed.thread_id = removed.thread_id or thread_id
                        continue
                    seen_turns += 1
                kept.append(line)

            if removed is None:
                return False, None, None

            if kept:
                tmp = path.with_suffix(".jsonl.tmp")
                try:
                    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
                    os.replace(tmp, path)
                except OSError as e:  # noqa: BLE001
                    logger.warning("重写轮次文件失败（原文件未动）: %s (%s)", thread_id, e)
                    return False, None, None
                remaining = self._read_turns(path)
                if remaining:
                    summary = summarize_turns(thread_id, remaining)
                    summary.exists = True
                    self._index[thread_id] = summary
                    self._save_index()
                    return True, summary, removed
            # 轮次已删光（或剩下的读不出来）→ 会话整个下线
            self._index.pop(thread_id, None)
            self._delete_file(thread_id)
            self._save_index()
            return True, None, removed

    def rename(self, thread_id: str, title: str) -> Optional[ConversationSummary]:
        """
        改标题。

        标题是**索引里的派生字段**（原本由首轮输入截断而来），所以改名只改索引，
        不动轮次文件 —— 轮次文件里存的是用户原话，不该被改名这种展示层操作污染。
        代价是索引损坏重建后会退回「首轮输入」那个标题，这是可接受的。
        """
        if not self.enabled or not is_valid_thread_id(thread_id):
            return None
        clean = " ".join((title or "").split())[: self.title_chars]
        if not clean:
            return None
        with self._lock:
            self._ensure_loaded()
            summary = self._index.get(thread_id)
            if summary is None:
                return None
            summary.title = clean
            self._save_index()
            return summary

    def apply_auto_title(
        self, thread_id: str, title: str, *, expected: str = ""
    ) -> Optional[ConversationSummary]:
        """
        应用模型生成的标题 —— 只在标题**仍是调用方看到的那一个**时生效。

        ★ 为什么必须比对 `expected`

        生成标题是一个几秒钟的窗口。在这几秒里用户完全可能自己改了名
        （这正是「首轮是寒暄」时用户唯一的补救手段），也可能已经又发了一轮。
        比对在锁内进行，于是「检查」与「写入」之间没有缝隙 ——
        模型的结果要么落在它该落的地方，要么整条丢掉，不会覆盖任何新东西。

        `expected` 为空表示不做比对（调用方明确知道自己在覆盖什么）。
        """
        if not self.enabled or not is_valid_thread_id(thread_id):
            return None
        clean = " ".join((title or "").split())[: self.title_chars]
        if not clean:
            return None
        with self._lock:
            self._ensure_loaded()
            summary = self._index.get(thread_id)
            if summary is None:
                return None
            if expected and summary.title != expected:
                return None
            if summary.title == clean:
                # 模型给出的标题和现有的一模一样：没有变化，不必写盘
                return summary
            summary.title = clean
            self._save_index()
            return summary

    def search(
        self,
        query: str,
        *,
        limit: int = 30,
        scan_limit: int = 200,
        per_conversation: int = 3,
        snippet_width: int = 80,
    ) -> List[Dict[str, Any]]:
        """
        跨会话搜「对话正文」（用户输入 + 助手回答），返回命中会话 + 片段。

        ★ 为什么搜正文而不是标题

        标题是派生出来的（多半还是自动生成的），用户记不住它写了什么；
        用户记得住的是**自己说过的那句话**。「那条问报销的对话」——
        「报销」在正文里，不在标题里。

        ★ 排序与截断

        会话按最后活跃时间倒序（和列表一致），每个会话最多给
        `per_conversation` 条命中片段，但**如实报告 `match_count`** ——
        界面因此能说「共 7 处，显示前 3 处」，而不是假装只有 3 处。
        命中的会话数到达 `limit` 就停（调用方给的是「一屏能看多少」）。

        ★ 同步扫文件是可接受的

        单用户桌面场景下会话数由 `CONVERSATION_MAX_ITEMS`（默认 200）兜住，
        每轮正文几 KB，全量扫一遍是几十毫秒量级。为它引一套倒排索引，
        换来的复杂度远大于收益（同「不引模糊搜索库」的取舍）。
        """
        needle = " ".join((query or "").split())
        if not needle:
            return []

        results: List[Dict[str, Any]] = []
        with self._lock:
            self._ensure_loaded()
            ordered = self._sorted_summaries()[: max(1, scan_limit)]
            for summary in ordered:
                path = self._turn_path(summary.thread_id)
                if not path.is_file():
                    continue
                turns = self._read_turns(path)
                matches: List[Dict[str, Any]] = []
                match_count = 0
                for turn in turns:
                    for role, text in (("user", turn.user_input), ("assistant", turn.answer)):
                        snippet = make_snippet(text, needle, snippet_width)
                        if snippet is None:
                            continue
                        match_count += 1
                        if len(matches) < per_conversation:
                            matches.append(
                                {
                                    "turn_id": turn.turn_id,
                                    "created_at": turn.created_at,
                                    "role": role,
                                    "snippet": snippet,
                                }
                            )
                if not matches:
                    continue
                results.append(
                    {
                        "thread_id": summary.thread_id,
                        "title": summary.title,
                        "updated_at": summary.updated_at,
                        "turns": len(turns),
                        "match_count": match_count,
                        "matches": matches,
                    }
                )
                if len(results) >= max(1, limit):
                    break
        return results

    def delete_conversation(self, thread_id: str) -> bool:
        """删除一个会话（索引 + 轮次文件）。返回是否真的删掉了东西"""
        if not is_valid_thread_id(thread_id):
            return False
        with self._lock:
            self._ensure_loaded()
            existed = self._index.pop(thread_id, None) is not None
            removed = self._delete_file(thread_id)
            if existed or removed:
                self._save_index()
        # 快照目录释放会话锁之后再删：大文件 rmtree 不该堵住列表/落盘。
        # 只要 id 合法就清一次 —— 轮次文件被手工删掉时目录会变成孤儿。
        artifact_store.purge_thread(thread_id)
        return existed or removed

    def clear(self, thread_filter: Optional[Callable[[str], bool]] = None) -> List[str]:
        """
        清空会话历史，返回被清掉的 thread_id 列表。

        `thread_filter` 为 None 时清全部；给出时只清「谓词为真」的会话
        （按命名空间清空用，例如只清助理会话）。调用方拿到 id 列表后
        自行联动清理 Checkpointer / 后台任务。
        """
        with self._lock:
            self._ensure_loaded()
            targets = [
                tid for tid in list(self._index)
                if thread_filter is None or thread_filter(tid)
            ]
            for thread_id in targets:
                self._delete_file(thread_id)
                self._index.pop(thread_id, None)
                artifact_store.purge_thread(thread_id)
            self._save_index()
            logger.info("会话历史已清空：%s 个会话", len(targets))
            return targets


#: 全局单例（与 memory / approval 同样的用法）
conversation_store = ConversationStore()

__all__ = [
    "ASSISTANT_MAIN_THREAD_ID",
    "ASSISTANT_THREAD_PREFIX",
    "ConversationStore",
    "conversation_store",
    "is_assistant_thread_id",
    "is_valid_thread_id",
    "make_snippet",
    "STATUS_ABORTED",
    "STATUS_ERROR",
    "STATUS_OK",
]
