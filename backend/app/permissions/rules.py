"""
allow / ask / deny 规则 —— 「这一类操作以后都别再问我」的持久表达

★ 为什么在预授权之外还要一套规则

`approval/grants.py` 的预授权回答的是「**这次会话**剩下的同类操作别烦我」，
它是会话态、带次数上限、重启即清空。规则回答的是另一句话：
「**这台机器上**以后读我工作目录里的文件都不用问」。两者不能互相替代：
把预授权做成永久等于把 HITL 关掉（那条模块注释已经说过），
而把规则做成会话态就没有解决「每天都要点二十次」。

★ 语法故意做得很小

一条规则 = `工具 [参数] 匹配` → 行为。三样东西，没有逻辑运算、没有正则。
理由不是偷懒：规则会改变**安全行为**，而写错一条正则的后果是
「本该拦住的操作被放行了」—— 那种错误静默无报，用户永远不知道。
`fnmatch` 的 `*` / `?` 已经能表达「以 git 开头」「下载目录里的任何文件」，
剩下的表达力交给 `danger_level` 默认值和权限模式档位。

★ 三条硬语义（`policy.py` 依赖它们）

1. **deny 永远赢**。allow 规则与 `auto` 模式都盖不过一条 deny ——
   否则「拉黑发邮件」这件事要看运气。
2. **ask 盖过放行，但盖不过 deny**。它是「这条我不管档位，一定要看一眼」。
3. **规则只改变「问不问」，不改变「能碰哪些目录」**。
   文件边界由权限模式 + `AGENT_ALLOWED_DIRS` 决定（见 `core/sandbox.py`）。
   一条 `allow read /etc/passwd` 不会给她开新的门，
   只会让本来就要问的那一下不再问 —— 这是规则最容易被误解的地方。

★ 匹配的是模型传进来的**原始**参数值

`pattern` 比对的是工具收到的字符串，不是解析后的绝对路径。
这不是漏洞而是边界：正因为放行不等于扩大可达目录，
拿 `..` 绕路去骗一条 allow 规则并不能真的多读到文件 —— 越界仍然会在
`resolve_agent_path` 那里被拦下。但**反过来**要记住：
`deny grep(*.env*)` 这类「用规则当黑名单」的写法是靠不住的，
路径写法太多。真正的黑名单应该由模式档位和目录边界来做。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

BEHAVIORS: Tuple[str, ...] = (DENY, ASK, ALLOW)

#: 界面用的中文标签（顺序=优先级：deny 在最前）
BEHAVIOR_LABELS: Dict[str, str] = {
    DENY: "禁止",
    ASK: "必须问我",
    ALLOW: "放行",
}

#: 行为 → 一句话说明。界面上直接展示，避免用户猜「ask 是不是拒绝」。
BEHAVIOR_HINTS: Dict[str, str] = {
    DENY: "不管用什么模式、谁批准过，这个操作都不会执行。",
    ASK: "即使当前模式是自动执行，这一步也会停下来问你。",
    ALLOW: "不再询问，直接执行（仍然受目录边界限制）。",
}

#: 参数留空时，按这个顺序挑一个「代表这次调用意图」的参数来匹配。
#: ★ 为什么要自动挑：让用户写规则时记住 `bash` 的参数叫 `command`、
#: `email_send` 的叫 `to` 是强人所难。她想说「git 开头的命令」，
#: 界面里只有一个匹配框，工具名已经给了，参数名由这里补。
PRIMARY_ARG_CANDIDATES: Tuple[str, ...] = (
    "command",
    "path",
    "file_path",
    "file",
    "url",
    "to",
    "query",
    "instruction",
    "title",
    "text",
    "content",
    "task_id",
    "event_id",
    "name",
)

#: 匹配值里的路径分隔符先归一，否则 Windows 上 `data/*` 这种写法永远匹配不上
_MAX_PATTERN_CHARS = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm_value(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").casefold()


def _primary_arg(args: Dict[str, Any]) -> Tuple[str, str]:
    """返回 (参数名, 值)。没有候选参数时取第一个字符串参数。"""
    for key in PRIMARY_ARG_CANDIDATES:
        if key in args:
            return key, str(args[key] or "")
    for key, value in args.items():
        if isinstance(value, str) and value.strip():
            return key, value
    return "", ""


@dataclass
class PermissionRule:
    """一条权限规则"""

    behavior: str
    tool: str
    #: 参数名；空 = 自动挑主要参数（只在 pattern 非空时才有意义）
    arg: str = ""
    #: fnmatch 通配；空 = 该工具的整个调用
    pattern: str = ""
    note: str = ""
    enabled: bool = True
    rule_id: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.rule_id:
            self.rule_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = _now_iso()

    # ---------- 判定 ----------
    def tool_hit(self, tool_name: str) -> bool:
        tool = (tool_name or "").casefold()
        pat = (self.tool or "").strip().casefold()
        if not pat or pat == "*":
            return True
        return fnmatch.fnmatch(tool, pat)

    def resolve_arg(self, args: Dict[str, Any]) -> str:
        """这条规则实际比对的是哪个参数"""
        if self.arg:
            return self.arg.strip()
        return _primary_arg(args)[0]

    def match(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        """
        命中则返回「命中说明」（给留痕和界面用），否则 None。

        说明里带上实际比对到的值 —— 排查「为什么这条没生效」时，
        最需要看见的就是那一个值。
        """
        if not self.enabled:
            return None
        if not self.tool_hit(tool_name):
            return None
        pat = (self.pattern or "").strip()
        if not pat:
            return f"{self.tool} 整个工具"
        arg_name = self.resolve_arg(args)
        if not arg_name:
            # 规则写了匹配条件，但这次调用没有任何可比对的参数 ——
            # 算**不命中**：宁可多问一次，也不要在无从判断时放行。
            return None
        value = str(args.get(arg_name, "") or "")
        if not value:
            return None
        if fnmatch.fnmatch(_norm_value(value), _norm_value(pat)):
            return f"{tool_name}.{arg_name} = {value[:60]} 命中 {pat}"
        # 前缀写法（`git status:*` 那一类习惯）也要能用：
        # fnmatch 里 `git*` 已经覆盖，但如果用户写的是裸前缀且没带 *，
        # 按「前缀」理解比按「全等」理解更符合直觉。
        return None

    # ---------- 对外表示 ----------
    def describe(self) -> str:
        """一句人话（界面与留痕共用）"""
        base = f"{BEHAVIOR_LABELS.get(self.behavior, self.behavior)} {self.tool}"
        pat = (self.pattern or "").strip()
        if not pat:
            return base
        arg = self.arg or "主参数"
        return f"{base}（{arg} 匹配 {pat}）"

    def to_dict(self) -> Dict[str, Any]:
        payload = self.to_storage()
        payload["description"] = self.describe()
        payload["behavior_label"] = BEHAVIOR_LABELS.get(self.behavior, self.behavior)
        payload["behavior_hint"] = BEHAVIOR_HINTS.get(self.behavior, "")
        return payload

    def to_storage(self) -> Dict[str, Any]:
        """只含**用户写下的**那些字段（出参里还要带派生说明，落盘时不要）"""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "PermissionRule":
        return cls(
            behavior=str(raw.get("behavior") or "").strip().lower(),
            tool=str(raw.get("tool") or "").strip(),
            arg=str(raw.get("arg") or "").strip(),
            pattern=str(raw.get("pattern") or "").strip(),
            note=str(raw.get("note") or "").strip(),
            enabled=bool(raw.get("enabled", True)),
            rule_id=str(raw.get("rule_id") or ""),
            created_at=str(raw.get("created_at") or ""),
        )


def validate(rule: PermissionRule) -> str | None:
    """返回错误文案；None 表示可以保存。"""
    if rule.behavior not in BEHAVIORS:
        return f"行为只能是 {' / '.join(BEHAVIORS)} 之一"
    if not rule.tool.strip():
        return "工具名不能为空（要覆盖全部工具请写 *）"
    if len(rule.tool) > 80:
        return "工具名太长"
    if len(rule.pattern) > _MAX_PATTERN_CHARS:
        return f"匹配条件最长 {_MAX_PATTERN_CHARS} 个字符"
    return None


class RuleStore:
    """规则表（内存快照 + 单文件落盘，唯一写入者见 `modes.py` 模块注释）"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rules: Optional[List[PermissionRule]] = None

    # ---------- 读写 ----------
    def _read(self) -> List[PermissionRule]:
        path = settings.permission_rules_path
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("权限规则读取失败，本次按空规则表处理: %s", e)
            return []
        items = raw.get("rules") if isinstance(raw, dict) else raw
        out: List[PermissionRule] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            rule = PermissionRule.from_dict(item)
            # 坏条目跳过而不是整份作废：一条手改错的规则不该让别的规则一起失效
            if validate(rule) is None:
                out.append(rule)
        return out

    def _write(self, rules: List[PermissionRule]) -> None:
        path = settings.permission_rules_path
        payload = {"version": 1, "rules": [r.to_storage() for r in rules]}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load(self, *, force: bool = False) -> List[PermissionRule]:
        with self._lock:
            if self._rules is None or force:
                self._rules = self._read()
            return list(self._rules)

    def reload(self) -> List[PermissionRule]:
        return self.load(force=True)

    # ---------- 变更 ----------
    def upsert(self, rule: PermissionRule) -> Tuple[PermissionRule, str]:
        """
        新建或替换一条规则。返回 (规则, 动作)。

        动作是 `created` / `updated` —— 界面要说清「新增了一条」还是「改了一条」，
        同 id 覆盖和新建在体感上是两回事。
        """
        err = validate(rule)
        if err:
            raise ValueError(err)
        with self._lock:
            rules = self.load(force=True)
            if len(rules) >= settings.PERMISSION_MAX_RULES and not any(
                r.rule_id == rule.rule_id for r in rules
            ):
                raise ValueError(
                    f"规则最多 {settings.PERMISSION_MAX_RULES} 条 —— 先删掉不再需要的"
                )
            action = "updated"
            for i, existing in enumerate(rules):
                if existing.rule_id == rule.rule_id:
                    rule.created_at = existing.created_at  # 保留创建时间，界面按它排序
                    rules[i] = rule
                    break
            else:
                rules.insert(0, rule)
                action = "created"
            self._write(rules)
            self._rules = rules
        logger.info("权限规则 %s: %s", action, rule.describe())
        return rule, action

    def delete(self, rule_id: str) -> bool:
        with self._lock:
            rules = self.load(force=True)
            left = [r for r in rules if r.rule_id != rule_id]
            if len(left) == len(rules):
                return False
            self._write(left)
            self._rules = left
        logger.info("权限规则已删除: %s", rule_id)
        return True

    def set_enabled(self, rule_id: str, enabled: bool) -> Optional[PermissionRule]:
        with self._lock:
            rules = self.load(force=True)
            for i, r in enumerate(rules):
                if r.rule_id == rule_id:
                    r.enabled = enabled
                    rules[i] = r
                    self._write(rules)
                    self._rules = rules
                    return r
        return None

    # ---------- 查询 ----------
    def find(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, List[Tuple[PermissionRule, str]]]:
        """
        按行为分桶返回命中的规则（策略层靠这个顺序做优先级）。

        同一桶内的顺序 = 文件里的顺序（新建的在前）。
        命中的规则多于一条时界面会列出来，让用户看见「为什么是这条赢」。
        """
        buckets: Dict[str, List[Tuple[PermissionRule, str]]] = {DENY: [], ASK: [], ALLOW: []}
        for rule in self.load():
            hit = rule.match(tool_name, args)
            if hit and rule.behavior in buckets:
                buckets[rule.behavior].append((rule, hit))
        return buckets

    def stats(self) -> Dict[str, Any]:
        """规则表概况（设置页的「共 N 条，其中 M 条已停用」用它）。

        ⚠️ 这里**不报**「最后修改时间」。曾经报过，实现是 `_now_iso() if rules else ""` ——
        只要表非空就说「刚刚改过」，那是凭空造出来的字段，比没有更糟。
        真要这个信息应当读文件 mtime，等界面需要时再加。
        """
        rules = self.load()
        return {
            "total": len(rules),
            "enabled": sum(1 for r in rules if r.enabled),
            "by_behavior": {
                b: sum(1 for r in rules if r.behavior == b) for b in BEHAVIORS
            },
            "max_rules": settings.PERMISSION_MAX_RULES,
        }

    def reset(self) -> None:
        """清掉内存快照（测试与文件外部改动后用）"""
        with self._lock:
            self._rules = None


#: 进程级单例
rule_store = RuleStore()


__all__ = [
    "ALLOW",
    "ASK",
    "BEHAVIOR_HINTS",
    "BEHAVIOR_LABELS",
    "BEHAVIORS",
    "DENY",
    "PermissionRule",
    "RuleStore",
    "rule_store",
    "validate",
]
