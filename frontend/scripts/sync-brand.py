"""Export application assets from brand/*.png. Requires Pillow (pip install Pillow)."""

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
BRAND = ROOT / "brand"
ASSETS = ROOT / "frontend/src/assets/brand"
PUBLIC = ROOT / "frontend/public"
DESKTOP = ROOT / "desktop/build"


def export_png(source: str, destination: Path, size: tuple[int, int]) -> None:
    with Image.open(BRAND / source) as image:
        image.thumbnail(size, Image.Resampling.LANCZOS)
        image.save(destination, optimize=True)


def main() -> None:
    for directory in (ASSETS, PUBLIC, DESKTOP):
        directory.mkdir(parents=True, exist_ok=True)

    export_png("顶栏图标.png", ASSETS / "header-icon.png", (160, 160))
    export_png("应用图标.png", ASSETS / "app-icon.png", (512, 512))
    export_png("Syncore品牌标志.png", ASSETS / "wordmark.png", (900, 300))
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        export_png("应用图标.png", PUBLIC / name, (size, size))

    with Image.open(BRAND / "应用图标.png") as image:
        image = image.convert("RGBA")
        image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        sizes = [(size, size) for size in (16, 24, 32, 48, 64, 128, 256)]
        image.save(PUBLIC / "favicon.ico", sizes=sizes)
    (DESKTOP / "icon.ico").write_bytes((PUBLIC / "favicon.ico").read_bytes())
    print("Exported Syncore header, app icon, wordmark, web icons and desktop icon.")


if __name__ == "__main__":
    main()
