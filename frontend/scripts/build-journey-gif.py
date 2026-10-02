"""Assemble docs/screenshots/user-journey.gif from the journey frames.

Reproduce (from frontend/, against a production build — see capture-screenshots.mjs):

    JOURNEY=1 SCREENSHOT_BASE_URL=http://localhost:3600 npm run screenshots
    uv run --no-project --with pillow python scripts/build-journey-gif.py

The first command writes 1440x900 viewport frames + frames.json (file, caption,
hold seconds) to JOURNEY_OUT (default /tmp/tt-journey-frames). This script
scales each frame to GIF_WIDTH (default 1200, LANCZOS), adds a white caption bar,
quantizes to an adaptive palette and writes a looping, optimized GIF.
Pillow is used ephemerally via `uv run --no-project --with pillow`; it is not a project dep.
"""

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FRAMES_DIR = Path(os.environ.get("JOURNEY_OUT", "/tmp/tt-journey-frames"))
OUT = Path(__file__).resolve().parents[2] / "docs" / "screenshots" / "user-journey.gif"
WIDTH = int(os.environ.get("GIF_WIDTH", "1200"))
COLORS = int(os.environ.get("GIF_COLORS", "256"))
INK = (26, 29, 33)  # #1A1D21
RULE = (222, 226, 230)
BAR_H = round(WIDTH * 0.04)  # caption bar height

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def load_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def build_frame(path: Path, caption: str, font) -> Image.Image:
    shot = Image.open(path).convert("RGB")
    h = round(shot.height * WIDTH / shot.width)
    shot = shot.resize((WIDTH, h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (WIDTH, h + BAR_H), "white")
    canvas.paste(shot, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.line([(0, h), (WIDTH, h)], fill=RULE, width=1)
    draw.text((WIDTH // 2, h + BAR_H // 2), caption, fill=INK, font=font, anchor="mm")
    return canvas


def main() -> None:
    manifest = json.loads((FRAMES_DIR / "frames.json").read_text())
    font = load_font(round(BAR_H * 0.42))
    frames = [build_frame(FRAMES_DIR / f["file"], f["caption"], font) for f in manifest]
    durations = [round(f["hold"] * 1000) for f in manifest]
    paletted = [
        im.quantize(
            colors=COLORS, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE
        )
        for im in frames
    ]
    paletted[0].save(
        OUT,
        save_all=True,
        append_images=paletted[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=1,
    )
    w, h = frames[0].size
    print(
        f"wrote {OUT} ({OUT.stat().st_size / 1e6:.2f} MB, {len(frames)} frames, {w}x{h})"
    )


if __name__ == "__main__":
    main()
