"""
沙箱路径解析 —— **唯一定义处**

Agent 的文件工具（读 / 写 / 删 / 列目录 / 生成 Office 文件）与
「下载这一轮产出的文件」接口，都必须走同一套判定：
**目标路径必须落在 `settings.sandbox_path` 之内**。

★ 为什么单独抽一个模块

这段逻辑原先只写在 `file_ops._resolve()` 里。当「会话产出文件下载」也要做
同样的校验时，如果就地再抄一份，两份实现迟早会漂移 —— 而这是**安全边界**，
漂移的代价是「某个入口能读到沙箱外的文件」。

⚠️ 判据是 `target == root or root in target.parents`，必须在 `resolve()` **之后**
比对：`resolve()` 会展开 `..` 与符号链接，先比字符串再 resolve 等于没校验
（`workspace/../.env` 的字符串前缀看起来仍在 `workspace/` 下）。
"""

from __future__ import annotations

from pathlib import Path

from app.config import settings


def sandbox_root() -> Path:
    """沙箱根目录（不存在时自动创建）"""
    return settings.sandbox_path.resolve()


def resolve_in_sandbox(path: str) -> Path:
    """
    把（来自用户 / LLM / URL 参数的）路径解析为沙箱内的绝对路径。

    Raises:
        PermissionError: 路径越出沙箱根目录
    """
    root = sandbox_root()
    raw = Path(path)
    target = (raw if raw.is_absolute() else root / raw).resolve()

    if target != root and root not in target.parents:
        raise PermissionError(
            f"路径越权：'{path}' 超出允许的工作目录 {root}。"
            "所有文件操作只能在沙箱目录内进行。"
        )
    return target


# ---------- Agent 工具的路径判定（沙箱 + 用户授权目录） ----------
def boundary_exempt() -> bool:
    """
    本次调用是否处于「不受目录限制」的权限模式（`full`）。

    ★ 为什么判据在上下文里而不是参数
    「能不能出沙箱」是用户配置的默认档，加上本轮只能**收紧**的覆盖，
    不能由工具参数或模型话术决定。
    `ToolContext.permission_mode` 由 agent 循环从图状态里带进来，
    而图状态里的这个字段只可能来自配置默认档与
    本轮的收紧覆盖（见 `permissions/policy.effective_mode`）。

    ★ 取不到上下文时一律按受限处理
    脚本直调工具、健康检查、没有会话身份的调用都会走到这里 ——
    默认收紧是唯一安全的方向。
    """
    # 延迟导入：`app.agents.tools.context` 只在执行期需要，
    # 放在模块顶层会让 sandbox ↔ tools 形成导入环。
    from app.agents.tools.context import get_tool_context
    from app.permissions.modes import escapes_file_boundary
    from app.permissions.policy import effective_mode

    return escapes_file_boundary(effective_mode(get_tool_context().permission_mode))


def _turn_workspace_root() -> Path | None:
    """
    本轮用户选定的工作区目录（`ToolContext.workspace_root`），没有则 None。

    ★ 为什么根目录从上下文里取，而不是加一个进程级设置
    「这一轮在哪个目录干活」是会话级的决定：两个标签页各开一个项目、
    后台子任务用派发那一刻冻结的目录，都必须互不干扰。
    写成进程级全局，症状就是「后打开的那个悄悄改掉了前一个的边界」。

    ★ 取不到上下文时返回 None，不返回沙箱
    调用方本来就把沙箱排在根列表第一位；这里再补一次只会掩盖漏传字段的路径。
    """
    # 延迟导入的理由与 `boundary_exempt` 相同：避免 sandbox ↔ tools 的导入环
    from app.agents.tools.context import get_tool_context

    raw = get_tool_context().workspace_root
    if raw is None:
        return None
    try:
        resolved = Path(raw).resolve()
    except OSError:
        return None
    # 目录中途被删/被挪走时按「没有工作区」处理：那句「路径越权」由下面的根列表给出，
    # 比在这里抛一个看不懂的 OSError 更好读。
    return resolved if resolved.is_dir() else None


def writable_roots() -> list[Path]:
    """
    允许 Agent **改动**的目录根：沙箱永远在第一位，其后是本轮工作区与配置里显式授权的目录。

    ★ 为什么授权清单放在配置而不是工具参数里
    「能碰哪些目录」是**用户对这台机器的决定**，不能由模型在调用时临时主张 ——
    否则任何一个能拼路径的工具都自动获得了全盘访问权。
    """
    roots = [sandbox_root()]
    turn = _turn_workspace_root()
    if turn is not None and turn not in roots:
        roots.append(turn)
    for p in settings.agent_allowed_dirs_list:
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def readable_roots() -> list[Path]:
    """
    允许读取的目录根 = 可写根 + 应用自己管理的附件目录 + 技能目录。

    ★ 附件为什么要单独放行读
    用户上传的临时附件落在 `ATTACHMENTS_DIR`（沙箱之外）。主对话把附件路径写进
    上下文后，子 Agent 需要能用 `read` 取原文；把它加进**可写**根等于允许模型
    删除/篡改用户刚上传的附件，而只放读则没有这个代价。

    ★ 技能目录为什么只放读
    渐进式披露的第三级（`scripts/` `references/` `assets/` 里的材料）就是靠
    `read` 打开的 —— 不放行，技能正文里「详见 references/checklist.md」那一句
    会直接回一句「路径越权」，症状是"装了技能反而更差"。
    不给可写是因为技能定义了她该怎么做：模型能改自己的做法说明，
    等于把一次跑偏写成了永久设置。用户要改，自己在磁盘上改。
    """
    roots = list(writable_roots())
    for extra in (
        settings.attachments_path,
        # 超长工具输出被落盘后，模型要靠 `read` 分段取回细节 —— 只读放行，
        # 不给可写：它是「模型自己产出的观测缓存」，删改它没有正当用途。
        settings.tool_results_path,
        settings.skills_dir,
        settings.skills_bundled_dir,
    ):
        try:
            resolved = extra.resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def resolve_agent_path(
    path: str, base: Path | None = None, for_write: bool = False
) -> Path:
    """
    解析 Agent 工具传入的路径，允许落在对应的目录根内。

    相对路径相对 `base`（缺省沙箱根）解析；绝对路径必须落在某个根内。
    `for_write=True` 只认 `writable_roots()`（附件目录可看不可改）。

    Raises:
        PermissionError: 路径既不在沙箱也不在任何允许的目录内
    """
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("路径不能为空")

    anchor = (base or sandbox_root()).resolve()
    candidate = Path(raw).expanduser()
    target = (candidate if candidate.is_absolute() else anchor / candidate).resolve()

    # `full` 模式：目录边界由**用户**解除了，这里只负责把路径算干净。
    # ⚠️ 只有 `resolve_agent_path`（Agent 工具）看这一档；
    # `resolve_in_sandbox`（文件下载等用户直连入口）永远严格。
    if boundary_exempt():
        return target

    roots = writable_roots() if for_write else readable_roots()
    if any(target == root or root in target.parents for root in roots):
        return target

    kind = "可读写" if for_write else "可读"
    raise PermissionError(
        f"路径越权：'{path}' 不在{kind}范围内。允许的目录：\n"
        + "\n".join(f"  - {r}" for r in roots)
        + "\n（要继续，请把这些目录之一加入 AGENT_ALLOWED_DIRS，"
        "或把 PERMISSION_MODE 配置为 full）"
    )


def display_path(path: Path) -> str:
    """
    把绝对路径转成对用户/模型友好的形式：沙箱内给相对路径，授权目录给绝对路径。
    """
    root = sandbox_root()
    try:
        if path == root:
            return "."
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


__all__ = [
    "display_path",
    "readable_roots",
    "resolve_agent_path",
    "resolve_in_sandbox",
    "sandbox_root",
    "writable_roots",
]
