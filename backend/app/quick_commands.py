"""
快捷指令 —— 把「会问的人的问法」变成随时可取的素材。

★ 与工作流的分工

工作流是**自动执行的一串步骤**（节点、分支、工具），快捷指令只是
**输入框里的一段话**：选中即插入，用户还能接着改、带附件、补上下文。
管理入口可以相邻，内容 store 不共用 —— 否则用户会以为存一条指令
就等于编排了一个流程。

★ 只落用户自建的那几条

内置指令是产品内容（跟代码走、随版本更新）；用户自建的才需要备份与 diff。
与记忆 / 人设同一套取舍：文件损坏 → 回退到纯内置，增益不是依赖。

★ `{{...}}` 占位原样保留

选中后整段填进输入框，由用户自己替换。不做弹窗式填表：
占位通常就一两个，弹一个表单的成本（实现 + 理解）远高于「填进去改两个词」。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

MAX_TITLE = 60
MAX_BODY = 4000
MAX_CATEGORY = 30
MAX_COMMANDS = 200

#: 用户指令的 id 形如 `u-<12 位十六进制>`；磁盘上不符合的一律丢弃
_USER_ID_RE = re.compile(r"^u-[0-9a-f]{12}$")


@dataclass
class QuickCommand:
    """一条可插入输入框的问法"""

    id: str
    title: str
    body: str
    category: str = "通用"
    builtin: bool = False
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


#: 内置指令 —— 从旧提示词模板里挑出**与工作流不重复**的问法。
#: 「跑一个命令」没有收：它把用户往 shell 上引，而那是危险操作的入口，
#: 不该被包装成一条点一下就能用的快捷指令。
_BUILTIN_COMMANDS: List[QuickCommand] = [
    QuickCommand(
        id="builtin-qa-summary",
        title="总结知识库文档",
        body="帮我总结一下知识库里关于 {{主题}} 的核心结论，按要点列出。",
        category="知识问答",
        builtin=True,
    ),
    QuickCommand(
        id="builtin-multi-hop",
        title="跨文档找答案",
        body="{{问题}} 的答案散在几份文档里，请把它们拼起来，并说明每条信息来自哪里。",
        category="知识问答",
        builtin=True,
    ),
    QuickCommand(
        id="builtin-attachment",
        title="解读我上传的文件",
        body="读一下我发的附件，{{想让我做什么，例如：列出其中的关键数据 / 挑出风险点}}。",
        category="文件",
        builtin=True,
    ),
    QuickCommand(
        id="builtin-report",
        title="写一份报告",
        body="根据我们刚才聊到的内容，写一份 {{主题}} 报告，用 Markdown 表格呈现关键数据，生成后告诉我文件在哪。",
        category="写作",
        builtin=True,
    ),
    QuickCommand(
        id="builtin-remember",
        title="让助手记住偏好",
        body="记住：{{以后都照这个来的一句偏好，例如：写报告都用 Markdown 表格}}。",
        category="记忆",
        builtin=True,
    ),
]

_BUILTIN_BY_ID = {c.id: c for c in _BUILTIN_COMMANDS}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class QuickCommandStore:
    """用户自建指令的文件存储；内置指令常驻内存，不进文件。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._commands: Optional[List[QuickCommand]] = None

    # ---------- 读取 / 落盘 ----------

    def _load(self) -> List[QuickCommand]:
        if self._commands is not None:
            return self._commands

        path = settings.quick_commands_store_path
        commands: List[QuickCommand] = []
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = data.get("commands") if isinstance(data, dict) else None
                for item in raw or []:
                    command = self._from_dict(item)
                    if command is not None:
                        commands.append(command)
            except (OSError, json.JSONDecodeError) as e:
                # 文件损坏 → 回退到纯内置。指令是增益不是依赖。
                logger.warning("快捷指令文件读取失败，回退内置指令: %s", e)
                commands = []

        self._commands = commands[:MAX_COMMANDS]
        return self._commands

    @staticmethod
    def _from_dict(item: Any) -> Optional[QuickCommand]:
        """磁盘内容不可信：手工改坏的记录直接丢弃，不让它进输入框菜单。"""
        if not isinstance(item, dict):
            return None
        command_id = str(item.get("id") or "")
        if not _USER_ID_RE.match(command_id):
            return None
        title = str(item.get("title") or "").strip()[:MAX_TITLE]
        body = str(item.get("body") or "").strip()[:MAX_BODY]
        if not title or not body:
            return None
        category = str(item.get("category") or "").strip()[:MAX_CATEGORY] or "通用"
        return QuickCommand(
            id=command_id,
            title=title,
            body=body,
            category=category,
            builtin=False,
            created_at=str(item.get("created_at") or ""),
        )

    def _save(self) -> None:
        assert self._commands is not None
        path = settings.quick_commands_store_path
        payload = {"commands": [c.to_dict() for c in self._commands]}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ---------- 对外 API ----------

    def list(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = [*_BUILTIN_COMMANDS, *self._load()]
        if category:
            items = [c for c in items if c.category == category]
        return [c.to_dict() for c in items]

    def categories(self) -> List[str]:
        with self._lock:
            items = [*_BUILTIN_COMMANDS, *self._load()]
        seen: List[str] = []
        for command in items:
            if command.category not in seen:
                seen.append(command.category)
        return seen

    def create(self, title: str, body: str, category: str = "通用") -> Dict[str, Any]:
        title = (title or "").strip()
        body = (body or "").strip()
        if not title or not body:
            raise ValueError("标题与内容都不能为空")
        if len(title) > MAX_TITLE:
            raise ValueError(f"标题不能超过 {MAX_TITLE} 个字符")
        if len(body) > MAX_BODY:
            raise ValueError(f"内容不能超过 {MAX_BODY} 个字符")

        command = QuickCommand(
            id=f"u-{uuid.uuid4().hex[:12]}",
            title=title,
            body=body,
            category=(category or "").strip()[:MAX_CATEGORY] or "通用",
            builtin=False,
            created_at=_now_iso(),
        )
        with self._lock:
            commands = self._load()
            if len(commands) >= MAX_COMMANDS:
                raise ValueError(f"自建指令数量已达上限（{MAX_COMMANDS} 条）")
            commands.append(command)
            self._save()
        return command.to_dict()

    def update(self, command_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        if command_id in _BUILTIN_BY_ID:
            raise PermissionError("内置指令不可修改 —— 复制一条成你自己的再改")
        with self._lock:
            commands = self._load()
            for index, command in enumerate(commands):
                if command.id != command_id:
                    continue
                if "title" in changes:
                    title = str(changes["title"] or "").strip()
                    if not title:
                        raise ValueError("标题不能为空")
                    if len(title) > MAX_TITLE:
                        raise ValueError(f"标题不能超过 {MAX_TITLE} 个字符")
                    command.title = title
                if "body" in changes:
                    body = str(changes["body"] or "").strip()
                    if not body:
                        raise ValueError("内容不能为空")
                    if len(body) > MAX_BODY:
                        raise ValueError(f"内容不能超过 {MAX_BODY} 个字符")
                    command.body = body
                if "category" in changes:
                    command.category = str(changes["category"] or "").strip()[:MAX_CATEGORY] or "通用"
                commands[index] = command
                self._save()
                return command.to_dict()
        raise KeyError(f"快捷指令不存在: {command_id}")

    def delete(self, command_id: str) -> Dict[str, Any]:
        if command_id in _BUILTIN_BY_ID:
            raise PermissionError("内置指令不可删除")
        with self._lock:
            commands = self._load()
            for index, command in enumerate(commands):
                if command.id == command_id:
                    commands.pop(index)
                    self._save()
                    return {"ok": True, "deleted": command_id}
        raise KeyError(f"快捷指令不存在: {command_id}")


_store: Optional[QuickCommandStore] = None
_store_lock = threading.Lock()


def get_quick_command_store() -> QuickCommandStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = QuickCommandStore()
        return _store
