"""
人设的管理器 —— 读取、更新、渲染的唯一入口

★ 为什么对外方法一律不抛异常

与记忆层同一条纪律（架构不变量 28）：人设是增益不是依赖。
它挂掉的正确表现是「助手回到默认人设」，而不是「问答 500」。
"""

from __future__ import annotations

import logging
from typing import Any

from app.persona.models import DEFAULT_PERSONA, PersonaProfile, TONE_CUSTOM, TONE_PRESETS
from app.persona.store import PersonaStore, get_store

logger = logging.getLogger(__name__)


class PersonaManager:
    """人设的读写与 Prompt 渲染"""

    def __init__(self, store: PersonaStore | None = None) -> None:
        self._store = store or get_store()

    @property
    def store(self) -> PersonaStore:
        return self._store

    # ---------- 读 ----------
    def get_profile(self) -> PersonaProfile:
        try:
            return self._store.load()
        except Exception as e:  # noqa: BLE001
            logger.warning("读取人设失败，按默认人设继续: %s", e)
            return PersonaProfile.from_json(DEFAULT_PERSONA.to_json())

    def build_prompt_parts(
        self, thread_id: str = "", *, for_checkin: bool = False, query: str = ""
    ) -> tuple[str, str]:
        """
        渲染 (稳定块, 动态块)。

        稳定块描述「我是谁」，动态块放当前时间与关系记忆 —— 分开返回是为了让
        子 Agent 只吃稳定块：它们的 system_prompt 在工具循环里会被反复重建，
        带上「此刻」和关系记忆既浪费 token，也会让同一轮里的多个规划调用前缀不一致。
        """
        try:
            profile = self.get_profile()
            if not profile.enabled:
                return "", ""
            if not profile.apply_globally:
                # 延迟导入：persona 是底层数据模块，不在模块顶部依赖会话命名空间
                from app.conversations.store import is_assistant_thread_id

                if not is_assistant_thread_id(thread_id):
                    return "", ""
            stable = profile.build_stable_fragment()
            relationship_lines = []
            try:
                from app.memory.manager import get_manager

                # 人设卡里写了称呼就不再从关系记忆里重复取
                relationship_lines = get_manager().owner_profile_lines(
                    skip_address=bool(profile.address_me), **({"query": query} if query else {})
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("读取关系记忆失败（本次不注入）: %s", e)
            dynamic = profile.build_dynamic_fragment(
                relationship_lines=relationship_lines, for_checkin=for_checkin
            )
            return stable, dynamic
        except Exception as e:  # noqa: BLE001
            logger.warning("渲染人设块失败（本次不注入）: %s", e)
            return "", ""

    def build_prompt_fragment(
        self,
        thread_id: str = "",
        *,
        include_dynamic: bool = True,
        for_checkin: bool = False,
        query: str = "",
    ) -> str:
        """
        渲染注入 System Prompt 的人设块。

        ★ 作用范围：apply_globally=False 时只注入助理命名空间（assistant_*）。
        普通对话拿不到人设 —— 它描述的是「那位助理是谁」，默认不该改写
        所有对话的语气；用户显式打开全局生效后才扩展出去。

        关闭 / 不在范围 / 失败 → 空字符串，调用方直接判空拼接即可。
        """
        stable, dynamic = self.build_prompt_parts(thread_id, for_checkin=for_checkin, query=query)
        parts = [stable, dynamic if include_dynamic else ""]
        return "\n\n".join(part for part in parts if part)

    def stats(self) -> dict[str, Any]:
        profile = self.get_profile()
        return {
            "enabled": profile.enabled,
            "apply_globally": profile.apply_globally,
            "preset": profile.preset,
            "proactive": profile.proactive,
            "time_awareness": profile.time_awareness,
            "store_path": str(self._store.path),
            "tone_presets": dict(TONE_PRESETS),
        }

    # ---------- 写 ----------
    def update(self, **changes: Any) -> PersonaProfile:
        """
        局部更新人设。非法 tone 会被 PersonaProfile.__post_init__ 收敛回预设，
        调用方无需（也不该）自己写校验 —— 收敛逻辑在数据模型一处生效。
        """
        current = self.get_profile()
        updated = current.merged(**changes)
        try:
            return self._store.save(updated)
        except Exception as e:  # noqa: BLE001
            logger.warning("保存人设失败（本次修改只在内存生效）: %s", e)
            return updated

    def reset(self) -> PersonaProfile:
        """恢复默认人设（覆盖写盘）"""
        fresh = PersonaProfile.from_json(DEFAULT_PERSONA.to_json())
        try:
            return self._store.save(fresh)
        except Exception as e:  # noqa: BLE001
            logger.warning("恢复默认人设失败: %s", e)
            return fresh

    # 供 API 层校验语气取值用
    @staticmethod
    def valid_tones() -> dict[str, str]:
        return {**TONE_PRESETS, TONE_CUSTOM: "自定义（用 tone_custom 文本）"}


_manager: PersonaManager | None = None


def get_persona_manager() -> PersonaManager:
    global _manager
    if _manager is None:
        _manager = PersonaManager()
    return _manager


__all__ = ["PersonaManager", "get_persona_manager"]
