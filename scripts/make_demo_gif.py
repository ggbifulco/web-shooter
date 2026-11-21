"""Render a terminal transcript (docs/demo-transcript.txt) into docs/demo.gif.

The transcript is real CLI output; this script only animates it:
    uv run python scripts/make_demo_gif.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BG, FG, DIM = (17, 24, 39), (229, 231, 235), (148, 163, 184)
ACCENT, OK = (248, 113, 113), (74, 222, 128)
FONT_CANDIDATES = [
    "C:/Windows/Fonts/consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]
SIZE, PAD, LINE = 18, 24, 26


def font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, SIZE)
    return ImageFont.load_default()


def color(line: str) -> tuple[int, int, int]:
    if line.startswith("$"):
        return ACCENT
    if "ready" in line or "passed, 0 failed" in line or line.startswith("claude:"):
        return OK
    if line.startswith("      "):
        return DIM
    return FG


def render(lines: list[str], f, width: int, height: int, cursor: bool) -> Image.Image:
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([PAD + i * 22, 12, PAD + i * 22 + 12, 24], fill=c)
    y = 44
    for line in lines:
        d.text((PAD, y), line, font=f, fill=color(line))
        y += LINE
    if cursor:
        last = lines[-1] if lines else ""
        x = PAD + int(d.textlength(last, font=f))
        d.rectangle([x + 2, y - LINE + 4, x + 11, y - 4], fill=FG)
    return img


def main() -> None:
    src = ROOT / "docs" / "demo-transcript.txt"
    out = ROOT / "docs" / "demo.gif"
    lines = [ln.replace("\\", "/") for ln in src.read_text(encoding="utf-8").splitlines()]
    lines = [ln for ln in lines if not ln.startswith(("real", "user", "sys"))]
    f = font()
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    width = max(int(probe.textlength(ln, font=f)) for ln in lines) + 2 * PAD + 20
    height = 44 + LINE * (len(lines) + 1) + PAD
    frames: list[Image.Image] = []
    durations: list[int] = []
    command = lines[0]
    for i in range(2, len(command) + 1, 3):  # typing
        frames.append(render([command[:i]], f, width, height, True))
        durations.append(40)
    frames.append(render([command], f, width, height, True))
    durations.append(600)
    for n in range(2, len(lines) + 1):  # output appears line by line
        frames.append(render(lines[:n], f, width, height, False))
        durations.append(450 if lines[n - 1].strip() else 150)
    durations[-1] = 4000
    frames[0].save(
        out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True
    )
    print(f"wrote {out} ({len(frames)} frames, {out.stat().st_size // 1024} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
