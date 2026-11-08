"""助理专属能力（目前只有主动问候 checkin）"""

from app.assistant.checkin import CHECKIN_PROMPT, maybe_checkin

__all__ = ["CHECKIN_PROMPT", "maybe_checkin"]
