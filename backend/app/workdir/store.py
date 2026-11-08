"""
工作区登记表 —— 用户授权的本地文件夹，以及「哪个会话正在用哪个工作区」。

★ 为什么叫 workdir 而不是 workspace

`app/workspace` 已经被「工作空间 = 群组 + 数字员工」那套组织概念占了。
这里的东西完全不同：它就是**一个磁盘目录**，界面上叫「工作区」，
代码里叫 `workdir`，两边各说各的话，但绝不共用一个名字 ——
否则半年后没人分得清 `workspace_store.get_workspace()` 拿的是员工还是文件夹。

★ 登记表就是授权清单（安全边界）

`POST /api/workdirs` 是**唯一**能让一个新目录进入 Agent 可达范围的地方，
而它只可能来自用户在目录选择器里的一次明确点击。
聊天请求带的是工作区 **id**，不是路径 —— 模型、钩子、任何一段被注入的文本
都没办法靠「传一个 /etc 进来」拿到新目录，它们连 id 都造不出来。

★ 与会话的绑定落在后端

「每个会话各自记住自己的工作区」是产品决定。绑定如果只存在前端 localStorage，
换一台设备/换一个标签页就会看到「界面显示着 A 项目，实际跑在沙箱里」——
症状是文件莫名找不到，而且没有任何一处报错。所以真相在后端这份文件里，
前端只是它的一个视图。

★ 两种来源：手动登记 vs 对话自动分配（`origin`）

手动的是用户点选的目录（`manual`），列表里出现、可以选择；
自动的是每个对话窗口一个产物文件夹（`auto`），名字是 `年月日+当日序号`
（如 `2026102901`），由系统在**第一次真正用到**时创建。

★ 「一个窗口一个文件夹」指的是整个对话，不是每一轮

同一个 thread 里聊多少轮都写进**同一个**文件夹：目录只建一次，绑定随即改写成
真实 id，之后每轮 `resolve_thread_root` 只是按 id 取回它。
换一个对话窗口才是另一个文件夹 —— 否则同一段对话的产物散在几十个目录里，
「这次生成的报告在哪」就只能靠时间猜。

自动目录刻意建在沙箱**里面**：下载、预览、打包三个接口只认
`resolve_in_sandbox()`（见 `core/sandbox.py`）。放到沙箱外，「本次产出」
会变成一排点不动的按钮 —— 而那个目录存在的唯一意义就是装这些产出。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import string
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.conversations.store import is_valid_thread_id

logger = logging.getLogger(__name__)

STORE_VERSION = 1

#: 工作区 id 的形态（进 URL 路径段，与会话 id 同一类攻击面，宁窄勿宽）
_WORKDIR_ID_RE = re.compile(r"^wd_[A-Za-z0-9_-]{4,40}$")

#: 绑定表里的特殊值：这个会话由系统自动分配一个产物文件夹。
#: 不是假工作区 id —— `resolve_thread_root` 看到它就去物化，然后改写成真实 id。
AUTO_BINDING = "auto"

#: 自动目录的来源标记；老记录没有这个字段，一律按手动处理
ORIGIN_MANUAL = "manual"
ORIGIN_AUTO = "auto"

#: 自动目录的名字：8 位日期 + 至少两位当日序号（`2026102901`）
_AUTO_DIR_RE = re.compile(r"^(\d{8})(\d{2,})$")

#: 一次最多数多少个文件（自动目录不该很大，数到上限就停，界面显示的是下限）
STATS_MAX_ENTRIES = 5000

#: 图标与颜色会被前端渲染成 class / 内联 style —— 只认白名单，其余回落到默认值。
#: 名字必须与 `frontend/src/components/WorkdirPicker.tsx` 的 `WORKDIR_ICONS` 对齐。
ICON_NAMES = (
    "folder",
    "code",
    "terminal",
    "database",
    "globe",
    "book",
    "brain",
    "bot",
    "sparkles",
    "wine",
    "star",
    "heart",
    "zap",
    "flag",
    "layers",
    "monitor",
)
DEFAULT_ICON = "folder"
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLOR = "#8b1e3f"

#: 名字只是界面上的一行字，太长会把列表撑坏
NAME_MAX = 40

#: 绑定表最多留多少条（会话是无限增长的，这份文件不该跟着无限长）
MAX_BINDINGS = 3000


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def is_valid_workdir_id(value: str) -> bool:
    return bool(value) and bool(_WORKDIR_ID_RE.match(value))


def normalize_icon(icon: str) -> str:
    clean = str(icon or "").strip().lower()
    return clean if clean in ICON_NAMES else DEFAULT_ICON


def normalize_color(color: str) -> str:
    raw = str(color or "").strip()
    return raw if _COLOR_RE.match(raw) else DEFAULT_COLOR


def normalize_name(name: str, path: Path | str = "") -> str:
    clean = " ".join(str(name or "").split())[:NAME_MAX]
    if clean:
        return clean
    # 没起名就用目录名 —— 「未命名工作区」这种占位词对用户没有任何信息量
    tail = Path(str(path or "")).name
    return (tail or "工作区")[:NAME_MAX]


# ---------- 落盘 ----------

_lock = threading.RLock()


def _read(path: Path) -> dict[str, Any]:
    """读一份 JSON 登记表；不存在/损坏一律退化成空表（与知识边界同一退化方向）"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning("工作区登记表不可读，按空表处理: %s → %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, payload: dict[str, Any]) -> None:
    """原子写：先落临时文件再 `os.replace`（半截文件会让下次启动读到空表）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------- 工作区清单 ----------

def _all_workdirs() -> list[dict[str, Any]]:
    """登记表里的全部条目（两种来源都在；调用方自己按 `origin` 过滤）"""
    with _lock:
        items = _read(settings.workdir_store_path).get("workdirs")
    if not isinstance(items, list):
        return []
    out = [item for item in items if isinstance(item, dict) and item.get("id")]
    for item in out:
        # 老记录没有 `origin`：它们全都是用户点选登记进来的，按手动处理
        if item.get("origin") not in (ORIGIN_MANUAL, ORIGIN_AUTO):
            item["origin"] = ORIGIN_MANUAL
    return out


def list_workdirs(origin: str = ORIGIN_MANUAL) -> list[dict[str, Any]]:
    """
    工作区清单，按最近使用排序（同刻按创建时间）。

    ★ 默认只给**手动登记**的那批
    自动分配的目录是每段对话一个，用过几十次对话就是几十条。
    它们混进「选择工作区」清单，会把用户真正点选过的那几个淹没掉；
    要看它们去「设置 → 数据」。传 `origin=""` 可以取全部。

    每条现算一个 `exists`：目录可能被用户在资源管理器里删掉。
    不实时判定的话，界面会一直显示一个点进去只会报「路径不存在」的工作区，
    而用户需要看到的是「这个已经不在了」。
    """
    items = _all_workdirs()
    if origin:
        items = [item for item in items if item.get("origin") == origin]
    for item in items:
        item["exists"] = _dir_exists(str(item.get("path") or ""))
    items.sort(key=lambda i: str(i.get("last_used_at") or i.get("created_at") or ""), reverse=True)
    return items


def get_workdir(workdir_id: str) -> dict[str, Any] | None:
    if not is_valid_workdir_id(workdir_id):
        return None
    return next((w for w in _all_workdirs() if w["id"] == workdir_id), None)


def _save(items: list[dict[str, Any]]) -> None:
    _write(settings.workdir_store_path, {"version": STORE_VERSION, "updated_at": _now(), "workdirs": items})


def create_workdir(*, name: str, path: str, icon: str = "", color: str = "") -> dict[str, Any]:
    """
    登记一个工作区 = 授权 Agent 读写这个目录。

    Raises:
        ValueError: 路径不是绝对路径、不存在或不是目录（界面上原样显示这句话）
    """
    target = validate_dir_path(path)
    with _lock:
        items = _all_workdirs()
        # 同一个目录登记两次会得到两个名字不同的条目，用户分不清哪个才是当前生效的
        dup = next((w for w in items if _same_dir(str(w.get("path") or ""), target)), None)
        if dup is not None:
            raise ValueError(f"这个目录已经登记为「{dup.get('name')}」")
        entry = {
            "id": f"wd_{uuid.uuid4().hex[:12]}",
            "name": normalize_name(name, target),
            "path": str(target),
            "icon": normalize_icon(icon),
            "color": normalize_color(color),
            "created_at": _now(),
            "last_used_at": _now(),
            "exists": True,
            "origin": ORIGIN_MANUAL,
            "thread_id": "",
        }
        items.append(entry)
        _save(items)
        logger.info("登记工作区: %s → %s", entry["name"], target)
        return dict(entry)


def update_workdir(workdir_id: str, **fields: Any) -> dict[str, Any] | None:
    """改名 / 换图标 / 换颜色 / 换目录。只认这四个键，其余忽略。"""
    with _lock:
        items = _all_workdirs()
        idx = next((i for i, w in enumerate(items) if w.get("id") == workdir_id), -1)
        if idx < 0:
            return None
        item = dict(items[idx])
        if "name" in fields:
            item["name"] = normalize_name(str(fields["name"]), item.get("path") or "")
        if "icon" in fields:
            item["icon"] = normalize_icon(str(fields["icon"]))
        if "color" in fields:
            item["color"] = normalize_color(str(fields["color"]))
        if "path" in fields:
            item["path"] = str(validate_dir_path(str(fields["path"])))
        items[idx] = item
        _save(items)
        item["exists"] = _dir_exists(item["path"])
        return item


def delete_workdir(workdir_id: str, *, purge: bool = False) -> bool:
    """
    撤销登记（默认**只删登记，不碰磁盘**）。同时清掉引用它的会话绑定。

    ★ `purge=True` 只对自动目录生效
    手动工作区是用户自己的文件夹 —— 可能是整个项目目录。「撤销授权」的语义
    只是「不让她再碰」，把它做成「删你的文件」是这个产品最不能犯的错。
    自动目录则相反：它由系统创建、只装这段对话的产出，随登记一起删才不留垃圾。
    即便如此也要再确认一次「确实在沙箱里」，路径异常时宁可不删。
    """
    with _lock:
        items = _all_workdirs()
        entry = next((w for w in items if w.get("id") == workdir_id), None)
        if entry is None:
            return False
        keep = [w for w in items if w.get("id") != workdir_id]
        _save(keep)
        bindings = _read(settings.workdir_bindings_path).get("bindings")
        if isinstance(bindings, dict) and workdir_id in bindings.values():
            _save_bindings({k: v for k, v in bindings.items() if v != workdir_id})
        logger.info("撤销工作区授权: %s", workdir_id)

    if purge:
        _purge_auto_dir(entry)
    return True


def _purge_auto_dir(entry: dict[str, Any]) -> bool:
    """删掉一个自动目录的磁盘内容。不是自动目录 / 不在沙箱里就什么都不做。"""
    if entry.get("origin") != ORIGIN_AUTO:
        logger.warning("拒绝删除非自动工作区的磁盘内容: %s", entry.get("id"))
        return False
    target = _to_dir(str(entry.get("path") or ""))
    root = _sandbox_dir()
    if target is None or root is None:
        return False
    if target == root or root not in target.parents:
        logger.warning("拒绝删除沙箱外的工作区目录: %s", target)
        return False
    try:
        shutil.rmtree(target)
        return True
    except OSError as e:  # noqa: BLE001
        logger.warning("工作区目录删除失败: %s (%s)", target, e)
        return False


def touch_workdir(workdir_id: str) -> None:
    """记一次「被用到」，让列表按最近使用排序。失败不打扰主流程。"""
    try:
        with _lock:
            items = _all_workdirs()
            hit = next((w for w in items if w.get("id") == workdir_id), None)
            if hit is None:
                return
            hit["last_used_at"] = _now()
            _save(items)
    except Exception as e:  # noqa: BLE001
        logger.debug("更新工作区使用时间失败（已忽略）: %s %s", workdir_id, e)


# ---------- 自动工作区（每段对话一个产物文件夹） ----------

def _sandbox_dir() -> Path | None:
    """沙箱根（自动目录的父目录）。取不到就当没有自动工作区能力。"""
    try:
        from app.core.sandbox import sandbox_root

        root = sandbox_root()
        root.mkdir(parents=True, exist_ok=True)
        return root
    except Exception as e:  # noqa: BLE001
        logger.warning("沙箱目录不可用，无法分配自动工作区: %s", e)
        return None


def _next_auto_name(root: Path, today: str) -> str:
    """
    当天还没被占用的最小名字：`2026102901`。

    扫的是**磁盘上的目录名**而不是登记表 —— 用户可能自己在资源管理器里
    建过一个同名文件夹，撞上了就换下一个，不覆盖。
    """
    seq = 0
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            m = _AUTO_DIR_RE.match(child.name)
            if m and m.group(1) == today:
                seq = max(seq, int(m.group(2)))
    except OSError as e:  # noqa: BLE001
        logger.debug("扫描沙箱目录失败（按从 01 开始）: %s", e)
    return f"{today}{seq + 1:02d}"


def _ensure_auto_locked(thread_id: str) -> dict[str, Any] | None:
    """
    在已持锁的前提下给会话分配产物文件夹（有就复用）。

    目录被用户删掉时**重新分配**，而不是复活旧条目：旧登记指向的东西已经不存在了。
    名字则以磁盘为准 —— 刚被删掉的那个编号空出来了，就照常从它开始用；
    真正占着它的目录还在，就往后顺延（见 `_next_auto_name`）。
    """
    items = _all_workdirs()
    hit = next(
        (w for w in items if w.get("origin") == ORIGIN_AUTO and w.get("thread_id") == thread_id),
        None,
    )
    if hit is not None and _to_dir(str(hit.get("path") or "")) is not None:
        return dict(hit)

    root = _sandbox_dir()
    if root is None:
        return None

    today = datetime.now().strftime("%Y%m%d")
    for _ in range(50):
        name = _next_auto_name(root, today)
        target = root / name
        try:
            # 不用 exist_ok：抢到这个名字才算数（并发与残留目录都靠这一句兜底）
            target.mkdir()
        except FileExistsError:
            continue
        except OSError as e:  # noqa: BLE001
            logger.warning("创建对话工作区失败: %s (%s)", target, e)
            return None

        if hit is not None:
            # 目录没了：把旧条目换掉（同一个会话不该在清单里留两条）
            items = [w for w in items if w.get("id") != hit.get("id")]
        entry = {
            "id": f"wd_{uuid.uuid4().hex[:12]}",
            "name": name,
            "path": str(target),
            "icon": "folder",
            "color": DEFAULT_COLOR,
            "created_at": _now(),
            "last_used_at": _now(),
            "exists": True,
            "origin": ORIGIN_AUTO,
            "thread_id": thread_id,
        }
        items.append(entry)
        _save(items)
        logger.info("分配对话工作区: %s → %s", thread_id, target)
        return dict(entry)
    return None


def ensure_auto_workdir(thread_id: str) -> dict[str, Any] | None:
    """给一个会话分配（或取回）它的产物文件夹；会话 id 非法时返回 None。"""
    if not is_valid_thread_id(thread_id):
        return None
    with _lock:
        return _ensure_auto_locked(thread_id)


def auto_workdirs() -> list[dict[str, Any]]:
    """全部自动目录，附文件数与占用字节（「设置 → 数据」那一页用）"""
    items = list_workdirs(ORIGIN_AUTO)
    for item in items:
        stats = dir_stats(str(item.get("path") or ""))
        item["files"] = stats["files"]
        item["bytes"] = stats["bytes"]
    return items


def auto_root() -> Path | None:
    """自动目录的父目录（就是沙箱根）；不可用时 None"""
    return _sandbox_dir()


def dir_stats(path: str) -> dict[str, int]:
    """目录里有多少文件、占多少字节。数到上限就停（界面显示的是下限）。"""
    root = _to_dir(path)
    if root is None:
        return {"files": 0, "bytes": 0}
    files = 0
    total = 0
    try:
        for p in root.rglob("*"):
            if files >= STATS_MAX_ENTRIES:
                break
            try:
                if p.is_file():
                    files += 1
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError as e:  # noqa: BLE001
        logger.debug("统计目录失败（已忽略）: %s (%s)", root, e)
    return {"files": files, "bytes": total}


# ---------- 会话绑定 ----------

def _save_bindings(bindings: dict[str, str]) -> None:
    # 插入顺序即使用顺序，超出上限时丢掉最早的一批（活跃会话会被重新写回末尾）
    if len(bindings) > MAX_BINDINGS:
        bindings = dict(list(bindings.items())[-MAX_BINDINGS:])
    _write(settings.workdir_bindings_path, {"version": STORE_VERSION, "updated_at": _now(), "bindings": bindings})


def get_binding(thread_id: str) -> str:
    """这个会话当前绑定的工作区 id（空串 = 没绑，走沙箱）。"""
    if not is_valid_thread_id(thread_id):
        return ""
    with _lock:
        bindings = _read(settings.workdir_bindings_path).get("bindings")
    if not isinstance(bindings, dict):
        return ""
    value = bindings.get(thread_id)
    return value if isinstance(value, str) else ""


def set_binding(thread_id: str, workdir_id: str) -> str:
    """
    把一个会话绑到某个工作区。

    - 真实 id：绑到那个目录
    - 空串：解绑，回到沙箱（用户明确选了「不指定」）
    - `"auto"`：交给系统分配 —— 目录在第一次真正用到时才建（见 `resolve_thread_root`），
      所以「新建了对话但没发过消息」不会留下一堆空文件夹

    Raises:
        ValueError: 会话 id 非法，或工作区 id 不存在
    """
    if not is_valid_thread_id(thread_id):
        raise ValueError("会话标识无效")
    wid = str(workdir_id or "").strip()
    if wid and wid != AUTO_BINDING:
        entry = get_workdir(wid)
        if entry is None:
            raise ValueError("工作区不存在（可能已被删除）")
        if not entry.get("exists"):
            raise ValueError(f"目录已不存在：{entry['path']}")
    with _lock:
        bindings_raw = _read(settings.workdir_bindings_path).get("bindings")
        bindings = {k: v for k, v in bindings_raw.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(bindings_raw, dict) else {}
        if wid:
            bindings[thread_id] = wid
        else:
            bindings.pop(thread_id, None)
        _save_bindings(bindings)
        touch_workdir(wid)
    return wid


def resolve_thread_root(thread_id: str) -> Path | None:
    """
    这个会话本轮该在哪个目录里干活；`None` = 用配置的沙箱默认值。

    ⚠️ 绑定指向的目录不见了要**退回沙箱**，不能报错也不能返回一个不存在的路径：
    前者会让整段对话发不出去（用户只看到「会话出错了」），
    后者会让文件工具在深处抛一个看不懂的 OSError。
    退回沙箱的代价是「这次没找到你要的文件」，而这是真的，也修得好。

    ★ `auto` 绑定在这里物化
    这是「新对话默认自动分配」的落点：建目录、登记、把绑定改写成真实 id，
    三步在同一把锁里完成 —— 否则两轮并发的首条消息会各建一个文件夹。

    ★ 自动目录被删掉时重新分配，而不是退回沙箱
    物化之后绑定指向的是真实 id，用户完全可能回头把那个文件夹删了。
    若此时退回沙箱，这段对话的产出就会散落在沙箱根上 —— 而那正是
    「每段对话一个文件夹」要避免的事。手动工作区则相反：目录没了就是没了，
    退回沙箱比随便挑一个目录更诚实。
    """
    wid = get_binding(thread_id)
    if not wid:
        return None
    if wid == AUTO_BINDING:
        return _materialize_auto(thread_id)
    entry = get_workdir(wid)
    if entry is None:
        return None
    root = _to_dir(str(entry.get("path") or ""))
    if root is None:
        if entry.get("origin") == ORIGIN_AUTO:
            logger.info("对话工作区目录已被删除，重新分配: thread=%s %s", thread_id, entry.get("path"))
            set_binding(thread_id, AUTO_BINDING)
            return _materialize_auto(thread_id)
        logger.warning("会话绑定的工作区目录已不存在，本轮退回沙箱: thread=%s %s", thread_id, entry.get("path"))
        return None
    return root


def _materialize_auto(thread_id: str) -> Path | None:
    with _lock:
        # 锁内再看一眼：并发进来的第二个请求应当直接走已经改写好的真实绑定
        current = get_binding(thread_id)
        if current and current != AUTO_BINDING:
            entry = get_workdir(current)
            return _to_dir(str(entry.get("path") or "")) if entry else None

        entry = _ensure_auto_locked(thread_id)
        if entry is None:
            logger.warning("自动工作区分配失败，本轮退回沙箱: thread=%s", thread_id)
            return None
        bindings_raw = _read(settings.workdir_bindings_path).get("bindings")
        bindings = {k: v for k, v in bindings_raw.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(bindings_raw, dict) else {}
        bindings[thread_id] = str(entry["id"])
        _save_bindings(bindings)
    return _to_dir(str(entry.get("path") or ""))


# ---------- 路径工具 ----------

def _dir_exists(path: str) -> bool:
    return _to_dir(path) is not None


def _to_dir(path: str) -> Path | None:
    """能解析成一个真实目录就返回它，否则 None（不抛）"""
    if not path:
        return None
    try:
        p = Path(path).expanduser().resolve()
    except OSError:
        return None
    return p if p.is_dir() else None


def _same_dir(a: str, b: Path) -> bool:
    """
    两个路径是不是同一个目录。

    Windows 大小写不敏感且 `C:/x` 与 `C:\\x` 长得不同 —— 直接比字符串会得到
    「同一个目录登记了两次」这种看上去像 bug 的结果，所以两边都走 `resolve()`。
    """
    pa = _to_dir(a)
    return pa is not None and pa == b


def validate_dir_path(path: str) -> Path:
    """
    校验并归一化一个「要被授权出去的目录路径」。

    ★ 为什么要求绝对路径
    相对路径会跟着后端进程的当前工作目录漂移 —— 界面上写着 `data`，
    实际指向的却是用户根本没看过的那个 `data`。目录选择器给出的永远是绝对路径，
    所以这条限制只挡住手写请求的人。
    """
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("目录不能为空")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("请填写绝对路径（在目录选择器里点选即可）")
    try:
        resolved = p.resolve()
    except OSError as e:
        raise ValueError(f"目录无法解析：{e}") from e
    if not resolved.exists():
        raise ValueError(f"目录不存在：{resolved}")
    if not resolved.is_dir():
        raise ValueError(f"那是一个文件，不是目录：{resolved}")
    return resolved


# ---------- 目录浏览（给前端的文件夹选择器） ----------

#: 一次最多列多少个条目 —— 选目录不该把一整棵 `node_modules` 搬进响应
BROWSE_LIMIT = 300


def drive_roots() -> list[Path]:
    """这台机器的盘根（Windows）；其他平台就是 `/`"""
    if os.name == "nt":
        out: list[Path] = []
        for letter in string.ascii_uppercase:
            candidate = Path(f"{letter}:\\")
            try:
                # 光驱/U 盘/已断开的网络盘会在访问时抛 OSError，直接跳过
                if candidate.exists() and candidate.is_dir():
                    out.append(candidate)
            except OSError:
                continue
        return out
    return [Path("/")]


def quick_roots() -> list[dict[str, Any]]:
    """
    目录选择器的快捷入口：主目录 + 几个常用文件夹 + 盘根。

    这些是**导航起点**，不是授权清单 —— 用户从这里进到哪一层，
    最终登记出去的仍然是他点定的那一个目录。
    """
    home = Path.home()
    named = [
        ("主目录", home),
        ("桌面", home / "Desktop"),
        ("文档", home / "Documents"),
        ("下载", home / "Downloads"),
    ]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label, p in named + [(str(r), r) for r in drive_roots()]:
        try:
            key = str(p.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "path": key, "exists": p.is_dir()})
    return out


def browse(path: str, query: str = "") -> dict[str, Any]:
    """
    列出某个目录下的**子目录**（选择器只要目录，文件对它没意义）。

    `can_read=False` 而不是 500：权限不足的文件夹真实存在，
    界面需要把它显示成「进不去」，而不是假装那里什么都没有。
    """
    target = validate_dir_path(path)
    try:
        entries = sorted(
            (p for p in target.iterdir() if p.is_dir()),
            key=lambda p: p.name.lower(),
        )
    except PermissionError:
        return {"path": str(target), "parent": _parent_of(target), "entries": [], "can_read": False,
                "truncated": False, "total": 0}
    except OSError as e:
        raise ValueError(f"无法读取该目录：{e}") from e

    q = query.strip().lower()
    matched = [p for p in entries if not q or q in p.name.lower()]
    total = len(matched)
    shown = matched[:BROWSE_LIMIT]
    return {
        "path": str(target),
        "parent": _parent_of(target),
        "can_read": True,
        "truncated": total > BROWSE_LIMIT,
        "total": total,
        "count": len(shown),
        "entries": [{"name": p.name, "path": str(p)} for p in shown],
    }


def _parent_of(p: Path) -> str:
    """往上一层；已经在盘根时给空串（前端据此禁用「上一级」）"""
    return str(p.parent) if p.parent != p else ""


__all__ = [
    "AUTO_BINDING",
    "ICON_NAMES",
    "ORIGIN_AUTO",
    "ORIGIN_MANUAL",
    "auto_root",
    "auto_workdirs",
    "browse",
    "create_workdir",
    "delete_workdir",
    "dir_stats",
    "ensure_auto_workdir",
    "get_binding",
    "get_workdir",
    "is_valid_workdir_id",
    "list_workdirs",
    "normalize_color",
    "normalize_icon",
    "normalize_name",
    "quick_roots",
    "resolve_thread_root",
    "set_binding",
    "touch_workdir",
    "update_workdir",
    "validate_dir_path",
]
