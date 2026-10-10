"""runserver/share_images.py: the share-image re-encode (PNG → ≤1600 px JPEG)."""

from __future__ import annotations

import io
import random

import pytest
from PIL import Image

from runserver.share_images import MAX_LONG_EDGE, to_share_jpeg


def _png(size: tuple[int, int], mode: str = "RGB", noisy: bool = False) -> bytes:
    img = Image.new("RGB", size, "red")
    if noisy:
        # Photographic-ish content (smooth noise + grain): PNG compresses it
        # poorly, like a real render.
        w, h = size
        rng = random.Random(7)
        img = Image.frombytes("RGB", (w // 8, h // 8), rng.randbytes(w * h * 3 // 64))
        img = img.resize(size, Image.Resampling.BICUBIC)
        grain = Image.frombytes("L", size, rng.randbytes(w * h)).convert("RGB")
        img = Image.blend(img, grain, 0.08)
    buf = io.BytesIO()
    img.convert(mode).save(buf, format="PNG")
    return buf.getvalue()


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def test_output_is_jpeg_with_long_edge_capped_and_aspect_preserved():
    out = _open(to_share_jpeg(_png((1536, 2752))))  # 9:16-ish 2K render
    assert out.format == "JPEG" and out.mode == "RGB"
    assert max(out.size) == MAX_LONG_EDGE == 1600
    assert out.size[0] / out.size[1] == pytest.approx(1536 / 2752, rel=0.01)


def test_landscape_long_edge_is_width():
    out = _open(to_share_jpeg(_png((2400, 1200))))
    assert out.size == (1600, 800)


def test_small_input_is_not_upscaled():
    out = _open(to_share_jpeg(_png((640, 480))))
    assert out.size == (640, 480)


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "L"])
def test_non_rgb_inputs_are_converted(mode):
    out = _open(to_share_jpeg(_png((300, 200), mode=mode)))
    assert out.format == "JPEG" and out.mode == "RGB" and out.size == (300, 200)


def test_transparent_pixels_become_white_not_black():
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out = _open(to_share_jpeg(buf.getvalue()))
    r, g, b = out.getpixel((5, 5))
    assert min(r, g, b) > 240


def test_metadata_is_stripped():
    img = Image.new("RGB", (100, 100), "blue")
    exif = Image.Exif()
    exif[0x010E] = "secret prompt"  # ImageDescription
    buf = io.BytesIO()
    img.save(buf, format="PNG", exif=exif.tobytes())
    data = to_share_jpeg(buf.getvalue())
    out = _open(data)
    assert not out.getexif()
    assert "exif" not in out.info and "icc_profile" not in out.info
    assert b"secret prompt" not in data


def test_progressive_jpeg():
    out = _open(to_share_jpeg(_png((800, 600))))
    assert out.info.get("progressive") or out.info.get("progression")


def test_2k_render_shrinks_substantially():
    src = _png((2048, 2048), noisy=True)
    out = to_share_jpeg(src)
    print(f"share jpeg: {len(src)} -> {len(out)} bytes ({len(out) / len(src):.1%})")
    assert len(out) < len(src) / 4


def test_rejects_non_image_bytes():
    with pytest.raises(Exception):  # noqa: B017
        to_share_jpeg(b"not an image")
