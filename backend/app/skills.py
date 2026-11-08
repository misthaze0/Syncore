"""
技能（Skills）—— 把「教会她做一件事」变成一个文件夹

一个技能 = 一个目录，里面一份 `SKILL.md`（YAML frontmatter + 正文指令），
可选 `scripts/` `references/` `assets/` 放支撑材料。这是 Agent Skills 规范的
结构，照它做有两个理由：一是**别人写的技能能直接放进来**（生态），
二是规范里那套「渐进式披露」正好解决本项目的老毛病 —— 以前所谓的"技能"
只是一段塞进输入框的提示词，没法随附材料，也没法让模型按需加载。

★ 渐进式披露在这里落成三级（这是本模块存在的核心理由）

| 级别 | 内容 | 什么时候进上下文 | 成本 |
|---|---|---|---|
| L1 | `name` + `description` | 每轮都注入（`build_prompt_block`） | 每技能一行 |
| L2 | `SKILL.md` 正文 | 模型调用 `use_skill` 时才给 | 一份技能的全文 |
| L3 | `scripts/` `references/` `assets/` 里的文件 | L2 末尾给出清单，模型用 `read` 逐个取 | 单个文件 |

**装得多不等于上下文贵**：30 个技能每轮只多 30 行一句话描述，正文和材料只在
真要用的那一轮才进来。这也是这里不给每个技能注册一个工具的原因 ——
N 个工具定义会进**每一个**请求，而工具语义是「能执行的动作」，
技能是「该怎么做这段活」，两者不是一回事。

★ 两个目录，不合并

- `settings.skills_bundled_dir`（`backend/skills/`，随仓库走）：出厂技能，改它 = 改产品，走 git。
- `settings.skills_dir`（`data/skills/`，不入库）：用户自己放的。

同名时**用户目录赢**（她能覆盖出厂做法而不必去改仓库文件），内置那份仍出现在
清单里、标 `shadowed` —— 静默把它藏起来的症状是「仓库里明明有这个技能，
`skills_list` 却说没有」。

★ `allowed-tools` 只提示、不强制

规范里这个字段声明「这个技能只该用这些工具」。真正的强制属于权限层
（哪一轮能用哪些工具、拦下来怎么提示），这一期没做。这里只做**如实告知**：
加载 L2 时把它显示出来，并写明「这是给模型看的约定，系统没有拦死其它工具」。
比假装生效安全得多 —— 后者会让人以为越界操作被挡住了。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.config import settings

logger = logging.getLogger(__name__)

# ---------- 规范里的硬约束（见模块开头） ----------
NAME_MAX_CHARS = 64
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESCRIPTION_MAX_CHARS = 1024
COMPATIBILITY_MAX_CHARS = 500

#: 规范建议 `SKILL.md` 正文控制在 5000 token 量级。下面这条**是我们**的字符闸门：
#: 超了不拒绝加载，只在清单里标 `oversized` —— 写得长的技能仍然能用，
#: 代价（挤掉同一轮里的检索内容）应该让人知道，而不是替他决定。
BODY_WARN_CHARS = 20000

#: L3 材料所在的约定目录（规范里就这三个名字）
RESOURCE_DIRS = ("scripts", "references", "assets")

#: 单个技能最多列出的材料文件数（`references/` 里塞两百个文件时不要整个倒进上下文）
MAX_RESOURCE_FILES = 40

#: frontmatter 里认得的键。`metadata` 在这里列是因为**它本身**是规范字段，
#: 里面的自定义键要原样透出（丢掉就等于把别人写的技能信息删了一半）
_KNOWN_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


@dataclass
class Skill:
    """一个已解析的技能（L1 元数据 + 定位到正文与材料；正文本身不常驻内存）"""

    name: str
    description: str
    dir_path: Path
    origin: str  # "bundled" | "user"
    license: str = ""
    compatibility: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    allowed_tools: List[str] = field(default_factory=list)
    body_chars: int = 0
    resources: List[str] = field(default_factory=list)
    resources_truncated: bool = False
    #: 让技能**不可用**的原因（frontmatter 缺失 / `name` 不合规 / 没有描述或正文）。
    #: 非空 = 不参与注入，但**必须出现在清单里**：用户改 SKILL.md 打错一个字母时，
    #: 界面上技能凭空消失比"显示出来并说明哪不对"难查得多。
    error: str = ""
    #: 不拦使用的**提示**（`name` 与目录名不一致、未知键、正文偏长）。
    #: 分成两档是因为「一处写法不规范就把技能废掉」太重 ——
    #: 目录名和 name 不一致照样能跑，只需要有人知道。
    warnings: List[str] = field(default_factory=list)
    #: 被 `data/skills` 里的同名技能遮住（因此这份内置的不生效）
    shadowed: bool = False

    @property
    def usable(self) -> bool:
        return not self.error

    @property
    def skill_md(self) -> Path:
        return self.dir_path / "SKILL.md"

    def to_dict(self, *, enabled: bool = True) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "origin": self.origin,
            "enabled": enabled,
            "usable": self.usable,
            "error": self.error,
            "warnings": list(self.warnings),
            "shadowed": self.shadowed,
            "dir": str(self.dir_path),
            "license": self.license,
            "compatibility": self.compatibility,
            "metadata": self.metadata,
            "allowed_tools": self.allowed_tools,
            "body_chars": self.body_chars,
            "oversized": self.body_chars > BODY_WARN_CHARS,
            "resources": self.resources,
            "resources_truncated": self.resources_truncated,
        }


def _split_frontmatter(text: str) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """
    拆 `SKILL.md`。

    Returns:
        `(frontmatter, body, error)` —— error 非空时前两项无意义。
    """
    if not text.lstrip("\ufeff").startswith("---"):
        return None, "", "文件开头缺少 YAML frontmatter（一对 `---` 包住的元数据）"
    lines = text.lstrip("\ufeff").splitlines()
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return None, "", "frontmatter 没有闭合（缺少第二个 `---`）"
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as e:
        return None, "", f"frontmatter 不是合法 YAML：{_one_line(str(e))[:200]}"
    if not isinstance(data, dict):
        return None, "", "frontmatter 不是一个键值映射"
    return data, "\n".join(lines[end + 1:]).strip(), ""


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _list_resources(root: Path) -> Tuple[List[str], bool]:
    """
    列出 L3 材料（相对技能目录、POSIX 分隔符 —— 这是给模型看的路径，不要混进 `\\`）。

    Returns:
        `(相对路径列表, 是否被上限截断)`
    """
    out: List[str] = []
    for sub in RESOURCE_DIRS:
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            out.append(p.relative_to(root).as_posix())
            if len(out) >= MAX_RESOURCE_FILES:
                return out, True
    return out, False


def _parse_skill(md_path: Path, origin: str) -> Skill:
    """读一份 `SKILL.md` → `Skill`（任何失败都收敛成 `error` 字段，不抛异常）"""
    dir_path = md_path.parent
    dir_name = dir_path.name
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return Skill(dir_name, "", dir_path, origin, error=f"读取 SKILL.md 失败：{e}")

    fm, body, err = _split_frontmatter(text)
    if err or fm is None:
        return Skill(dir_name, "", dir_path, origin, error=err)

    raw_name = str(fm.get("name") or "").strip()
    raw_tools = fm.get("allowed-tools")
    allowed = (
        [str(t).strip() for t in raw_tools if str(t).strip()]
        if isinstance(raw_tools, list)
        else ([str(raw_tools).strip()] if str(raw_tools or "").strip() else [])
    )
    raw_meta = fm.get("metadata")
    skill = Skill(
        name=(raw_name or dir_name)[:NAME_MAX_CHARS],
        description=str(fm.get("description") or "").strip()[:DESCRIPTION_MAX_CHARS],
        dir_path=dir_path,
        origin=origin,
        license=str(fm.get("license") or "").strip(),
        compatibility=str(fm.get("compatibility") or "").strip()[:COMPATIBILITY_MAX_CHARS],
        metadata=dict(raw_meta) if isinstance(raw_meta, dict) else {},
        allowed_tools=allowed,
        body_chars=len(body),
    )
    skill.resources, skill.resources_truncated = _list_resources(dir_path)

    errors: List[str] = []
    warnings: List[str] = []
    if not raw_name:
        warnings.append(f"frontmatter 未写 `name`，用目录名「{dir_name}」作为技能名")
    elif not NAME_RE.match(raw_name):
        errors.append(f"`name` 不合规：只允许小写字母、数字和连字符（当前是 {raw_name!r}）")
    elif raw_name != dir_name:
        warnings.append(f"`name`（{raw_name}）与目录名（{dir_name}）不一致")
    if len(raw_name) > NAME_MAX_CHARS:
        errors.append(f"`name` 超过 {NAME_MAX_CHARS} 字符")
    if not skill.description:
        # `description` 是必填项，更是 L1 里模型唯一能看到的东西：
        # 没有它，这个技能永远不会被想起来 —— 与其"装着但从不生效"，不如现在就报出来。
        errors.append("`description` 为空 —— 模型看不到用途，就永远不会想起用这个技能")
    if not body:
        errors.append("frontmatter 下面没有正文指令")
    if not isinstance(raw_meta, dict) and raw_meta is not None:
        warnings.append("`metadata` 不是一个键值映射（已忽略）")
    unknown = sorted(str(k) for k in fm if str(k) not in _KNOWN_KEYS)
    if unknown:
        # 说而不废：`allowed_tools:`（下划线写法）这类笔误很常见，
        # 一句提示比悄悄丢掉那个字段有用得多。
        warnings.append(f"未知的 frontmatter 键：{'、'.join(unknown)}")
    if skill.body_chars > BODY_WARN_CHARS:
        warnings.append(f"正文约 {skill.body_chars} 字，偏长（会占同一轮里的上下文）")

    skill.error = "；".join(errors)
    skill.warnings = warnings
    return skill


class SkillStore:
    """技能扫描 + 启停状态（进程内缓存，按目录指纹失效）"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Optional[List[Skill]] = None
        self._fingerprint: Optional[Tuple[Tuple[str, float], ...]] = None

    # ---------- 扫描 ----------

    def _roots(self) -> List[Tuple[Path, str]]:
        roots: List[Tuple[Path, str]] = []
        bundled = settings.skills_bundled_dir
        if bundled.is_dir():
            roots.append((bundled, "bundled"))
        roots.append((settings.skills_dir, "user"))
        return roots

    def _fingerprint_now(self) -> Tuple[Tuple[str, float], ...]:
        """
        目录指纹 = 每个根的 mtime + 其中每份 `SKILL.md` 的 mtime。

        ★ 为什么不用 TTL：TTL 的失效窗口里，用户刚放进去的技能在设置页上还是看不见
        —— 而"我装了，她为什么说没有"正是这个功能最常见的一次交互。
        指纹只花几次 `stat`，比读 JSON 便宜，也就没有"什么时候该刷新"这个问题了。
        """
        fp: List[Tuple[str, float]] = []
        for root, _origin in self._roots():
            try:
                fp.append((str(root), root.stat().st_mtime))
                for md in sorted(root.glob("*/SKILL.md")):
                    fp.append((str(md), md.stat().st_mtime))
            except OSError:
                fp.append((str(root), -1.0))
        return tuple(fp)

    def scan(self, *, force: bool = False) -> List[Skill]:
        """扫描两个目录（用户目录的同名技能生效，内置那份标 `shadowed` 保留）"""
        with self._lock:
            fp = self._fingerprint_now()
            if not force and self._cache is not None and fp == self._fingerprint:
                return self._cache

            parsed: List[Skill] = []
            for root, origin in self._roots():
                if not root.is_dir():
                    continue
                for md in sorted(root.glob("*/SKILL.md")):
                    parsed.append(_parse_skill(md, origin))

            user_names = {s.name for s in parsed if s.origin == "user" and s.usable}
            for s in parsed:
                if s.origin == "bundled" and s.name in user_names:
                    s.shadowed = True
            # 生效的排前面（`get()` 取第一个匹配 → 用户版本赢）。
            # ★ 坏掉的用户版本也排在内置前面：用户放了一份写坏的 SKILL.md 时，
            # 「use_skill 报出哪一行坏了」比「悄悄回退到出厂那份、看起来一切正常」有用 ——
            # 回退会让用户以为自己改的内容生效了。
            parsed.sort(key=lambda x: (x.shadowed, x.origin != "user", x.name.lower()))

            self._cache, self._fingerprint = parsed, fp
            return parsed

    # ---------- 启停状态 ----------

    def _read_state(self) -> Dict[str, bool]:
        path = settings.skills_state_path
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # 状态文件读不出来 → 全部按默认（启用）。技能是增益，不能因为它
            # 坏了一次就把清单清空 —— 那会让用户以为技能丢了。
            logger.warning("技能状态文件读取失败，按默认（全部启用）处理: %s", e)
            return {}
        if not isinstance(data, dict):
            return {}
        overrides = data.get("overrides") or {}
        return (
            {str(k): bool(v) for k, v in overrides.items()} if isinstance(overrides, dict) else {}
        )

    def is_enabled(self, name: str) -> bool:
        """默认启用，只有状态文件里显式写 false 才关。"""
        return self._read_state().get(name, True)

    def set_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        """开/关一个技能。回到默认（开）时删掉覆写项，不留 `false→true` 的冗余记录。"""
        known = {s.name for s in self.scan()}
        if name not in known:
            raise KeyError(f"技能不存在: {name}")
        state = self._read_state()
        if enabled:
            state.pop(name, None)
        else:
            state[name] = False
        settings.skills_state_path.write_text(
            json.dumps({"overrides": state}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"name": name, "enabled": enabled}

    # ---------- 对外读 ----------

    def list_dicts(self) -> List[Dict[str, Any]]:
        state = self._read_state()
        return [s.to_dict(enabled=state.get(s.name, True)) for s in self.scan()]

    def get(self, name: str) -> Optional[Skill]:
        key = (name or "").strip().lower()
        for s in self.scan():
            if s.name.lower() == key:
                return s
        return None

    def active_skills(self) -> List[Skill]:
        """真正会进上下文的那些：解析没问题 + 已启用 + 没被遮住"""
        state = self._read_state()
        return [
            s for s in self.scan()
            if s.usable and not s.shadowed and state.get(s.name, True)
        ]

    # ---------- L1：注入块 ----------

    def build_prompt_block(self, *, allowed_names: set[str] | None = None) -> str:
        """
        每个启用技能压成一行（名字 + 描述）注入 system prompt。

        装不下时**明说**还有几个没列出并指向 `skills_list`。
        静默截断的症状是「她确实装了，但每轮都不知道」，用户那边表现为
        「我明明装了，她就是不用」，而日志里完全看不出来。
        """
        if not settings.SKILLS_ENABLED:
            return ""
        skills = self.active_skills()
        if allowed_names is not None:
            skills = [skill for skill in skills if skill.name in allowed_names]
        if not skills:
            return ""

        budget = max(200, int(settings.SKILLS_INJECT_MAX_CHARS or 0))
        lines: List[str] = []
        used = 0
        for s in skills:
            # 描述压成一行：L1 只负责「有没有这件事」，怎么做留给 L2
            line = f"- {s.name}: {' '.join(s.description.split())[:200]}"
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1

        if not lines:
            return (
                "【可用技能】技能功能是开着的，但注入预算太小，本轮一条都没列出。"
                "需要时用 `skills_list` 直接看清单。"
            )

        header = (
            "【可用技能】下面是本机已安装的技能（一份份写好的做法说明）。"
            "用户的诉求对上某条描述时，**先调用 `use_skill(名字)` 取回那份说明再动手** —— "
            "技能里写的正是这件事该怎么做，不取就等于凭印象做。\n"
        )
        block = header + "\n".join(lines)
        hidden = len(skills) - len(lines)
        if hidden > 0:
            block += (
                f"\n（还有 {hidden} 个技能因为长度限制没在这里列出，"
                "用 `skills_list` 可以看到全部。）"
            )
        return block

    # ---------- L2 + L3 ----------

    def load_instructions(self, name: str) -> str:
        """取回技能正文（L2），末尾附材料清单（进入 L3 的入口）"""
        skill = self.get(name)
        if skill is None:
            names = "、".join(s.name for s in self.scan()[:12]) or "（本机还没有技能）"
            return (
                f"没有找到技能「{name}」。技能名要逐字来自 skills_list（当前有：{names}）。"
                "大小写不敏感，但连字符和拼写必须一致。"
            )
        if skill.error:
            return (
                f"技能「{skill.name}」装上了但读不出可用内容：{skill.error}。"
                "这需要改那个 SKILL.md，换个名字重试没有用。"
            )
        if not self.is_enabled(skill.name):
            return (
                f"技能「{skill.name}」在设置页里是关闭的，所以本轮不按它来做。"
                "需要的话请在 设置 → 技能 里打开。"
            )

        try:
            text = skill.skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return f"读取技能「{skill.name}」正文失败：{e}"
        _fm, body, err = _split_frontmatter(text)
        if err:
            return f"技能「{skill.name}」正文解析失败：{err}"

        parts: List[str] = [f"【技能：{skill.name}】", f"用途：{skill.description}"]
        if skill.compatibility:
            parts.append(f"适用条件：{skill.compatibility}")
        if skill.allowed_tools:
            parts.append(
                f"该技能声明只应使用这些工具：{', '.join(skill.allowed_tools)}。"
                "这是写在技能里的约定，由你遵守 —— 系统并没有拦死其它工具。"
            )
        parts.append("----")
        parts.append(body)
        if skill.resources:
            # ★ 给**完整路径**而不是 `references/checklist.md`：`read` 的相对路径是相对
            # 沙箱根解析的，照技能里写的相对名去读只会得到「文件不存在」。
            # 技能正文（别人写的，按规范就该用相对名）里的引用靠下面那句前缀规则接上。
            prefix = skill.dir_path.as_posix().rstrip("/")
            listing = "\n".join(f"  · {prefix}/{r}" for r in skill.resources)
            more = (
                f"\n  · …另有更多文件未列出（上限 {MAX_RESOURCE_FILES} 个）。"
                if skill.resources_truncated
                else ""
            )
            parts.append(
                "----\n"
                "这个技能的材料文件（正文里提到的相对路径都要加上前缀 `" + prefix + "/` 才能打开）。\n"
                "需要时用 `read` 逐个取，一次一份（下面已经是完整路径，照着传即可，读了就占上下文）：\n"
                + listing + more
            )
        return "\n".join(parts)

    def describe_all(self) -> str:
        """给 `skills_list`：完整清单（不受注入预算限制），含被关闭与有问题的条目"""
        items = self.list_dicts()
        if not items:
            return (
                "本机还没有安装任何技能。技能放在 "
                f"`{settings.skills_dir}`（用户）或 `{settings.skills_bundled_dir}`（内置），"
                "每个技能一个子目录，里面要有 `SKILL.md`。"
            )
        live = [i for i in items if i["usable"] and i["enabled"] and not i["shadowed"]]
        lines = [f"本机技能共 {len(items)} 项，其中 {len(live)} 项会注入到对话里："]
        for i in items:
            ok = i["usable"] and i["enabled"] and not i["shadowed"]
            note = ""
            if i["error"]:
                note = f"（读不出来：{i['error']}）"
            elif not i["enabled"]:
                note = "（已在设置页关闭）"
            elif i["shadowed"]:
                note = "（被 data/skills 里的同名技能遮住，生效的是那一个）"
            elif i["warnings"]:
                note = f"（{'；'.join(i['warnings'])}）"
            lines.append(
                f"  {'✓' if ok else '×'} "
                f"[{'内置' if i['origin'] == 'bundled' else '用户'}] "
                f"{i['name']} —— {i['description']}{note}"
            )
        lines.append("用 `use_skill(名字)` 取某一份的完整做法说明。")
        return "\n".join(lines)


_store: Optional[SkillStore] = None
_store_lock = threading.Lock()


def get_skill_store() -> SkillStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = SkillStore()
        return _store


__all__ = ["RESOURCE_DIRS", "Skill", "SkillStore", "get_skill_store"]
