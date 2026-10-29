"""Draw the four small pictures the demo backup uses as thumbnails (needs Pillow: pip install -e ".[dev]").

They're simple drawn scenes, not photos, so the demo holds nothing real. Run from the project folder:

    python scripts/make_demo_pictures.py
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = (360, 270)
OUT = Path(__file__).resolve().parent.parent / "afterimage" / "pictures"


def gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", SIZE)
    draw = ImageDraw.Draw(image)
    for y in range(SIZE[1]):
        t = y / (SIZE[1] - 1)
        draw.line([(0, y), (SIZE[0], y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    return image


def sunset() -> Image.Image:
    image = gradient((255, 168, 92), (112, 64, 140))
    draw = ImageDraw.Draw(image)
    draw.ellipse([210, 60, 290, 140], fill=(255, 236, 190))
    draw.polygon([(0, 270), (0, 190), (90, 120), (170, 200), (250, 150), (360, 230), (360, 270)], fill=(48, 24, 72))
    return image


def meadow() -> Image.Image:
    image = gradient((126, 192, 238), (214, 236, 250))
    draw = ImageDraw.Draw(image)
    draw.ellipse([40, 40, 100, 100], fill=(255, 245, 200))
    draw.ellipse([-120, 170, 260, 420], fill=(92, 168, 88))
    draw.ellipse([140, 190, 520, 440], fill=(64, 138, 70))
    for x, y in [(70, 215), (110, 228), (210, 238), (260, 226), (300, 246)]:
        draw.ellipse([x, y, x + 7, y + 7], fill=(250, 220, 90))
    return image


def night() -> Image.Image:
    image = gradient((14, 22, 58), (40, 46, 96))
    draw = ImageDraw.Draw(image)
    stars = random.Random(4821)
    for _ in range(70):
        x, y = stars.randrange(SIZE[0]), stars.randrange(170)
        draw.point((x, y), fill=(230, 232, 255))
    draw.ellipse([250, 40, 300, 90], fill=(236, 236, 228))
    draw.ellipse([262, 34, 312, 84], fill=(18, 26, 64))  # a crescent
    draw.rectangle([0, 210, 360, 270], fill=(10, 12, 28))
    for x in range(0, 360, 46):
        draw.polygon([(x, 214), (x + 20, 150), (x + 40, 214)], fill=(6, 16, 20))
    return image


def lake() -> Image.Image:
    image = gradient((170, 210, 240), (236, 244, 250))
    draw = ImageDraw.Draw(image)
    draw.polygon([(0, 150), (80, 80), (150, 140), (230, 70), (360, 150)], fill=(120, 136, 160))
    draw.rectangle([0, 150, 360, 270], fill=(80, 140, 190))
    reflection = (
        image.crop((0, 30, 360, 150)).transpose(Image.Transpose.FLIP_TOP_BOTTOM).filter(ImageFilter.GaussianBlur(3))
    )
    image.paste(Image.blend(reflection, Image.new("RGB", reflection.size, (80, 140, 190)), 0.55), (0, 150))
    for x in range(10, 360, 38):
        draw.polygon([(x, 152), (x + 9, 112), (x + 18, 152)], fill=(36, 84, 52))
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, draw in {"sunset": sunset, "meadow": meadow, "night": night, "lake": lake}.items():
        draw().save(OUT / f"{name}.jpg", "JPEG", quality=82, optimize=True)
        print(OUT / f"{name}.jpg", (OUT / f"{name}.jpg").stat().st_size, "bytes")


if __name__ == "__main__":
    main()
