"""导出办公基础依赖的具体版本、传递关系与许可证原文；不替代发行审查。

python scripts/audit_office_dependencies.py --output third_party/office-runtime
输出用于构建环境验收；--strict 使缺依赖、未知/非宽松声明或缺 notices 的情况失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

MANIFEST = Path(__file__).resolve().parents[1] / "third_party" / "office-components.json"
PERMISSIVE = {"MIT", "MIT-CMU", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "HPND", "Zlib", "PSF-2.0", "Python-2.0"}


def license_status(expression: str) -> str:
    # 不把复杂 SPDX 表达式拆开后猜授权；混合条款留给人工确认。
    return "permissive-declared" if expression.strip() in PERMISSIVE else "review-required"


def audit(output: Path) -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    pending = list(manifest["python_roots"])
    seen = set()
    packages = []
    issues = []
    environment = {**default_environment(), "extra": ""}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in seen:
            continue
        seen.add(name)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            issues.append({"package": name, "reason": "not-installed"})
            continue
        expression = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        status = license_status(expression)
        if status != "permissive-declared":
            issues.append({"package": name, "reason": "license-review-required"})
        notices = []
        for entry in dist.files or []:
            relative = str(entry).replace("\\", "/")
            base = Path(relative).name.lower()
            if ".dist-info/" not in relative or not base.startswith(("license", "licence", "copying", "notice", "authors")):
                continue
            source = Path(dist.locate_file(entry))
            if not source.is_file():
                continue
            # RECORD 内的路径不用于构造输出位置。
            target = output / "notices" / name / f"{len(notices):02d}-{source.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            target.write_bytes(data)
            notices.append({"file": target.relative_to(output).as_posix(), "sha256": hashlib.sha256(data).hexdigest()})
        if not notices:
            issues.append({"package": name, "reason": "license-text-missing"})
        requirements = []
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            dependency = canonicalize_name(requirement.name)
            requirements.append(dependency)
            pending.append(dependency)
        packages.append({"name": name, "version": dist.version, "license": expression,
                         "status": status, "requires": sorted(requirements), "notices": notices})
    report = {"schema_version": "1.0", "plugin": manifest["plugin"], "roots": manifest["python_roots"],
              "packages": sorted(packages, key=lambda item: item["name"]), "issues": issues,
              "scope": "当前 Python 环境的办公基础依赖图；不包含 extras、独立引擎、原生 wheel 内嵌依赖、模型、字体或其他产品组件，也不是法律批准。"}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    report = audit(args.output)
    print(json.dumps({"packages": len(report["packages"]), "issues": report["issues"], "report": str(args.output / "report.json")}, ensure_ascii=False))
    return 1 if args.strict and report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
