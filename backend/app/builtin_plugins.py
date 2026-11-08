"""内置代码插件目录。沿用工具注册中心和市场库存，不创建另一套执行协议。"""
from app.office.manifest import plugin_manifest


def manifests() -> dict[str, dict]:
    office = plugin_manifest()
    return {office["id"]: office}


def get_builtin(canonical: str) -> dict | None:
    return manifests().get(canonical)


def market_entries() -> list:
    from app.marketplace import MarketEntry

    return [MarketEntry(
        kind="plugin", canonical=spec["id"], name=spec["name"],
        description=spec["description"], source="official", publisher="Syncore",
        version=spec["version"], category=spec["category"], tags=["内置", "办公"],
        danger_level="moderate", updated_at="2026-11-01",
    ) for spec in manifests().values()]
