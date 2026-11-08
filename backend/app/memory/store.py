"""
长期记忆的存储层 —— 文件是唯一事实来源

★ 为什么是文件而不是 Redis

Redis 在本项目里是**可降级依赖**（挂掉自动退化成 InMemorySaver，见架构不变量 14）。
如果把长期记忆放进 Redis，它就继承了「重启可能丢」的属性 ——
而「长期」记忆恰恰是最不能丢的那一类数据。放文件里换来三件事：
重启不丢、可以 git diff 看出来改了什么、坏掉时最差也只是「Agent 不认识你」。

代价是没有并发写入能力。本项目的写入口只有三个（显式快路径 / 后台抽取 / API），
单进程 uvicorn 下用一把进程内锁足够；真要多进程时该换的是存储而不是锁。

★ 为什么每次读都要 stat 一次而不是直接缓存

`list_items()` 会被每个请求调用（注入 Prompt 用），而一轮问答里
`run_planner_turn` 可能被调很多次（每次工具调用一轮）。
所以这里用「stat 比 mtime+size，没变就复用上次解析结果」的缓存，
而不是无脑读文件、也不是永久缓存 ——
永久缓存会让「用户在控制台删掉一条记忆」在下次重启前都不生效。

★ 损坏时的行为

文件不存在 → 空；解析失败 → 空 + 告警。**不抛异常**。
记忆层是增益不是依赖：它坏掉的正确表现是「Agent 不认识你」，
而不是「问答功能 500」。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from app.config import settings
from app.memory.models import MemoryItem, normalize_key, now_iso
from app.memory.retention import reinforce

logger = logging.getLogger(__name__)

STORE_VERSION = 2
# 每条约保留多少个被取代的历史值
MAX_SUPERSEDED = 5


@dataclass
class UpsertResult:
    """一次写入的结果。action 让调用方能区分「真的记住了新东西」和「重复学习」"""

    item: MemoryItem
    action: str  # created / updated / unchanged / protected

    @property
    def changed(self) -> bool:
        return self.action in {"created", "updated"}


class MemoryStore:
    """长期记忆的持久化（单文件 JSON + 原子写）"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        # 解析一次就记住：`path` 属性会被每次 Prompt 注入调用，
        # 而 `settings.memory_store_path` 每次都带一次 mkdir，别放进热路径
        self._resolved: Path | None = None
        # 读改写是复合操作，必须互斥；用可重入锁以便 future 的嵌套调用
        self._lock = threading.RLock()
        # 解析结果缓存：(mtime, size) → items
        self._cache_stamp: tuple[float, int] | None = None
        self._cache_items: Dict[str, Dict[str, Any]] = {}

    # ---------- 路径与原始读写 ----------
    @property
    def path(self) -> Path:
        if self._resolved is None:
            self._resolved = self._path or settings.memory_store_path
        return self._resolved

    def _empty(self) -> Dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "user_id": settings.MEMORY_USER_ID,
            "updated_at": "",
            "items": {},
        }

    def _read_raw(self) -> Dict[str, Any]:
        """读原始结构（带 stat 缓存）"""
        path = self.path
        if not path.is_file():
            self._cache_stamp = None
            self._cache_items = {}
            return self._empty()

        try:
            stat = path.stat()
        except OSError as e:
            logger.debug("记忆文件 stat 失败: %s", e)
            return self._empty()

        stamp = (stat.st_mtime, stat.st_size)
        if self._cache_stamp == stamp:
            return {"version": STORE_VERSION, "items": self._cache_items}

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("items")
            if not isinstance(items, dict):
                raise ValueError("items 字段不是对象")
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆文件不可读，按空记忆处理（本次不覆盖磁盘内容）: %s", e)
            self._cache_stamp = None
            self._cache_items = {}
            return self._empty()

        self._cache_stamp = stamp
        self._cache_items = items
        return {"version": int(data.get("version", STORE_VERSION)), "items": items}

    def _write_raw(self, items: Dict[str, Dict[str, Any]]) -> None:
        """原子写：临时文件 + os.replace，避免半截 JSON"""
        path = self.path
        payload = {
            "version": STORE_VERSION,
            "user_id": settings.MEMORY_USER_ID,
            "updated_at": now_iso(),
            "items": items,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            self._cache_stamp = None  # 强制下次重读
        except Exception as e:  # noqa: BLE001
            logger.warning("写入记忆文件失败（本次记忆不会持久化）: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 查询 ----------
    def list_items(self, active_only: bool = False) -> List[MemoryItem]:
        """
        全部记忆，按更新时间倒序（最新的在前）。

        `active_only=True` 时只返回未被否定的条目（注入 Prompt 用）；
        默认 False 返回全部 —— 控制台需要看到被否定的条目才能审计与恢复。
        """
        raw = self._read_raw()["items"]
        items: List[MemoryItem] = []
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                item = MemoryItem.from_json({**value, "key": value.get("key") or key})
            except Exception as e:  # noqa: BLE001
                logger.debug("跳过损坏的记忆条目 %s: %s", key, e)
                continue
            if active_only and not item.active:
                continue
            items.append(item)
        items.sort(key=lambda i: i.updated_at, reverse=True)
        return items

    def get(self, key: str) -> MemoryItem | None:
        nkey = normalize_key(key)
        raw = self._read_raw()["items"].get(nkey)
        if not isinstance(raw, dict):
            return None
        return MemoryItem.from_json({**raw, "key": nkey})

    def stats(self) -> Dict[str, Any]:
        items = self.list_items()
        by_category: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        for item in items:
            by_category[item.category] = by_category.get(item.category, 0) + 1
            by_source[item.source] = by_source.get(item.source, 0) + 1
        return {
            "total": len(items),
            "active": sum(1 for i in items if i.active),
            "inactive": sum(1 for i in items if not i.active),
            "by_category": by_category,
            "by_source": by_source,
            "updated_at": max((i.updated_at for i in items), default=""),
        }

    # ---------- 写入 ----------
    def upsert(self, item: MemoryItem, *, pinned: bool | None = None, confirm: bool = True) -> UpsertResult:
        """
        按 key 写入（同一 key 视为同一条偏好，新值取代旧值）。

        四种特殊情形在这里收口：
        - **内容完全相同**：只提升置信度与时间，不把同样的值塞进 superseded（否则历史被噪声填满）
        - **手动条目**：自动抽取不得覆盖用户亲手设的值（返回 protected）
        - **旧值不同**：旧值进 superseded 留痕，便于「我什么时候改的主意」
        - **旧条目已失效**：重新表达 = 复活（新值 active=True 天然覆盖软删除标记）
        """
        with self._lock:
            raw = self._read_raw()
            items = dict(raw["items"])
            existing_raw = items.get(item.key)
            existing = (
                MemoryItem.from_json({**existing_raw, "key": item.key})
                if isinstance(existing_raw, dict)
                else None
            )

            if existing is None:
                if pinned is not None:
                    item.pinned = pinned
                items[item.key] = item.to_json()
                self._write_raw(items)
                return UpsertResult(item=item, action="created")

            # 手动设定的值不允许被自动抽取悄悄改掉
            if existing.source == "manual" and item.source != "manual":
                return UpsertResult(item=existing, action="protected")

            same_value = existing.value.strip() == item.value.strip()
            item.created_at = existing.created_at
            item.confidence = max(existing.confidence, item.confidence)
            item.superseded = list(existing.superseded)
            item.pinned = existing.pinned if pinned is None else pinned

            if same_value:
                # 同一事实沿用学习轨迹；修正后的新事实则从新曲线开始。
                if existing.active:
                    item.stability_days = existing.stability_days
                    item.last_reinforced_at = existing.last_reinforced_at
                    item.reinforcement_count = existing.reinforcement_count
                    item.last_reinforced_event = existing.last_reinforced_event
                    if existing.source == "explicit" and item.source == "inferred":
                        item.source = existing.source
                    if confirm:
                        reinforce(item)
                items[item.key] = item.to_json()
                self._write_raw(items)
                return UpsertResult(item=item, action="unchanged" if existing.active else "updated")

            history = [existing.value] + [s for s in existing.superseded if s != existing.value]
            item.superseded = history[:MAX_SUPERSEDED]
            items[item.key] = item.to_json()
            self._write_raw(items)
            return UpsertResult(item=item, action="updated")

    def reinforce(self, key: str, *, event_id: str = "", expected_value: str | None = None) -> bool:
        """有效回忆/用户确认才调用；读取列表、预览和提示词构建不得调用。"""
        with self._lock:
            items = dict(self._read_raw()["items"])
            nkey = normalize_key(key)
            raw = items.get(nkey)
            if not isinstance(raw, dict):
                return False
            item = MemoryItem.from_json({**raw, "key": nkey})
            if expected_value is not None and item.value.strip() != expected_value.strip():
                return False  # 后台判定期间用户已经改口，不能强化旧事实。
            if not reinforce(item, event_id=event_id):
                return False
            items[nkey] = item.to_json()
            self._write_raw(items)
            return True

    def delete(self, key: str) -> bool:
        with self._lock:
            raw = self._read_raw()
            items = dict(raw["items"])
            if normalize_key(key) not in items:
                return False
            items.pop(normalize_key(key), None)
            self._write_raw(items)
            return True

    def deactivate(self, key: str, reason: str = "", *, allow_manual: bool = False) -> bool:
        """
        软删除：打 active=False 标记，数据保留（可审计、可复活）。

        与 `delete`（硬删，控制台/API 用）的分工：自动判定的否定走这里 ——
        模型的判断可能出错，硬删一条真实偏好无法挽回，标记失效永远可以恢复。

        手动条目拒绝自动否定（与 upsert 的 protected 同一原则）；
        目标不存在或已失效时返回 False，让调用方能区分「否定了什么」和「扑空」。

        ★ `allow_manual` 只给「用户亲口要求忘记」那一条路（`MemoryManager.forget_by_user`）
        受保护的从来不是「手动条目」这四个字，而是**没有人要求改动它**。
        用户说「忘掉我上次说的那个偏好」时，改动就是用户自己的意愿，
        此时按 source==manual 拒绝只会变成「你亲手设的东西你自己删不掉」，
        而界面还得假装是权限问题。自动抽取路径一律不传这个参数。
        """
        nkey = normalize_key(key)
        with self._lock:
            raw = self._read_raw()
            items = dict(raw["items"])
            existing_raw = items.get(nkey)
            if not isinstance(existing_raw, dict):
                return False
            try:
                existing = MemoryItem.from_json({**existing_raw, "key": nkey})
            except Exception as e:  # noqa: BLE001
                logger.warning("deactivate 读取条目失败（key=%s）: %s", nkey, e)
                return False
            if existing.source == "manual" and not allow_manual:
                return False
            if not existing.active:
                return False
            existing.active = False
            existing.inactive_reason = (reason or "").strip() or "被后续记忆否定"
            existing.updated_at = now_iso()
            items[nkey] = existing.to_json()
            self._write_raw(items)
            return True

    def reactivate(self, key: str) -> bool:
        """
        复活一条已失效的记忆（控制台「恢复」按钮的后端）。

        与 `deactivate` 的不对称是刻意的：否定**自动判定**要拦（模型可能出错），
        复活却是**用户亲手点的** —— 用户的显式动作不需要再被「保护」一遍。
        目标不存在或本来就有效时返回 False，让 API 能区分「恢复了」和「扑空」。
        """
        nkey = normalize_key(key)
        with self._lock:
            raw = self._read_raw()
            items = dict(raw["items"])
            existing_raw = items.get(nkey)
            if not isinstance(existing_raw, dict):
                return False
            try:
                existing = MemoryItem.from_json({**existing_raw, "key": nkey})
            except Exception as e:  # noqa: BLE001
                logger.warning("reactivate 读取条目失败（key=%s）: %s", nkey, e)
                return False
            if existing.active:
                return False
            existing.active = True
            existing.inactive_reason = ""
            existing.updated_at = now_iso()
            existing.last_reinforced_at = existing.updated_at
            existing.last_reinforced_event = ""
            items[nkey] = existing.to_json()
            self._write_raw(items)
            return True

    def clear(self) -> int:
        with self._lock:
            raw = self._read_raw()
            n = len(raw["items"])
            if n:
                self._write_raw({})
            return n

    def clear_categories(self, categories: set[str]) -> int:
        """
        只清空指定类别的记忆（关系记忆的「关于用户」卡用）。

        一次读 → 过滤 → 一次原子写，不做「逐条 delete」：逐条写盘除了慢，
        中途失败还会留下「清了一半」的状态，而用户点的是「全部清掉」。
        """
        if not categories:
            return 0
        with self._lock:
            raw = self._read_raw()
            items = dict(raw["items"])
            kept: Dict[str, Dict[str, Any]] = {}
            removed = 0
            for key, value in items.items():
                category = value.get("category") if isinstance(value, dict) else None
                if category in categories:
                    removed += 1
                    continue
                kept[key] = value
            if removed:
                self._write_raw(kept)
            return removed

    def reload(self) -> None:
        """丢弃缓存（单测与外部改文件后用）"""
        with self._lock:
            self._cache_stamp = None
            self._cache_items = {}


_store: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


__all__ = ["MemoryStore", "UpsertResult", "get_store", "STORE_VERSION"]
