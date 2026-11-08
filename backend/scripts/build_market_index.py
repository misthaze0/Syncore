#!/usr/bin/env python3
"""
生成 `backend/market/index.json` —— 目录条目不手写，从两处来：

1. `market/packs/<名>/SKILL.md` 的 frontmatter → 技能条目
   （`metadata.market_*` 那几个键：market_name / market_category / market_tags /
    market_popularity / market_featured）
2. `market/catalog.json` → 组合包 + 连接器

★ 为什么 index.json 是产物

技能卡片上的名字和描述如果手写在 index.json 里，包正文改了、目录还说旧的，
而且没人会记得两处一起改。让包自己带着目录元信息，加一个技能 = 加一个目录。
index.json 仍然提交进仓库（和 lockfile 一个道理）：桌面版离线也要能开市场。

★ 组合包成员的名字以包为准

catalog.json 里 `components[].name` 只是给人读的提示，生成时会用被引用技能自己的
`market_name` 覆盖掉 —— 否则「改了包里的显示名，包详情还是旧名」这种小谎会长期留着。

★ 校验先于产出

连接器条目装不下去、组合包引用了不存在的技能，这类错误在生成时就炸，
不留到用户点「安装」的时候。校验用的是运行时同一套模型（`MCPServerPayload`），
所以「脚本说 OK」和「装得上」是同一件事。

用法：
    python scripts/build_market_index.py            # 重新生成 index.json
    python scripts/build_market_index.py --check     # 只校验是否过期（CI 用，过期退出码 1）
    python scripts/build_market_index.py --verbose   # 多打一份分类分布
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.api.mcp import MCPServerPayload  # noqa: E402
from app.skills import _split_frontmatter  # noqa: E402

MARKET = BACKEND / "market"
PACKS = MARKET / "packs"
CATALOG = MARKET / "catalog.json"
INDEX = MARKET / "index.json"

KIND_ORDER = ("skill", "plugin", "mcp")
DANGER_LEVELS = ("safe", "moderate", "danger")


class BuildError(Exception):
    """目录构建失败 —— 一条消息说清是哪个包/哪条条目、坏在哪"""


# ---------- 技能：从 packs 扫 ----------

def scan_packs() -> List[Dict[str, Any]]:
    if not PACKS.is_dir():
        raise BuildError(f"技能包目录不存在：{PACKS}")

    entries: List[Dict[str, Any]] = []
    for md in sorted(PACKS.glob("*/SKILL.md")):
        entries.append(_pack_entry(md))

    seen: Dict[str, str] = {}
    for e in entries:
        prev = seen.get(e["canonical"])
        if prev:
            raise BuildError(f"技能名重复：{e['canonical']}（{prev} 与 {e['_from']}）")
        seen[e["canonical"]] = e["_from"]
    return entries


def _pack_entry(md: Path) -> Dict[str, Any]:
    dir_name = md.parent.name
    text = md.read_text(encoding="utf-8")
    fm, body, err = _split_frontmatter(text)
    if err or fm is None:
        raise BuildError(f"{dir_name}: {err or 'frontmatter 读不出来'}")
    if not body.strip():
        raise BuildError(f"{dir_name}: frontmatter 下面没有正文指令")

    name = str(fm.get("name") or "").strip()
    if name != dir_name:
        # 名字和目录不一致时，安装落盘的目录名和注册表里的 canonical 会分叉，
        # 之后「卸载再装」就会撞已存在 —— 现在拦住比那时解释便宜。
        raise BuildError(f"{dir_name}: frontmatter 的 name 是「{name}」，和目录名不一致")

    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    description = str(fm.get("description") or "").strip()
    if not description:
        raise BuildError(f"{dir_name}: description 为空，模型看不到用途就永远不会想起用它")
    category = str(meta.get("market_category") or "").strip()
    if not category:
        raise BuildError(
            f"{dir_name}: 没写 metadata.market_category —— 没有分类的条目在目录页找不到归属"
        )

    tags = meta.get("market_tags")
    if tags is not None and not isinstance(tags, list):
        raise BuildError(f"{dir_name}: metadata.market_tags 要写成列表")

    entry: Dict[str, Any] = {
        "kind": "skill",
        "canonical": name,
        "name": str(meta.get("market_name") or name),
        "description": description,
        "source": "official",
        "publisher": str(meta.get("owner") or "Syncore"),
        "version": str(meta.get("version") or "1.0"),
        "category": category,
        "tags": [str(t) for t in (tags or [])],
        "install": {"kind": "pack-dir", "path": dir_name},
        "danger_level": str(meta.get("market_danger") or "safe"),
        "popularity": _popularity(meta),
        "featured": bool(meta.get("market_featured") or False),
        # 技能条目不带 updated_at：包里没有可靠的「发布日期」，写一个会随构建漂移的
        # 日期比空着更糟 —— 界面这一栏空着，排序自动退到按名字。
        "_from": f"packs/{dir_name}/SKILL.md",
    }
    if entry["danger_level"] not in DANGER_LEVELS:
        raise BuildError(f"{dir_name}: metadata.market_danger「{entry['danger_level']}」不认识")
    return entry


def _popularity(meta: Dict[str, Any]) -> int:
    raw = meta.get("market_popularity")
    if raw is None:
        return 50
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise BuildError(f"market_popularity 不是整数：{raw!r}") from e
    if not 0 <= value <= 100:
        raise BuildError(f"market_popularity 要在 0–100，当前 {value}")
    return value


# ---------- 手写条目：catalog.json ----------

def load_catalog() -> Dict[str, Any]:
    if not CATALOG.is_file():
        raise BuildError(f"缺少 {CATALOG}")
    try:
        data = json.loads(CATALOG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BuildError(f"catalog.json 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise BuildError("catalog.json 顶层要是一个对象")
    return data


def catalog_entries(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for group, expect_kind in (("plugins", "plugin"), ("connectors", "mcp")):
        items = catalog.get(group) or []
        if not isinstance(items, list):
            raise BuildError(f"catalog.json 的 {group} 要是一个数组")
        for raw in items:
            entry = dict(raw)
            entry["kind"] = _normalize(entry, expect_kind, group)
            out.append(entry)
    return out


def _normalize(entry: Dict[str, Any], expect_kind: str, group: str) -> str:
    where = f"{group}「{entry.get('canonical') or '（缺 canonical）'}」"
    kind = str(entry.get("kind") or expect_kind)
    if kind != expect_kind:
        raise BuildError(f"{where} 的 kind 是「{kind}」，{group} 里只放 {expect_kind}")
    if not str(entry.get("canonical") or "").strip():
        raise BuildError(f"{where} 缺 canonical")
    if not str(entry.get("name") or "").strip():
        raise BuildError(f"{where} 缺 name")
    if not str(entry.get("description") or "").strip():
        raise BuildError(f"{where} 缺 description")
    if not str(entry.get("category") or "").strip():
        raise BuildError(f"{where} 缺 category")
    danger = str(entry.get("danger_level") or "safe")
    if danger not in DANGER_LEVELS:
        raise BuildError(f"{where} 的 danger_level「{danger}」不认识")
    entry.setdefault("source", "official")
    entry.setdefault("version", "1.0.0")
    entry.setdefault("tags", [])
    entry.setdefault("popularity", 50)
    entry.setdefault("featured", False)
    entry.setdefault("updated_at", "")
    entry["danger_level"] = danger
    entry["_from"] = "catalog.json"
    if kind == "plugin":
        _check_plugin(entry, where)
    else:
        _check_connector(entry, where)
    return kind


def _check_plugin(entry: Dict[str, Any], where: str) -> None:
    components = entry.get("components") or []
    if not components:
        raise BuildError(f"{where} 是组合包但没有任何成员")
    # 组合包不自己装东西，成员各装各的：install.kind 留空是刻意的，
    # 但字段得在，否则读目录时形状不齐。
    entry["install"] = {"kind": ""}
    for spec in components:
        if not isinstance(spec, dict) or not str(spec.get("ref") or "").strip():
            raise BuildError(f"{where} 的成员要写成 {{kind, ref, name}}")


def _check_connector(entry: Dict[str, Any], where: str) -> None:
    install = entry.get("install") or {}
    if str(install.get("kind") or "") != "mcp-server":
        raise BuildError(f"{where} 的 install.kind 必须是 mcp-server")
    server = dict(install.get("server") or {})
    if not server.get("name"):
        # 和 marketplace._install_mcp 保持同一套默认值，否则「脚本过了、装的时候炸」
        server["name"] = entry["canonical"]
    server.setdefault("danger_level", entry["danger_level"])
    server.setdefault("note", f"来自插件市场：{entry['name']}")
    try:
        parsed = MCPServerPayload(**server)
    except Exception as e:  # noqa: BLE001 —— pydantic 的报错原文最有用，别翻译
        raise BuildError(f"{where} 的 install.server 过不去校验：{e}") from e
    if parsed.name != entry["canonical"]:
        raise BuildError(
            f"{where} 的 server.name 是「{parsed.name}」，和 canonical 不一致"
        )
    if (parsed.danger_level or entry["danger_level"]) != entry["danger_level"]:
        raise BuildError(
            f"{where} 的 danger_level 和 server.danger_level 不一致（界面上会自相矛盾）"
        )
    install["server"] = server
    entry["install"] = install


# ---------- 合并 + 交叉校验 ----------

def build_entries() -> List[Dict[str, Any]]:
    skills = scan_packs()
    catalog = load_catalog()
    others = catalog_entries(catalog)

    by_canonical: Dict[str, Dict[str, Any]] = {}
    for entry in skills + others:
        canonical = entry["canonical"]
        if canonical in by_canonical:
            raise BuildError(
                f"canonical「{canonical}」同时出现在 {by_canonical[canonical]['_from']} "
                f"和 {entry['_from']}"
            )
        by_canonical[canonical] = entry

    for entry in others:
        if entry["kind"] != "plugin":
            continue
        for spec in entry["components"]:
            ref = str(spec["ref"])
            target = by_canonical.get(ref)
            if target is None:
                raise BuildError(
                    f"组合包「{entry['canonical']}」引用了目录里不存在的成员：{ref}"
                )
            if target["kind"] == "plugin":
                raise BuildError(f"组合包「{entry['canonical']}」的成员 {ref} 不能又是组合包")
            # 成员显示名以被引用条目自己说的为准
            spec["kind"] = target["kind"]
            spec["name"] = target["name"]

    return sorted(
        (clean(e) for e in by_canonical.values()),
        key=lambda e: (KIND_ORDER.index(e["kind"]), e["canonical"]),
    )


def clean(entry: Dict[str, Any]) -> Dict[str, Any]:
    """去掉构建期用的私有键，并把字段顺序固定下来（产物要能看懂 diff）"""
    entry = {k: v for k, v in entry.items() if k != "_from"}
    order = [
        "kind", "canonical", "name", "description", "source", "publisher", "version",
        "category", "tags", "components", "install", "danger_level", "required_env",
        "popularity", "featured", "updated_at",
    ]
    out = {key: entry[key] for key in order if key in entry}
    missing = set(entry) - set(order)
    if missing:
        raise BuildError(f"条目 {entry.get('canonical')} 带了目录模型不认识的字段：{sorted(missing)}")
    return out


def build_text(entries: List[Dict[str, Any]]) -> str:
    payload = {
        "$schema": "./index.schema.json",
        "updated_at": max((e.get("updated_at") or "" for e in entries), default=""),
        "entries": entries,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# ---------- 入口 ----------

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成插件市场目录 index.json")
    parser.add_argument("--check", action="store_true", help="只校验产物是否过期，不写文件")
    parser.add_argument("--verbose", action="store_true", help="打印分类分布")
    args = parser.parse_args(argv)

    try:
        entries = build_entries()
        text = build_text(entries)
    except BuildError as e:
        print(f"目录构建失败：{e}", file=sys.stderr)
        return 1

    if args.check:
        current = INDEX.read_text(encoding="utf-8") if INDEX.is_file() else ""
        if current.rstrip("\n") + "\n" != text:
            print(
                "index.json 和 packs/ + catalog.json 不一致（目录过期了）。\n"
                "跑一次：python scripts/build_market_index.py",
                file=sys.stderr,
            )
            return 1
        print(f"目录是最新的：{len(entries)} 条")
        return 0

    INDEX.write_text(text, encoding="utf-8")

    by_kind: Dict[str, int] = {}
    for e in entries:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    summary = " / ".join(f"{k} {by_kind.get(k, 0)}" for k in KIND_ORDER)
    print(f"已写入 {INDEX.relative_to(BACKEND).as_posix()}：{len(entries)} 条（{summary}）")

    if args.verbose:
        cats: Dict[str, List[str]] = {}
        for e in entries:
            cats.setdefault(e["category"], []).append(e["name"])
        for cat in sorted(cats):
            print(f"  {cat}（{len(cats[cat])}）：{'、'.join(cats[cat])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
