"""Record docs/demo.gif: `afterimage demo` typed into a terminal, its real output, then the report page.

The terminal is drawn with Pillow from the command's actual output, so the GIF always matches what the
program prints. The last frame is docs/report.png, a screenshot of the report the demo makes. Run from the
project folder after `pip install -e ".[dev]"`:

    python scripts/make_demo_gif.py
"""

from __future__ import annotations

import io
import tempfile
import textwrap
from datetime import timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from afterimage.cli import main

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT, PAD, SIZE, LEADING = 1100, 640, 22, 15, 21
COLUMNS = 116
BACKGROUND, TEXT, PROMPT, DIM = (24, 26, 33), (222, 224, 230), (125, 211, 140), (140, 146, 160)
FONTS = ["DejaVuSansMono.ttf", "Menlo.ttc", "Consolas.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]


def font() -> ImageFont.FreeTypeFont:
    for name in FONTS:
        try:
            return ImageFont.truetype(name, SIZE)
        except OSError:
            continue
    raise SystemExit("No monospaced font found: install DejaVu Sans Mono.")


def demo_output() -> list[str]:
    with tempfile.TemporaryDirectory() as folder:
        out = io.StringIO()
        main(["demo", str(Path(folder) / "afterimage-demo")], out=out, err=io.StringIO(), tz=timezone.utc)
    text = out.getvalue().replace(folder, "~")
    lines = []
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip())
        lines += textwrap.wrap(line, COLUMNS, subsequent_indent=" " * (indent + 4)) or [""]
    return lines


def frame(lines: list[str], typed: str | None, face: ImageFont.FreeTypeFont) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for x, colour in ((PAD, (255, 95, 87)), (PAD + 20, (254, 188, 46)), (PAD + 40, (40, 200, 64))):
        draw.ellipse([x, 14, x + 12, 26], fill=colour)
    rows = (HEIGHT - 50) // LEADING
    shown = (["$ " + typed] if typed is not None else []) + lines
    for row, line in enumerate(shown[-rows:]):
        y = 42 + row * LEADING
        colour = PROMPT if line.startswith("$ ") else DIM if line.startswith(("Made a sample", "The report")) else TEXT
        draw.text((PAD, y), line, font=face, fill=colour)
    return image


def main_gif() -> None:
    face = font()
    command = "afterimage demo"
    frames, durations = [], []
    for n in range(len(command) + 1):  # typing the command
        frames.append(frame([], command[:n] + "▌", face))
        durations.append(70 if n else 600)
    durations[-1] = 500
    output = demo_output()
    for end in range(3, len(output) + 3, 3):  # the output arriving a few lines at a time
        frames.append(frame(output[:end], command, face))
        durations.append(120)
    durations[-1] = 3500
    report = Image.open(ROOT / "docs" / "report.png").convert("RGB")
    scale = WIDTH / report.width
    report = report.resize((WIDTH, round(report.height * scale))).crop((0, 0, WIDTH, HEIGHT))
    frames.append(report)
    durations.append(4500)
    palette = [f.convert("P", palette=Image.Palette.ADAPTIVE, colors=128) for f in frames]
    target = ROOT / "docs" / "demo.gif"
    palette[0].save(target, save_all=True, append_images=palette[1:], duration=durations, loop=0, optimize=True)
    print(f"{target}: {len(frames)} frames, {target.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main_gif()
