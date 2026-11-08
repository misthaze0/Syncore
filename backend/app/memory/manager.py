"""
长期记忆的管理器 —— 抽取、落库、注入 Prompt 的唯一入口

★ 三条设计约束

1. **记忆层是增益，不是依赖**。它挂掉时正确表现是「Agent 不认识你」，
   而不是让问答报错。所以这里所有对外方法都不抛异常。

2. **注入的 Prompt 块必须带「以本次请求为准」的出口**。
   偏好是**默认值**不是**硬约束** —— 用户说过「报告用 PPT」，
   但这次明确要 Markdown 时，模型必须听这一次的。
   没有这个出口，一条过期的偏好会把之后所有回答都带偏，而且用户很难察觉原因。

3. **注入要有硬上限**（条数 + 字符数）。记忆会随时间增长，
   不设上限的话，某一天它会悄悄挤掉真正有用的检索上下文。

★ 后台抽取的 fire-and-forget 需要注意

`asyncio.create_task` 返回的 Task 必须**保存强引用**，否则它可能被 GC 回收、
任务中途消失且毫无提示。这里用一个模块级 set 持有，并在 done 回调里移除。
同时用信号量限制并发，避免用户连发几条消息时同时打出多个模型请求。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from app.config import settings
from app.memory import extractor
from app.memory.models import (
    CATEGORIES,
    RELATIONSHIP_CATEGORIES,
    RELATIONSHIP_KEYS,
    MemoryCandidate,
    MemoryItem,
    relationship_label,
)
from app.memory.store import MemoryStore, UpsertResult, get_store
from app.memory.retention import cue_score, memory_state, retention, utc_now

logger = logging.getLogger(__name__)

# 后台抽取任务的强引用（不持有的话 Task 可能被 GC 掉，且不会有任何报错）
_background_tasks: Set[asyncio.Task] = set()
# 限制同时进行的抽取请求数：用户连发消息时不该同时打出一串模型调用
_extract_semaphore: asyncio.Semaphore | None = None

INJECT_HEADER = "【用户长期偏好】"
INJECT_FOOTER = (
    "以上是用户的长期偏好，请默认遵循；"
    "但若与本次请求的明确要求冲突，**以本次请求为准**。"
)
OWNER_BLOCK_HEADER = "【关于用户】"

#: 不随 MEMORY_TTL_DAYS 过期的事实：生日/纪念日这类日期没有「太久没提就失效」的道理
_TTL_EXEMPT_KEYS = frozenset({"birthday", "important_dates"})
_FOCUS_KEY = "current_focus"


def _get_semaphore() -> asyncio.Semaphore:
    """惰性构造信号量：Semaphore 必须在事件循环里创建"""
    global _extract_semaphore
    if _extract_semaphore is None:
        _extract_semaphore = asyncio.Semaphore(2)
    return _extract_semaphore


class MemoryManager:
    """长期记忆的读写与注入"""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self._store = store or get_store()

    @property
    def store(self) -> MemoryStore:
        return self._store

    # ---------- 读 ----------
    def list_items(self) -> List[MemoryItem]:
        return self._store.list_items()

    def stats(self) -> Dict[str, Any]:
        info = self._store.stats()
        info["enabled"] = bool(settings.MEMORY_ENABLED)
        info["extract_enabled"] = bool(settings.MEMORY_EXTRACT_ENABLED)
        info["store_path"] = str(self._store.path)
        info["max_items"] = settings.MEMORY_MAX_ITEMS
        info["max_chars"] = settings.MEMORY_INJECT_MAX_CHARS
        info["ttl_days"] = int(settings.MEMORY_TTL_DAYS)
        info["owner_items"] = sum(
            info.get("by_category", {}).get(c, 0) for c in RELATIONSHIP_CATEGORIES
        )
        info["owner_max_items"] = settings.MEMORY_OWNER_MAX_ITEMS
        info["owner_max_chars"] = settings.MEMORY_OWNER_MAX_CHARS
        info["decay_enabled"] = settings.MEMORY_DECAY_ENABLED
        info["recall_threshold"] = settings.MEMORY_RECALL_THRESHOLD
        states: Dict[str, int] = {}
        for item in self.list_items():
            state = memory_state(item)
            states[state] = states.get(state, 0) + 1
        info["by_state"] = states
        return info

    @staticmethod
    def item_payload(item: MemoryItem) -> Dict[str, Any]:
        return {**item.to_json(), "retention": retention(item), "memory_state": memory_state(item)}

    def _recallable(self, items: List[MemoryItem], query: str = "") -> List[MemoryItem]:
        if not settings.MEMORY_DECAY_ENABLED:
            return self._drop_expired(items)
        now = utc_now()
        # 沉睡条目只在有相关线索时作为待确认的旧记录出现。绝不唤醒被否定的条目。
        return [item for item in items if item.active and (
            retention(item, now) >= settings.MEMORY_RECALL_THRESHOLD or cue_score(item, query) > 0
        )]

    @staticmethod
    def _recall_value(item: MemoryItem) -> str:
        if memory_state(item) == "dormant":
            return f"较久未确认的旧记录（请先向用户确认是否仍适用）：{item.value}"
        return item.value

    def build_prompt_block(self, query: str = "", *, shareable_only: bool = False) -> str:
        """
        构造注入 System Prompt 的偏好块。

        关系记忆**不在这里**：它讲的是「用户是谁」，由 `build_owner_block`
        注入到人设的动态块里。混进偏好块会让模型把「生日」当成工作约定。
        """
        if not settings.MEMORY_ENABLED:
            return ""
        try:
            items = self._store.list_items(active_only=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取长期记忆失败（本次不注入）: %s", e)
            return ""
        items = [i for i in items if i.category not in RELATIONSHIP_CATEGORIES]
        if shareable_only:
            items = [i for i in items if i.category in {"output_format", "language", "tone", "length", "tooling"}]
        if not items:
            return ""

        items = self._recallable(items, query)
        if not items:
            return ""

        # 相关性优先，再看可及性与可信度；单纯注入不会让全部记忆永生。
        items = sorted(items, key=lambda i: (cue_score(i, query), retention(i) * i.confidence, i.updated_at), reverse=True)[
            : settings.MEMORY_MAX_ITEMS
        ]

        lines: List[str] = []
        used = 0
        for item in items:
            line = f"- {self._recall_value(item)}"
            if used + len(line) > settings.MEMORY_INJECT_MAX_CHARS:
                logger.debug("记忆注入达到字符上限，截断剩余 %s 条", len(items) - len(lines))
                break
            lines.append(line)
            used += len(line) + 1

        if not lines:
            return ""
        return "\n\n".join([INJECT_HEADER, *lines, INJECT_FOOTER])

    # ---------- 关系记忆（「她记得你」） ----------
    def owner_items(self, query: str = "") -> List[MemoryItem]:
        """全部有效的关系/生活类事实（未做条数截断，控制台与预览共用）"""
        try:
            items = self._store.list_items(active_only=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取关系记忆失败（本次不注入）: %s", e)
            return []
        items = [i for i in items if i.category in RELATIONSHIP_CATEGORIES]
        return self._recallable(items, query)

    def owner_profile_lines(self, *, skip_address: bool = False, query: str = "") -> List[str]:
        """
        「关于用户」的注入行（条数与字符数都有硬上限）。

        `skip_address=True` 用于人设卡里已经写了「称呼用户为 X」的时候 ——
        同一件事只该说一遍，重复注入会让模型以为有两种叫法。
        """
        if not settings.MEMORY_ENABLED:
            return []
        items = self.owner_items(query)
        if skip_address:
            items = [i for i in items if not i.key.startswith("address")]

        def order(item: MemoryItem) -> tuple[int, int, float, str]:
            base = item.key.removesuffix("_avoid")
            rank = RELATIONSHIP_KEYS.index(base) if base in RELATIONSHIP_KEYS else len(RELATIONSHIP_KEYS)
            # 否定式排在肯定式之后（「不要叫我小明」先让位给「称呼：老王」）
            return (rank, 0 if item.key.endswith("_avoid") else 1, -item.confidence, item.updated_at)

        items = sorted(items, key=order)

        lines: List[str] = []
        used = 0
        for item in items:
            line = self._owner_line(item)
            if not line:
                continue
            if len(lines) >= settings.MEMORY_OWNER_MAX_ITEMS:
                break
            if used + len(line) > settings.MEMORY_OWNER_MAX_CHARS:
                logger.debug("关系记忆注入达到字符上限，截断剩余条目")
                break
            lines.append(line)
            used += len(line) + 1
        return lines

    @staticmethod
    def _owner_line(item: MemoryItem) -> str:
        """一条关系记忆压成「标签：值」；值本身已含标签时不重复"""
        base = item.key.removesuffix("_avoid")
        label = relationship_label(base)
        value = (item.value or "").strip()
        if not value:
            return ""
        if memory_state(item) == "dormant":
            return f"{label}：{MemoryManager._recall_value(item)}"
        negative = item.key.endswith("_avoid")
        if not negative and label in value:
            return value
        return f"{label}：{value}"

    def build_owner_block(self, *, skip_address: bool = False) -> str:
        """关系记忆的完整注入块（API 预览用；人设动态块只消费其中的行）"""
        lines = self.owner_profile_lines(skip_address=skip_address)
        if not lines:
            return ""
        return "\n".join([OWNER_BLOCK_HEADER, *[f"- {line}" for line in lines]])

    def clear_owner(self) -> int:
        """清空全部关系记忆（「她记得你」卡的清空按钮）"""
        return self._store.clear_categories(set(RELATIONSHIP_CATEGORIES))

    def _drop_expired(self, items: List[MemoryItem]) -> List[MemoryItem]:
        """
        时效过滤（Zep 式 temporal validity 的务实版）：
        超过 `MEMORY_TTL_DAYS` 未再确认的偏好不再注入。

        四个刻意的取舍：
        - **非破坏性**：只影响注入，数据保留在文件里（控制台可见、可复活）；
        - **identity 豁免**：「我叫什么」不会因为久未提起就失效；
        - **关系记忆豁免**：称呼/生日/作息这类事实不会「过期」，只有「最近在忙」
          按 `MEMORY_FOCUS_TTL_DAYS` 单独老化；
        - **TTL=0 关闭**：解析时间戳失败视为未过期（坏时间戳不该吞掉一条真实偏好）。
        """
        ttl_days = int(settings.MEMORY_TTL_DAYS)
        if ttl_days <= 0:
            return items
        now = datetime.now(timezone.utc)
        kept: List[MemoryItem] = []
        for item in items:
            base = item.key.removesuffix("_avoid")
            if item.category == "identity":
                kept.append(item)
                continue
            if item.category in RELATIONSHIP_CATEGORIES and base != _FOCUS_KEY:
                kept.append(item)
                continue
            if base in _TTL_EXEMPT_KEYS:
                kept.append(item)
                continue
            limit_days = (
                int(settings.MEMORY_FOCUS_TTL_DAYS) if base == _FOCUS_KEY else ttl_days
            )
            try:
                updated = datetime.fromisoformat(item.updated_at)
                age_days = (now - updated).days
            except (TypeError, ValueError):
                kept.append(item)
                continue
            if age_days > limit_days:
                logger.debug("记忆已过期不注入（key=%s，%s 天未确认）", item.key, age_days)
                continue
            kept.append(item)
        return kept

    # ---------- 写：手动 ----------
    def add_manual(
        self,
        value: str,
        key: str = "",
        category: str = "",
        confidence: float = 1.0,
        evidence: str = "",
        pinned: bool | None = None,
    ) -> UpsertResult | None:
        """
        手动新增 / 覆盖一条偏好（控制台与 `memory_remember` 工具用）。

        与自动抽取的区别：**这里由调用方指定 key**，所以可以并存多条同类偏好。
        自动抽取按类别去重是为了避免矛盾，手动添加时用户自己承担这个责任。

        `evidence` 是「用户原话」。控制台手敲一条偏好时确实没有原话可存，
        留空就写「（手动添加）」；而工具那条路是用户在对话里说的，
        不存下来的话，用户在界面上看到这条记忆时就无法判断系统是不是理解错了
        （`models.py` 里 evidence 存在的意义正在这里）。
        """
        value = (value or "").strip()
        if not value:
            return None
        category = category if category in CATEGORIES else extractor.derive_category(value)
        key = (key or "").strip() or category
        item = MemoryItem(
            key=key,
            value=value,
            category=category,
            confidence=max(0.0, min(1.0, float(confidence))),
            evidence=(evidence or "").strip() or "（手动添加）",
            source="manual",
        )
        return self._store.upsert(item, pinned=pinned)

    def delete(self, key: str) -> bool:
        return self._store.delete(key)

    def forget_by_user(self, key: str, reason: str = "用户要求忘记") -> str:
        """
        用户亲口要求「忘掉某条偏好」时的失效操作。返回状态码，由工具层组织文案：
        `forgotten` / `not_found` / `already_inactive` / `failed` / `disabled`。

        ★ 为什么这里允许动 manual 条目
        `store.deactivate` 默认拒绝手动条目，保护的是**没有人要求改动**的那些路径
        （自动判定的否定可能判错）。而用户在对话里说「忘掉它」时，改动就是他的意愿 ——
        此时再按来源拒绝，界面只能说「这条删不掉」，可它明明是能删的。

        失效仍然是软删除（`active=False`，控制台可见、可复活），不是抹除。
        """
        if not settings.MEMORY_ENABLED:
            return "disabled"
        existing = self._store.get(key)
        if existing is None:
            return "not_found"
        if not existing.active:
            return "already_inactive"
        return "forgotten" if self._store.deactivate(
            existing.key, reason=reason, allow_manual=True
        ) else "failed"

    def update_item(
        self,
        key: str,
        value: str,
        category: str = "",
        confidence: float | None = None,
        pinned: bool | None = None,
    ) -> UpsertResult | None:
        """
        编辑一条已有记忆（控制台「编辑」的后端）。

        ★ 三条边界：
        - **key 与来源保持不变**：编辑是「修正措辞」，不是「换一条」——
          key 变了就等于删旧建新，历史留痕（superseded）会断；
          来源变了会触发 upsert 的 protected 拦截（手动条目仍是 manual）。
        - **旧值自动进 superseded**：直接复用 upsert 的历史留痕，
          「这条偏好被人工改过」同样可追溯。
        - **条目不存在或 value 为空返回 None**：让 API 层映射成 404 / 400。
        """
        value = (value or "").strip()
        if not value:
            return None
        existing = self._store.get(key)
        if existing is None:
            return None
        item = MemoryItem(
            key=existing.key,
            value=value,
            category=category if category in CATEGORIES else existing.category,
            confidence=existing.confidence if confidence is None else max(0.0, min(1.0, float(confidence))),
            evidence=existing.evidence,
            source=existing.source,
            thread_id=existing.thread_id,
        )
        return self._store.upsert(item, pinned=pinned, confirm=pinned is None)

    def reinforce_item(self, key: str) -> bool:
        if not settings.MEMORY_ENABLED:
            return False
        return self._store.reinforce(key)

    def reactivate_item(self, key: str) -> bool:
        """复活一条已失效的记忆（用户在控制台点「恢复」）"""
        return self._store.reactivate(key)

    def clear(self) -> int:
        return self._store.clear()

    # ---------- 写：显式快路径（同步、零延迟） ----------
    def remember_explicit(self, user_input: str, thread_id: str = "") -> List[MemoryItem]:
        """
        处理「记住：X」这类显式指令。

        同步、纯 CPU，可在回答用户之前调用，从而**立刻**给出
        「已记住：X」的反馈 —— 这是显式路径存在的意义。
        """
        if not settings.MEMORY_ENABLED or not user_input:
            return []
        try:
            candidates = extractor.extract_explicit(user_input)
        except Exception as e:  # noqa: BLE001
            logger.warning("显式记忆抽取失败: %s", e)
            return []

        saved: List[MemoryItem] = []
        for cand in candidates:
            # 写入前先做矛盾检测：新偏好与现有条目构成直接否定时，旧条目失效。
            # 例如先「记住：报告用PPT」，后「我不要报告用PPT」——
            # 两条若同时注入就是一对互相矛盾的指令，模型输出会变得不可预测。
            try:
                saved.extend(self._deactivate_contradictions(cand))
            except Exception as e:  # noqa: BLE001
                logger.warning("显式记忆矛盾检测失败（key=%s）: %s", cand.key, e)
            try:
                result = self._store.upsert(cand.to_item(thread_id))
            except Exception as e:  # noqa: BLE001
                logger.warning("写入记忆失败（key=%s）: %s", cand.key, e)
                continue
            if result.changed:
                saved.append(result.item)
        return saved

    # 显式否定式的统一前缀（extractor._TRIGGERS 的 negative=True 分支产出）
    _NEG_PREFIX = "不要"
    _AVOID_SUFFIX = "_avoid"

    def _deactivate_contradictions(self, cand: MemoryCandidate) -> List[MemoryItem]:
        """
        显式路径的规则化冲突解决（Mem0 DELETE 判定的确定性版本）。

        显式抽取的否定式有固定形态：value = 「不要X」，key = 「{category}_avoid」。
        与现有条目构成**直接否定**当且仅当去掉前缀后与对方值完全相等 ——
        完全相等才能保证是同一件事的表态反转；子串匹配会误伤
        （「不要简洁」不该否定「简洁但不生硬」这类更精细的偏好）。

        双向对称：
        - 新 avoid 候选「不要X」 → 现有正向条目（key=X 的类别键）值为 X → 失效
        - 新正向候选 X → 现有 avoid 条目值为「不要X」 → 失效

        手动条目由 store.deactivate 拒绝，天然受保护。
        """
        deactivated: List[MemoryItem] = []
        if cand.key.endswith(self._AVOID_SUFFIX):
            base_key = cand.key[: -len(self._AVOID_SUFFIX)]
            base_value = cand.value.removeprefix(self._NEG_PREFIX)
            if base_value and base_value != cand.value:
                existing = self._store.get(base_key)
                if (
                    existing is not None
                    and existing.active
                    and existing.value.strip() == base_value
                    and self._store.deactivate(base_key, reason=f"用户改口：{cand.value}")
                ):
                    deactivated.append(existing)
        else:
            avoid_key = f"{cand.key}{self._AVOID_SUFFIX}"
            existing = self._store.get(avoid_key)
            if (
                existing is not None
                and existing.active
                and existing.value.strip() == f"{self._NEG_PREFIX}{cand.value.strip()}"
                and self._store.deactivate(avoid_key, reason=f"用户改口：{cand.value}")
            ):
                deactivated.append(existing)
        return deactivated

    # ---------- 写：被动抽取（后台，fire-and-forget） ----------
    def schedule_inferred(self, user_input: str, answer: str, thread_id: str = "", *, turn_id: str = "") -> None:
        """
        安排一次后台被动抽取。

        **不阻塞 SSE 流关闭**：答案已经完整推给用户了，
        不该为了学一条偏好让用户多等几秒（前端会一直显示「生成中」）。
        学到的东西在下一次提问时自然生效（注入 Prompt），
        也可以在控制台的记忆面板里立刻看到。
        """
        if not settings.MEMORY_ENABLED or not settings.MEMORY_EXTRACT_ENABLED:
            return
        if not (user_input or "").strip() or not (answer or "").strip():
            return
        # ⚠️ 必须先确认有运行中的事件循环，再构造协程。
        # 反过来写（先 create_task 再 except RuntimeError）会留下一个
        # 「创建了但从未 await」的协程对象 —— 表现为一条
        # `RuntimeWarning: coroutine ... was never awaited`。
        # 这类警告最麻烦的地方不是它自己，而是它会淹没日志里真正的问题。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("无法创建后台记忆任务（无运行中的事件循环）")
            return
        task = asyncio.create_task(
            self._inferred_worker(user_input, answer, thread_id, turn_id=turn_id),
            name="xjms-memory-extract",
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _inferred_worker(self, user_input: str, answer: str, thread_id: str, *, turn_id: str = "") -> None:
        async with _get_semaphore():
            try:
                # Mem0 式判定需要现有记忆作对照：只给活跃条目（失效条目不再参与判定，
                # 否则「否定过又被翻出来对照」会让模型输出混乱的 delete）
                existing = self._store.list_items(active_only=True)
                # 先给与本轮相关的记忆，避免近期无关条目挤掉沉睡的旧记录。
                existing.sort(key=lambda item: cue_score(item, user_input), reverse=True)
                candidates = await asyncio.wait_for(
                    extractor.extract_inferred(user_input, answer, existing=existing),
                    timeout=settings.MEMORY_EXTRACT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.info(
                    "记忆被动抽取超时（%.0fs），本轮不学习",
                    settings.MEMORY_EXTRACT_TIMEOUT_SECONDS,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("记忆被动抽取失败: %s", e)
                return

            changed = 0
            revised_keys = {cand.target_key or cand.key for cand in candidates if cand.action in {"update", "delete"}}
            for cand in candidates:
                try:
                    if cand.action == "reinforce":
                        if (cand.target_key or cand.key) in revised_keys:
                            continue
                        # 强化必须有用户原话，不能靠助手反复念自己的提示词刷强度。
                        if len(cand.evidence.strip()) < 2 or cand.evidence.strip() not in user_input:
                            continue
                        target = next((item for item in existing[:extractor.JUDGE_CONTEXT_MAX_ITEMS]
                                       if item.key == (cand.target_key or cand.key)
                                       and item.value.strip() == cand.value.strip()), None)
                        if target is None or cand.confidence < 0.6 or cue_score(target, user_input) <= 0:
                            continue
                        changed += self._store.reinforce(
                            target.key, expected_value=target.value,
                            event_id=f"{thread_id}:{turn_id}" if turn_id else "",
                        )
                        continue
                    changed += self._apply_candidate(cand, thread_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("应用推断记忆失败（key=%s action=%s）: %s", cand.key, cand.action, e)
            if changed:
                logger.info("记忆被动抽取：%s 条记忆发生变更", changed)

    def _apply_candidate(self, cand: MemoryCandidate, thread_id: str) -> int:
        """
        按判定动作落库，返回发生变更的条数（0 或 1）。

        - add / update → upsert（update 的 key 已在解析阶段让位给 target_key）
        - delete → 软删除 target（手动条目由 store 拒绝；扑空记 debug 不算失败）
        - noop → 跳过；reinforce 只允许在带有用户原话校验的 worker 中处理
        """
        if cand.action == "delete":
            if self._store.deactivate(cand.target_key or cand.key, reason=f"推断判定：{cand.evidence[:60]}"):
                logger.info("记忆判定 delete：失效 %s", cand.target_key or cand.key)
                return 1
            logger.debug("记忆判定 delete 扑空（key=%s）", cand.target_key or cand.key)
            return 0
        if cand.action in {"noop", "reinforce"}:
            return 0
        result = self._store.upsert(cand.to_item(thread_id))
        return 1 if result.changed else 0


_manager: MemoryManager | None = None


def get_manager() -> MemoryManager:
    global _manager
    if _manager is None:
        _manager = MemoryManager()
    return _manager


async def drain_background_tasks(timeout: float = 5.0) -> None:
    """等待后台抽取任务收尾（应用关闭 / 测试用）"""
    pending = [t for t in list(_background_tasks) if not t.done()]
    if not pending:
        return
    try:
        await asyncio.wait(pending, timeout=timeout)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "MemoryManager",
    "get_manager",
    "drain_background_tasks",
    "INJECT_HEADER",
    "OWNER_BLOCK_HEADER",
]
