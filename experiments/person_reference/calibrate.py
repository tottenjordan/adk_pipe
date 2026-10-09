"""Person-reference calibration spike (PR 0 of docs/plans/2026-10-09-person-reference.md).

Renders a 3 style x 2 framing grid of generic product-ad concepts per consented
test photo on the pipeline's image model, each concept twice (with
``ImageConfig(person_generation="ALLOW_ADULT")`` and without), and records per
render whether it was blocked (``prompt_feedback.block_reason``, a block-like
``finish_reason``, or no image part) plus a vision-model likeness verdict (same
person as the photo? yes/no + reason, on the image-QA model).

Pure core (``build_grid``, ``block_reason``, ``summarise``, ``summary_markdown``,
``photo_uri_errors``) is unit-tested in ``tests/test_person_calibration.py``; the
render loop is the live path (never run in tests or CI).

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.person_reference.calibrate \\
        --photos gs://$BUCKET/person-refs/<slug>/a.jpg gs://$BUCKET/person-refs/<slug>/b.jpg \\
        [--out experiments/person_reference/results] [--pace 31] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from creative_agent.image_tools import REFERENCE_IGNORE_TEXT_LINE, _final_image_part

# The three photographic STYLE_PALETTE families (exact canonical names), the
# only ones where a real person's likeness is meaningful.
STYLES: tuple[str, ...] = (
    "Candid 35mm film photo",
    "Photoreal / editorial",
    "Cinematic film still",
)

FRAMINGS: tuple[str, ...] = ("close", "mid")
ASPECT_RATIO = "4:5"
DEFAULT_OUT = Path(__file__).parent / "results"
DEFAULT_PACE_SECS = 31.0  # image model ~2 RPM, project-wide
PERSON_REFS_PREFIX = "person-refs/"

# Go/no-go rule from the plan (Task 0.2).
SAFE_STYLE_MIN_LIKENESS = 0.7
GO_MIN_STYLE_LIKENESS = 0.6
GO_MAX_BLOCK_RATE = 0.2

_STYLE_CUES = {
    "Candid 35mm film photo": (
        "Candid, unposed moment, natural window light, visible film grain, "
        "slightly warm colour."
    ),
    "Photoreal / editorial": (
        "Polished magazine portrait, soft key light, shallow depth of field, "
        "clean colour grade."
    ),
    "Cinematic film still": (
        "Widescreen-movie mood, motivated practical lighting, teal-and-amber "
        "grade, subtle anamorphic bokeh."
    ),
}
_FRAMING_CUES = {
    "close": (
        "Hero close-up: head and shoulders fill the frame, face sharp and "
        "clearly visible, the cup raised near the chin."
    ),
    "mid": (
        "Mid shot from the waist up, seated at a café table, face clearly "
        "visible, some of the café around them."
    ),
}
PERSON_REFERENCE_LINE = (
    "Reference image 1 (person): this is the person to feature. Keep their face, "
    "hair, skin tone and build recognisably the same; do not copy the photo's "
    "background, clothing or pose."
)

# finish_reason values that mean a safety/policy block, even with an image part.
_HARD_BLOCK_FINISH = frozenset(
    {
        "SAFETY",
        "IMAGE_SAFETY",
        "PROHIBITED_CONTENT",
        "IMAGE_PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
        "RECITATION",
        "IMAGE_RECITATION",
    }
)
# Opaque finish reasons that read as a block only when no image came back.
_SOFT_BLOCK_FINISH = frozenset({"OTHER", "IMAGE_OTHER", "NO_IMAGE"})

LIKENESS_INSTRUCTION = (
    "The first image is a reference photo of a real person. The second image is "
    "an advertising image that was asked to feature that same person. Decide "
    "whether the main person in the second image is recognisably the same "
    "individual as in the reference photo (face shape, features, hair, skin "
    "tone), ignoring clothing, pose, lighting and art style. Answer "
    "same_person=true only if a friend of the person would recognise them. Give "
    "a one-sentence reason."
)


class LikenessVerdict(BaseModel):
    """The vision model's same-person verdict."""

    same_person: bool
    reason: str


@dataclass(frozen=True)
class GridItem:
    style: str
    framing: str
    photo_uri: str
    prompt: str


def concept_prompt(style: str, framing: str) -> str:
    """The brace-free concept prompt for one (style, framing) cell — pure."""
    return (
        f"{style}. An advertisement for a neighbourhood coffee brand: the person "
        "in the person reference image is the hero, holding a ceramic coffee cup "
        "in a cosy sunlit café, relaxed and smiling. "
        f"{_FRAMING_CUES[framing]} {_STYLE_CUES[style]} "
        "No text, no captions, no logos."
    )


def build_grid(photos: Sequence[str]) -> list[GridItem]:
    """Every (photo, style, framing) cell → a ``GridItem`` (3 x 2 per photo)."""
    return [
        GridItem(style, framing, photo, concept_prompt(style, framing))
        for photo in photos
        for style in STYLES
        for framing in FRAMINGS
    ]


def _enum_name(value: Any) -> str:
    return str(getattr(value, "name", value) or "")


def block_reason(response: Any) -> str | None:
    """Why a render produced no usable image, or None when it has one.

    ``"prompt:<reason>"`` (input filter), ``"finish:<reason>"`` (a safety
    finish reason, or an opaque one with no image), ``"no_image"``.
    Works on genai ``GenerateContentResponse`` objects and duck-typed fakes.
    """
    feedback = getattr(response, "prompt_feedback", None)
    prompt_block = _enum_name(getattr(feedback, "block_reason", None))
    if prompt_block:
        return f"prompt:{prompt_block}"
    candidates = getattr(response, "candidates", None) or []
    first = candidates[0] if candidates else None
    finish = _enum_name(getattr(first, "finish_reason", None))
    if finish in _HARD_BLOCK_FINISH:
        return f"finish:{finish}"
    content = getattr(first, "content", None)
    parts = getattr(content, "parts", None) or []
    if _final_image_part(parts) is not None:
        return None
    if finish in _SOFT_BLOCK_FINISH:
        return f"finish:{finish}"
    return "no_image"


def _rate(values: Iterable[bool]) -> float | None:
    vals = list(values)
    return round(sum(vals) / len(vals), 4) if vals else None


def _likeness_by(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, float]:
    groups: dict[str, list[bool]] = {}
    for row in rows:
        if isinstance(row.get("likeness"), bool):
            groups.setdefault(str(row.get(key, "")), []).append(row["likeness"])
    return {k: r for k, v in groups.items() if (r := _rate(v)) is not None}


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate per-render rows → likeness/block rates and the go/no-go call.

    Rows with an ``error`` (API exception, ALLOW_ADULT rejected) count in ``n``
    and ``errors`` but not in any rate. Every block is a filter false positive:
    the photos are ordinary consented adult photos.
    """
    rendered = [r for r in rows if not r.get("error")]

    def blocked(subset: Iterable[Mapping[str, Any]]) -> list[bool]:
        return [bool(r.get("block_reason")) for r in subset]

    by_style = _likeness_by(rendered, "style")
    block_rate = {
        "overall": _rate(blocked(rendered)),
        "with_allow_adult": _rate(
            blocked(r for r in rendered if r.get("allow_adult") is True)
        ),
        "without_allow_adult": _rate(
            blocked(r for r in rendered if not r.get("allow_adult"))
        ),
    }
    if not by_style:
        decision = "inconclusive"
    elif max(by_style.values()) < GO_MIN_STYLE_LIKENESS or (
        (block_rate["overall"] or 0.0) > GO_MAX_BLOCK_RATE
    ):
        decision = "no-go"
    else:
        decision = "go"
    return {
        "n": len(rows),
        "errors": len(rows) - len(rendered),
        "allow_adult_unsupported": any(
            str(r.get("error", "")).startswith("allow_adult_unsupported") for r in rows
        ),
        "likeness_overall": _rate(
            r["likeness"] for r in rendered if isinstance(r.get("likeness"), bool)
        ),
        "likeness_by_style": by_style,
        "likeness_by_framing": _likeness_by(rendered, "framing"),
        "block_rate": block_rate,
        "false_positive_blocks": sum(blocked(rendered)),
        "block_reasons": sorted(
            {str(r["block_reason"]) for r in rendered if r.get("block_reason")}
        ),
        "person_safe_styles": sorted(
            s for s, rate in by_style.items() if rate >= SAFE_STYLE_MIN_LIKENESS
        ),
        "decision": decision,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def summary_markdown(summary: Mapping[str, Any]) -> str:
    """The committable summary (no photo URIs or person identifiers) — pure."""
    lines = [
        "# Person reference calibration summary",
        "",
        f"- Renders: {summary['n']} (errors: {summary['errors']})",
        f"- ALLOW_ADULT unsupported: {summary['allow_adult_unsupported']}",
        f"- Likeness overall: {_pct(summary['likeness_overall'])}",
        "",
        "| Style | Likeness |",
        "|---|---|",
        *(f"| {k} | {_pct(v)} |" for k, v in summary["likeness_by_style"].items()),
        "",
        "| Framing | Likeness |",
        "|---|---|",
        *(f"| {k} | {_pct(v)} |" for k, v in summary["likeness_by_framing"].items()),
        "",
        "| Block rate | |",
        "|---|---|",
        *(f"| {k} | {_pct(v)} |" for k, v in summary["block_rate"].items()),
        "",
        f"- Filter false positives (blocked renders): "
        f"{summary['false_positive_blocks']}",
        f"- Block reasons: {', '.join(summary['block_reasons']) or 'none'}",
        f"- Person-safe styles (>= {SAFE_STYLE_MIN_LIKENESS:.0%} likeness): "
        f"{', '.join(summary['person_safe_styles']) or 'none'}",
        f"- **Decision (rule of thumb): {summary['decision']}**",
        "",
    ]
    return "\n".join(lines)


def photo_uri_errors(photos: Sequence[str], bucket: str | None) -> list[str]:
    """Messages for photo URIs not under ``gs://<bucket>/person-refs/`` — pure."""
    if not bucket:
        return ["GOOGLE_CLOUD_STORAGE_BUCKET is not set"]
    prefix = f"gs://{bucket}/{PERSON_REFS_PREFIX}"
    return [
        f"{uri!r} is not an object under {prefix}"
        for uri in photos
        if not (uri.startswith(prefix) and len(uri) > len(prefix)) or uri.endswith("/")
    ]


# --- live path (never exercised by tests) -------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _render_config(allow_adult: bool) -> Any:
    from google.genai import types

    from creative_agent.config import config

    image_config = types.ImageConfig(
        aspect_ratio=ASPECT_RATIO, image_size=config.image_size
    )
    if allow_adult:
        image_config.person_generation = "ALLOW_ADULT"
    return types.GenerateContentConfig(
        response_modalities=["IMAGE"], image_config=image_config
    )


def _likeness(photo_part: Any, image: bytes, mime: str) -> LikenessVerdict:
    from google.genai import types

    from creative_agent import image_qa
    from creative_agent.config import config

    response = image_qa._get_qa_client().models.generate_content(
        model=config.image_qa_model,
        contents=[
            photo_part,
            types.Part.from_bytes(data=image, mime_type=mime),
            types.Part.from_text(text=LIKENESS_INSTRUCTION),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=LikenessVerdict,
            temperature=0,
        ),
    )
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, LikenessVerdict):
        return parsed
    return LikenessVerdict.model_validate_json(response.text or "")


async def run(grid: Sequence[GridItem], out: Path, pace: float) -> list[dict]:
    from google.genai import types

    from creative_agent.config import config
    from creative_agent.image_tools import (
        _fetch_reference_image,
        _generate_image_with_backoff,
    )

    out.mkdir(parents=True, exist_ok=True)
    photo_parts = {}
    for i, uri in enumerate(dict.fromkeys(g.photo_uri for g in grid), start=1):
        part = _fetch_reference_image(uri)
        if part is None:
            sys.exit(f"Could not fetch photo {uri}")
        photo_parts[uri] = (f"photo{i}", part)

    allow_adult_supported = True
    rows: list[dict] = []
    renders = 0
    for item in grid:
        label, photo_part = photo_parts[item.photo_uri]
        for allow_adult in (True, False):
            row: dict[str, Any] = {
                **asdict(item),
                "photo": label,
                "allow_adult": allow_adult,
                "block_reason": None,
                "error": "",
                "image_path": "",
                "likeness": None,
                "likeness_reason": "",
            }
            rows.append(row)
            if allow_adult and not allow_adult_supported:
                row["error"] = "allow_adult_unsupported"
                continue
            if renders:
                await asyncio.sleep(pace)
            renders += 1
            text = "\n\n".join(
                [item.prompt, PERSON_REFERENCE_LINE, REFERENCE_IGNORE_TEXT_LINE]
            )
            try:
                response = await _generate_image_with_backoff(
                    model=config.image_gen_model,
                    contents=[types.Part.from_text(text=text), photo_part],
                    config=_render_config(allow_adult),
                )
            except Exception as exc:
                if allow_adult:
                    allow_adult_supported = False
                    row["error"] = f"allow_adult_unsupported: {exc}"
                else:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                logging.warning(f"Render failed ({row['error']})")
                continue
            row["block_reason"] = block_reason(response)
            print(
                f"[{len(rows)}/{2 * len(grid)}] {item.style} / {item.framing} / "
                f"{label} / allow_adult={allow_adult}: "
                f"{row['block_reason'] or 'image'}"
            )
            if row["block_reason"]:
                continue
            part = _final_image_part(response.candidates[0].content.parts)
            mime = part.inline_data.mime_type or "image/png"
            image = part.inline_data.data
            name = (
                f"{len(rows):02d}_{_slug(item.style)}_{item.framing}_{label}_"
                f"{'adult' if allow_adult else 'default'}.{mime.split('/')[-1]}"
            )
            (out / name).write_bytes(image)
            row["image_path"] = name
            try:
                verdict = await asyncio.to_thread(_likeness, photo_part, image, mime)
                row["likeness"] = verdict.same_person
                row["likeness_reason"] = verdict.reason
            except Exception as exc:
                logging.warning(f"Likeness check failed: {exc}")
                row["likeness_reason"] = f"check failed: {exc}"
    return rows


def write_results(rows: Sequence[Mapping[str, Any]], out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    summary = summarise(rows)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "summary.md").write_text(summary_markdown(summary))
    return summary


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--photos", nargs="+", required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--pace", type=float, default=DEFAULT_PACE_SECS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from creative_agent.config import config

    errors = photo_uri_errors(args.photos, config.GCS_BUCKET_NAME)
    if errors:
        sys.exit(
            "Photos must be consented test photos under "
            f"gs://<GOOGLE_CLOUD_STORAGE_BUCKET>/{PERSON_REFS_PREFIX}:\n  "
            + "\n  ".join(errors)
        )
    grid = build_grid(args.photos)
    if args.dry_run:
        for item in grid:
            print(f"{item.style} | {item.framing} | {item.photo_uri}\n  {item.prompt}")
        print(f"{len(grid)} concepts x 2 renders = {2 * len(grid)} renders")
        return
    rows = asyncio.run(run(grid, args.out, args.pace))
    summary = write_results(rows, args.out)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
