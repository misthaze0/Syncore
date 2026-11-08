"""
人设的存储层 —— 单文件 JSON + 原子写

★ 为什么照抄 memory/store.py 的形态而不是新发明一种

两者的约束完全同构：都是「跨请求存活、重启不能丢、损坏不能拖垮主流程」的
小体量用户数据。memory 那边已经用 stat 缓存 + 原子写 + 损坏兜底换来了
「可 git diff、可备份、坏掉也不致命」，这里直接沿用同一套，
差异只在数据形状（单对象 vs 条目字典）。

★ stat 缓存为什么必须有

`build_fragment()` 会在**每轮问答的每个子 Agent 规划**时被调
（runner 的 system_prompt 构造处）。不缓存的话，一次多步工具调用
就要反复读盘解析十几遍。stat 比 mtime+size 未变即复用 ——
控制台改了人设，文件变了，下一轮自然生效，缓存不会骗人。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from app.config import settings
from app.persona.models import DEFAULT_PERSONA, PersonaProfile
from app.persona.timeutil import now_iso

logger = logging.getLogger(__name__)

STORE_VERSION = 1


class PersonaStore:
    """人设的持久化（单文件 JSON + stat 缓存 + 原子写）"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._resolved: Path | None = None
        self._lock = threading.RLock()
        # stat 缓存：(mtime, size) → profile；未命中才重读
        self._cache_stamp: tuple[float, int] | None = None
        self._cache: PersonaProfile | None = None

    @property
    def path(self) -> Path:
        if self._resolved is None:
            self._resolved = self._path or settings.persona_store_path
        return self._resolved

    # ---------- 读写 ----------
    def load(self) -> PersonaProfile:
        """读人设。文件不存在 / 损坏 → 默认人设（不抛异常）"""
        path = self.path
        if not path.is_file():
            self._cache_stamp = None
            self._cache = None
            return PersonaProfile.from_json(DEFAULT_PERSONA.to_json())

        try:
            stat = path.stat()
        except OSError as e:
            logger.debug("人设文件 stat 失败: %s", e)
            return PersonaProfile.from_json(DEFAULT_PERSONA.to_json())

        stamp = (stat.st_mtime, stat.st_size)
        if self._cache_stamp == stamp and self._cache is not None:
            return self._cache

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            profile = PersonaProfile.from_json(data.get("profile") or {})
        except Exception as e:  # noqa: BLE001
            logger.warning("人设文件不可读，按默认人设继续（本次不覆盖磁盘内容）: %s", e)
            self._cache_stamp = None
            self._cache = None
            return PersonaProfile.from_json(DEFAULT_PERSONA.to_json())

        self._cache_stamp = stamp
        self._cache = profile
        return profile

    def save(self, profile: PersonaProfile) -> PersonaProfile:
        """原子写：临时文件 + os.replace，避免半截 JSON"""
        with self._lock:
            profile.updated_at = profile.updated_at or now_iso()
            payload = {
                "version": STORE_VERSION,
                "updated_at": profile.updated_at,
                "profile": profile.to_json(),
            }
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            try:
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, path)
                self._cache_stamp = None  # 强制下次重读（mtime 已变，重读一次即可）
            except Exception as e:  # noqa: BLE001
                logger.warning("写入人设文件失败（本次修改不会持久化）: %s", e)
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
            return profile

    def reload(self) -> None:
        """丢弃缓存（单测与外部改文件后用）"""
        with self._lock:
            self._cache_stamp = None
            self._cache = None


_store: PersonaStore | None = None


def get_store() -> PersonaStore:
    global _store
    if _store is None:
        _store = PersonaStore()
    return _store


__all__ = ["PersonaStore", "get_store", "STORE_VERSION"]
