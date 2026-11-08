"""
子进程环境 —— 白名单继承的**唯一定义处**

★ 为什么从 shell 工具里搬出来

原来这段只写在 `agents/tools/builtin/shell.py`，因为只有 bash 会起子进程。
Phase 5 加了 Hooks：钩子同样是「用户在本机跑的命令行」，而且它由 agent 的动作触发，
暴露面比 bash 更大。如果钩子那边再抄一份白名单，两份迟早漂移 ——
而漂移的后果是**密钥泄漏**：`LLM_API_KEY` 出现在一个谁都没注意的子进程里。

★ 为什么是白名单而不是黑名单

业务密钥的名字是本项目的约定（`LLM_*` / `EMBEDDING_*` / `REDIS_URL` / `ES_*`），
而环境变量里还有什么取决于用户机器。默认拒绝、只放「让命令能正常跑起来」必需的那几个，
才是可以长期成立的规则。`_ENV_DENY_RE` 那一条是双保险，不是主判据。

★ 用这一层的三处

- `bash` 工具（agent 执行的命令）
- `/api/terminal`（用户手敲的命令，安全部照搬 shell）
- Hooks 钩子（用户自己写的脚本）
"""

from __future__ import annotations

import os
import re

#: 只保留「让命令能正常跑起来」所必需的系统级变量。
#: 业务密钥（LLM_* / EMBEDDING_* / ES_* / REDIS_URL / *_API_KEY）一律不透传。
ENV_ALLOWLIST = {
    # Windows
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "USERNAME", "USERDOMAIN",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    # POSIX
    "HOME", "LANG", "LC_ALL", "TERM", "TZ", "SHELL",
    # 通用
    "PATH",
}

#: 明确禁止透传的变量名模式（双保险：万一白名单里混进了什么）
ENV_DENY_RE = re.compile(r"(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.IGNORECASE)


def safe_env() -> dict[str, str]:
    """构造子进程环境：白名单继承 + 强制 UTF-8 + 去掉一切疑似凭据"""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper not in ENV_ALLOWLIST:
            continue
        if ENV_DENY_RE.search(key):
            continue
        env[key] = value

    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


__all__ = ["ENV_ALLOWLIST", "ENV_DENY_RE", "safe_env"]
