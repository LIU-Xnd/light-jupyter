"""Make the lj app icon.

The source was a photograph of a pixel-art toy in assets/, cropped to
the character and pixelated: the toy is made of square blocks, so
pixelating a photo of it gives the same look as the original rather
than a beige blur.

That photograph is not in the repository -- it was a working reference,
and a 800KB phone snapshot has no business in a source tree. The
generated icon-256.png is what ships; this script derives the other
sizes from it, so they can never disagree.

    python lj/make_icon.py
"""
import pathlib
import sys

from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
# the assets live at the repository root, not inside the package
ASSETS = HERE.parent / "assets"
SOURCE = ASSETS / "icon-256.png"

SIZES = (16, 24, 32, 48, 64, 128)


def main() -> int:
    if not SOURCE.is_file():
        raise SystemExit(f"no source icon at {SOURCE}")
    master = Image.open(SOURCE).convert("RGBA")
    master.save(SOURCE)

    for s in SIZES:
        master.resize((s, s), Image.Resampling.LANCZOS).save(
            ASSETS / f"icon-{s}.png")

    master.save(ASSETS / "lj.ico",
                sizes=[(s, s) for s in SIZES] + [(256, 256)])

    print("wrote:")
    for p in sorted(ASSETS.glob("icon-*.png")) + [ASSETS / "lj.ico"]:
        print(f"  {p.name:16} {p.stat().st_size:>7} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
