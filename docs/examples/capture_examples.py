"""Prepare and post-process the fresh example captures in docs/examples/.

Two subcommands, run around the Playwright gallery capture (capture_gallery.mjs):

  prep      Rewrite the downloaded HTML gallery's GCS image URLs to local, downscaled
            JPEG copies (so it renders offline without GCS auth) and render the first two
            pages of the research PDF to PNG.
  optimize  Resize every raw capture to <= 1200 px wide and write optimized PNG/JPEG
            files into docs/examples/.

Usage (see capture_examples.md for the full recipe):
  uv run --no-project --with pypdfium2 --with pillow python \
      docs/examples/capture_examples.py prep /tmp/ex/creative_output
  uv run --no-project --with pillow python \
      docs/examples/capture_examples.py optimize /tmp/ex/raw docs/examples
"""

import re
import sys
from pathlib import Path

from PIL import Image

MAX_WIDTH = 1200
GCS_SRC = re.compile(
    r'https://storage\.mtls\.cloud\.google\.com/[^"]*/creative_output/'
    r'(?:resized/)?([^"/?]+)\?authuser=\d+'
)


def prep(run_dir: Path) -> None:
    local = run_dir / "local"
    local.mkdir(exist_ok=True)
    for png in run_dir.glob("*.png"):
        img = Image.open(png).convert("RGB")
        img.thumbnail((1200, 1200 * 4))
        img.save(local / f"{png.stem}.jpg", quality=85)
        # The lightbox loads the XL_local_ copy; point it at an equivalent local file.
        img.save(local / f"XL_local_{png.stem}.jpg", quality=85)
    html = (run_dir / "creative_portfolio_gallery.html").read_text(encoding="utf-8")
    html = GCS_SRC.sub(lambda m: Path(m.group(1)).stem + ".jpg", html)
    (local / "gallery.html").write_text(html, encoding="utf-8")

    import pypdfium2 as pdfium  # ty: ignore[unresolved-import]  # uv run --with pypdfium2

    raw = run_dir.parent / "raw"
    raw.mkdir(exist_ok=True)
    pdf = pdfium.PdfDocument(run_dir / "research_report_with_citations.pdf")
    for i in range(min(2, len(pdf))):  # cover/summary + a page dense with citations
        pdf[i].render(scale=2).to_pil().save(raw / f"research-page-{i + 1}.png")


def optimize(raw: Path, out: Path) -> None:
    for src in sorted(raw.glob("*.png")):
        img = Image.open(src).convert("RGB")
        if img.width > MAX_WIDTH:
            img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)))
        if src.stem.startswith("research"):
            # Text pages: palette PNG keeps glyphs crisp and small.
            dest = out / f"{src.stem}.png"
            img.quantize(colors=128).save(dest, optimize=True)
        else:
            # Photographic gallery captures compress far smaller as JPEG.
            dest = out / f"{src.stem}.jpg"
            img.save(dest, quality=82, optimize=True, progressive=True)
        print(f"{dest} {dest.stat().st_size // 1024} KB")


if __name__ == "__main__":
    cmd, first, *rest = sys.argv[1:]
    if cmd == "prep":
        prep(Path(first))
    else:
        optimize(Path(first), Path(rest[0]))
