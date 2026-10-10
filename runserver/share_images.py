"""Share-image re-encode: a render PNG → a web-sized JPEG for public share links.

The image tool uploads ~10 MB 2K PNGs; a public share page (and its link preview,
where e.g. Facebook caps og:image at 8 MB) needs far less. ``to_share_jpeg`` is
pure and CPU-bound: callers on the event loop run it via ``asyncio.to_thread``.
"""

from __future__ import annotations

import io

from PIL import Image

MAX_LONG_EDGE = 1600
JPEG_QUALITY = 85


def to_share_jpeg(png_bytes: bytes) -> bytes:
    """Re-encode an image as an RGB progressive JPEG (quality 85, optimized) whose
    long edge is at most ``MAX_LONG_EDGE`` (LANCZOS downscale, never an upscale).

    No EXIF, ICC profile or other metadata is carried over; transparency is
    flattened onto white. Raises on bytes that are not a decodable image."""
    with Image.open(io.BytesIO(png_bytes)) as src:
        src.load()
        img = src
        if img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            rgba = img.convert("RGBA")
            img = Image.new("RGB", rgba.size, (255, 255, 255))
            img.paste(rgba, mask=rgba.getchannel("A"))
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > MAX_LONG_EDGE:
            # thumbnail keeps the aspect ratio and only ever shrinks.
            img.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        # Explicit args only: no exif= / icc_profile=, so no metadata is written.
        img.save(
            out,
            format="JPEG",
            quality=JPEG_QUALITY,
            progressive=True,
            optimize=True,
        )
        return out.getvalue()
