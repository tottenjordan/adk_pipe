"""Assemble docs/screenshots/user-journey.gif from the annotated journey frames.

Reproduce (from frontend/, against a production build — see capture-screenshots.mjs):

    JOURNEY=1 SCREENSHOT_BASE_URL=http://localhost:3600 npm run screenshots
    uv run --no-project --with pillow python scripts/build-journey-gif.py

The first command writes 1440x900 viewport frames (@2x) plus journey-frames.json
to JOURNEY_OUT (default /tmp/tt-journey-frames):

    [{file, step, phase, title, caption, hold_ms,
      highlights: [{x, y, w, h, label, place?}]}]   # boxes in 1440-wide viewport CSS px

For each frame this script scales the screenshot to GIF_WIDTH (default 1200,
LANCZOS) and draws:
  * a top progress strip (Brief → Research → Reviews → Images & eval → Results →
    History) with the frame's phase as an amber pill, past phases ink, future muted;
  * a spotlight: everything outside the highlight boxes is dimmed, each box gets an
    amber rounded outline and a callout pill (numbered when a frame has two),
    placed above/below/beside the box so it doesn't cover it (`place` hint first);
  * a bottom caption bar: "Step N of M · title" in bold plus a one-line caption.
Annotated frames are preceded by a short un-annotated beat (GIF_TRANSITION_MS,
default 600; 0 disables) so the eye sees the screen first, then the spotlight.
Output is quantized to an adaptive palette and written as a looping, optimized GIF.
GIF_EXPORT_DIR=/some/dir also saves each annotated frame as a PNG for review.
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
TRANSITION_MS = int(os.environ.get("GIF_TRANSITION_MS", "600"))  # 0 disables
EXPORT_DIR = os.environ.get("GIF_EXPORT_DIR")  # optional: also dump annotated PNGs
CAPTURE_W = 1440  # viewport width the highlight boxes are measured in

PHASES = ["Brief", "Research", "Reviews", "Images & eval", "Results", "History"]

INK = (26, 29, 33)  # #1A1D21
MUTED = (150, 156, 164)
RULE = (222, 226, 230)
ACCENT = (245, 158, 11)  # amber #F59E0B: annotation colour, distinct from UI cyan
HALO = (253, 230, 138)  # amber-200
DIM = 0.42  # darkening outside the spotlight

STRIP_H = 44
CAPTION_H = 76

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def load_font(
    size: int, bold: bool = False
) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT
    for p in (path, path.replace("/truetype/dejavu/", "/dejavu/")):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


F_STRIP = load_font(15)
F_STRIP_B = load_font(15, bold=True)
F_LABEL = load_font(18, bold=True)
F_BADGE = load_font(15, bold=True)
F_TITLE = load_font(22, bold=True)
F_CAPTION = load_font(17)


def text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return round(draw.textlength(text, font=font))


def overlaps(a: tuple, b: tuple, gap: int = 4) -> bool:
    return not (
        a[2] + gap <= b[0]
        or b[2] + gap <= a[0]
        or a[3] + gap <= b[1]
        or b[3] + gap <= a[1]
    )


def contains(outer: tuple, inner: tuple) -> bool:
    return (
        outer != inner
        and outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def draw_strip(draw: ImageDraw.ImageDraw, phase: str) -> None:
    draw.rectangle([0, 0, WIDTH, STRIP_H], fill="white")
    draw.line([(0, STRIP_H - 1), (WIDTH, STRIP_H - 1)], fill=RULE, width=1)
    current = PHASES.index(phase)
    arrow = "  →  "
    pad_x = 12
    widths = [
        text_w(draw, p, F_STRIP_B if i == current else F_STRIP) + 2 * pad_x
        for i, p in enumerate(PHASES)
    ]
    aw = text_w(draw, arrow, F_STRIP)
    x = (WIDTH - (sum(widths) + aw * (len(PHASES) - 1))) // 2
    cy = STRIP_H // 2
    for i, (p, w) in enumerate(zip(PHASES, widths, strict=True)):
        if i == current:
            draw.rounded_rectangle([x, cy - 14, x + w, cy + 14], radius=14, fill=ACCENT)
            draw.text((x + w // 2, cy), p, fill=INK, font=F_STRIP_B, anchor="mm")
        else:
            draw.text(
                (x + w // 2, cy),
                p,
                fill=INK if i < current else MUTED,
                font=F_STRIP,
                anchor="mm",
            )
        x += w
        if i < len(PHASES) - 1:
            draw.text((x, cy), arrow, fill=MUTED, font=F_STRIP, anchor="lm")
            x += aw


def place_label(
    box: tuple, size: tuple, avoid: list, top: int, bottom: int, hint: str | None = None
) -> tuple:
    """Pick a spot for a w x h pill next to box that stays on-canvas and clear of `avoid`.

    `hint` (above|below|right|left) is tried first; boxes that contain `box` (e.g. the
    report around a citation) are not avoided, since the pill must sit inside them.
    """
    x0, y0, x1, y1 = box
    w, h = size
    gap = 10
    left = min(max(x0, 8), WIDTH - w - 8)
    named = {
        "above": (left, y0 - gap - h),
        "below": (left, y1 + gap),
        "right": (x1 + gap, (y0 + y1 - h) // 2),
        "left": (x0 - gap - w, (y0 + y1 - h) // 2),
    }
    cands = [named[hint]] if hint in named else []
    cands += [
        named["above"],
        named["below"],
        named["right"],
        named["left"],
        (min(max(x1 - w, 8), WIDTH - w - 8), y0 - gap - h),  # above, right-aligned
        (min(max(x1 - w - 10, 8), WIDTH - w - 8), y0 + 10),  # inside, top-right
    ]
    for cx, cy in cands:
        r = (cx, cy, cx + w, cy + h)
        if cx < 4 or cx + w > WIDTH - 4 or cy < top + 4 or cy + h > bottom - 4:
            continue
        if any(overlaps(r, a) for a in avoid if not contains(a, box)):
            continue
        return r
    cx, cy = cands[-1]
    return (cx, cy, cx + w, cy + h)


def annotate(
    shot: Image.Image, frame: dict, total: int, spotlight: bool
) -> Image.Image:
    h = round(shot.height * WIDTH / shot.width)
    shot = shot.resize((WIDTH, h), Image.Resampling.LANCZOS)
    s = WIDTH / CAPTURE_W
    boxes = [
        (
            round(b["x"] * s),
            round(b["y"] * s),
            round((b["x"] + b["w"]) * s),
            round((b["y"] + b["h"]) * s),
        )
        for b in frame["highlights"]
    ]
    if spotlight and boxes:
        dark = Image.blend(shot, Image.new("RGB", shot.size, (0, 0, 0)), DIM)
        mask = Image.new("L", shot.size, 0)
        md = ImageDraw.Draw(mask)
        for b in boxes:
            md.rounded_rectangle(b, radius=8, fill=255)
        shot = Image.composite(shot, dark, mask)

    canvas = Image.new("RGB", (WIDTH, STRIP_H + h + CAPTION_H), "white")
    canvas.paste(shot, (0, STRIP_H))
    draw = ImageDraw.Draw(canvas)
    draw_strip(draw, frame["phase"])

    if spotlight and boxes:
        moved = [(x0, y0 + STRIP_H, x1, y1 + STRIP_H) for x0, y0, x1, y1 in boxes]
        for b in moved:
            draw.rounded_rectangle(
                (b[0] - 3, b[1] - 3, b[2] + 3, b[3] + 3),
                radius=11,
                outline=HALO,
                width=2,
            )
            draw.rounded_rectangle(b, radius=8, outline=ACCENT, width=4)
        avoid = list(moved)
        numbered = len(moved) > 1
        for i, (b, hl) in enumerate(zip(moved, frame["highlights"], strict=True)):
            label = hl["label"]
            badge = 24 if numbered else 0
            lw = text_w(draw, label, F_LABEL) + 28 + (badge + 8 if numbered else 0)
            lh = 34
            r = place_label(b, (lw, lh), avoid, STRIP_H, STRIP_H + h, hl.get("place"))
            avoid.append(r)
            draw.rounded_rectangle(r, radius=lh // 2, fill=ACCENT, outline=INK, width=1)
            tx = r[0] + 14
            if numbered:
                cy = (r[1] + r[3]) // 2
                draw.ellipse(
                    (tx - 4, cy - badge // 2, tx - 4 + badge, cy + badge // 2), fill=INK
                )
                draw.text(
                    (tx - 4 + badge // 2, cy),
                    str(i + 1),
                    fill=ACCENT,
                    font=F_BADGE,
                    anchor="mm",
                )
                tx += badge + 4
            draw.text(
                (tx, (r[1] + r[3]) // 2), label, fill=INK, font=F_LABEL, anchor="lm"
            )

    y = STRIP_H + h
    draw.line([(0, y), (WIDTH, y)], fill=RULE, width=1)
    draw.text(
        (WIDTH // 2, y + 25),
        f"Step {frame['step']} of {total} · {frame['title']}",
        fill=INK,
        font=F_TITLE,
        anchor="mm",
    )
    draw.text(
        (WIDTH // 2, y + 55),
        frame["caption"],
        fill=(70, 76, 84),
        font=F_CAPTION,
        anchor="mm",
    )
    return canvas


def main() -> None:
    manifest = json.loads((FRAMES_DIR / "journey-frames.json").read_text())
    total = len(manifest)
    frames: list[Image.Image] = []
    durations: list[int] = []
    for f in manifest:
        shot = Image.open(FRAMES_DIR / f["file"]).convert("RGB")
        if f["highlights"] and TRANSITION_MS > 0:
            frames.append(annotate(shot, f, total, spotlight=False))
            durations.append(TRANSITION_MS)
        final = annotate(shot, f, total, spotlight=True)
        frames.append(final)
        durations.append(int(f["hold_ms"]))
        if EXPORT_DIR:
            Path(EXPORT_DIR).mkdir(parents=True, exist_ok=True)
            final.save(Path(EXPORT_DIR) / f"annotated-{f['file']}")
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
