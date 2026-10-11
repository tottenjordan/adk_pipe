"""Person swap spike: re-generate vs edit (PR 0 of docs/plans/2026-10-11-faithful-person-swaps.md).

For each (concept, photo, mode, repeat) renders a personalised variant of a
finished concept and compares it with the run's original render:

- **regenerate** — today's variant path: ``runserver.variants.variant_concept``
  through ``creative_agent.render_concept.render_concept`` (the concept's text
  prompt + the person photo, ``ALLOW_ADULT``, QA off, no person-less fallback).
- **edit** — the prototype ``edit_contents``: the original render + the person
  photo with ``EDIT_INSTRUCTION`` ("replace only the person"), one edit from the
  original, ``ALLOW_ADULT``, the aspect ratio read from the original's size.

Each output gets one comparison vision call on the image-QA model (temperature
0; original + variant → ``{same_layout, same_text, same_graphics,
same_product, only_person_changed, differences}``) and the image check's own
likeness verdict (``image_qa.inspect_image(..., person_image=)`` →
``person_likeness``). Results: ``results.csv`` + ``summary.json`` +
``summary.md`` (rates per mode and photo, go/no-go) under ``--out``.

Pure core (``parse_comparison``, ``summarise``, ``decision``,
``summary_markdown``, ``nearest_aspect_ratio``, ``edit_contents``) is
unit-tested in ``tests/test_swap_compare.py``; the render loop is live only.

Usage (session JSON = the api's ``GET /apps/{app}/users/{u}/sessions/{id}``):
    PYTHONPATH="$PWD" uv run python -m experiments.person_reference.swap_compare \\
        --sessions s1.json s2.json --concepts "Concept A" "Concept B" \\
        --photos gs://$BUCKET/person-refs/<slug>/a.jpg gs://$BUCKET/person-refs/<slug>/b.jpg \\
        [--n 2] [--modes regenerate edit] [--out experiments/person_reference/results/swap] \\
        [--pace 31]

Photos are labelled photo1, photo2 … (in the order given) in every output, and
``summary.md`` never contains a URI. Delete the renders and the CSV afterwards
(real people).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from google.genai import types
from pydantic import BaseModel

# The edit request moved to creative_agent.person_edit (PR 1); re-exported
# here for the spike's tests and live loop.
from creative_agent.person_edit import (  # noqa: F401
    BASE_LABEL,
    EDIT_INSTRUCTION,
    PERSON_LABEL,
    edit_contents,
    nearest_aspect_ratio,
)

MODES: tuple[str, ...] = ("regenerate", "edit")
DEFAULT_OUT = Path(__file__).parent / "results" / "swap"
DEFAULT_PACE_SECS = 31.0  # image model ~2 images/min, project-wide

# Go/no-go rule (plan Task 0.1): edit must be clearly more faithful, and still
# look like the person.
GO_MIN_FIDELITY_GAIN = 0.3
GO_MIN_EDIT_LIKENESS = 0.8

SAME_KEYS: tuple[str, ...] = (
    "same_layout",
    "same_text",
    "same_graphics",
    "same_product",
)
COMPARISON_KEYS: tuple[str, ...] = (*SAME_KEYS, "only_person_changed", "differences")

COMPARISON_INSTRUCTION = (
    "Image 1 is an original advertising image. Image 2 is a variant that was "
    "asked to replace only the main person with a different person and keep "
    "everything else identical. Compare them carefully, ignoring the person's "
    "identity (face, hair, skin tone, body). same_layout: the composition, "
    "framing, camera angle and the placement of the main elements are the same. "
    "same_text: every word of in-image text is present, spelled the same and in "
    "the same place, and no text was added or duplicated. same_graphics: every "
    "graphic element (arrows, labels, icons, overlays, props) is present with "
    "the same count and direction. same_product: the product is present and "
    "looks the same. only_person_changed: true only when the person is the sole "
    "difference. differences: concrete differences other than the person's "
    "identity, one short statement each (e.g. 'arrow points left instead of "
    "right', '6 flags instead of 2'); empty if none."
)


class Comparison(BaseModel):
    """The comparison call's structured verdict."""

    same_layout: bool
    same_text: bool
    same_graphics: bool
    same_product: bool
    only_person_changed: bool
    differences: list[str]


def parse_comparison(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    """A comparison payload (JSON text or dict) → every key, never raises.

    Missing / non-bool flags read ``False`` (unknown is not faithful); a bare
    string ``differences`` becomes a one-item list.
    """
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
    if not isinstance(data, Mapping):
        data = {}
    out: dict[str, Any] = {
        key: data.get(key) is True for key in (*SAME_KEYS, "only_person_changed")
    }
    diffs = data.get("differences")
    if isinstance(diffs, str):
        diffs = [diffs]
    out["differences"] = [str(d) for d in diffs or [] if str(d).strip()]
    return out


def _rate(values: Iterable[bool]) -> float | None:
    vals = list(values)
    return round(sum(vals) / len(vals), 4) if vals else None


def _faithful(row: Mapping[str, Any]) -> bool:
    return all(row.get(key) is True for key in SAME_KEYS)


def _block(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if not r.get("error")]
    return {
        "n": len(rows),
        "errors": len(rows) - len(ok),
        "fidelity_rate": _rate(_faithful(r) for r in ok),
        "only_person_changed_rate": _rate(
            r.get("only_person_changed") is True for r in ok
        ),
        "likeness_rate": _rate(
            r["likeness"] for r in ok if isinstance(r.get("likeness"), bool)
        ),
    }


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-mode (and per mode × photo) rates — pure.

    ``fidelity_rate`` = share of rendered outputs with every ``same_*`` flag
    true; ``likeness_rate`` = share with a ``True`` likeness verdict (unknown
    verdicts excluded). Rows with an ``error`` (blocked / failed render) count
    in ``n`` and ``errors`` only.
    """
    modes = list(dict.fromkeys(str(r.get("mode", "")) for r in rows))
    summary: dict[str, Any] = {"modes": {}, "by_photo": {}}
    for mode in modes:
        subset = [r for r in rows if r.get("mode") == mode]
        summary["modes"][mode] = _block(subset)
        photos = list(dict.fromkeys(str(r.get("photo", "")) for r in subset))
        summary["by_photo"][mode] = {
            p: _block([r for r in subset if r.get("photo") == p]) for p in photos
        }
    return summary


def decision(summary: Mapping[str, Any]) -> str:
    """``go`` when edit fidelity ≥ regenerate + 0.3 and edit likeness ≥ 0.8."""
    modes = summary.get("modes", {})
    edit = modes.get("edit", {})
    regen = modes.get("regenerate", {})
    edit_fid = edit.get("fidelity_rate")
    edit_like = edit.get("likeness_rate")
    regen_fid = regen.get("fidelity_rate") or 0.0
    if edit_fid is None or edit_like is None:
        return "no-go"
    gain_ok = edit_fid + 1e-9 >= regen_fid + GO_MIN_FIDELITY_GAIN
    like_ok = edit_like + 1e-9 >= GO_MIN_EDIT_LIKENESS
    return "go" if gain_ok and like_ok else "no-go"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _table_row(label: str, block: Mapping[str, Any]) -> str:
    return (
        f"| {label} | {block['n']} | {block['errors']} | "
        f"{_pct(block['fidelity_rate'])} | "
        f"{_pct(block['only_person_changed_rate'])} | "
        f"{_pct(block['likeness_rate'])} |"
    )


def summary_markdown(summary: Mapping[str, Any]) -> str:
    """The committable summary (rates only: no URIs, names or images) — pure."""
    header = [
        "| {} | Renders | Errors | Fidelity | Only person changed | Likeness |",
        "|---|---|---|---|---|---|",
    ]
    lines = [
        "# Person swap: edit vs regenerate",
        "",
        "Fidelity = layout, text, graphics and product all unchanged (vision "
        "comparison with the original). Likeness = the image check's "
        "same-person verdict.",
        "",
        header[0].format("Mode"),
        header[1],
        *(_table_row(m, b) for m, b in summary["modes"].items()),
        "",
        header[0].format("Mode / photo"),
        header[1],
        *(
            _table_row(f"{m} / {p}", b)
            for m, photos in summary["by_photo"].items()
            for p, b in photos.items()
        ),
        "",
        f"Rule: go when edit fidelity >= regenerate + {GO_MIN_FIDELITY_GAIN:.0%} "
        f"and edit likeness >= {GO_MIN_EDIT_LIKENESS:.0%}.",
        "",
        f"**Decision: {summary.get('decision', decision(summary))}**",
        "",
    ]
    return "\n".join(lines)


# --- live path (never exercised by tests) -------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _load_sessions(paths: Sequence[Path]) -> list[dict]:
    """Session JSONs → their ``state`` dicts (a bare state dict is accepted)."""
    states = []
    for path in paths:
        data = json.loads(path.read_text())
        states.append(data.get("state", data) if isinstance(data, dict) else {})
    return states


def _locate(states: Sequence[Mapping[str, Any]], name: str):
    from runserver.variants import find_concept

    for state in states:
        concept = find_concept(state, name)
        images = state.get("generated_images") or {}
        if isinstance(images, str):
            images = json.loads(images)
        uri = (images.get(name) or {}).get("gcs_uri") if concept else None
        if concept and uri:
            return state, concept, uri
    sys.exit(f"Concept {name!r} (with a rendered image) not found in the sessions")


def _download(uri: str) -> bytes:
    from creative_agent.image_tools import _download_blob

    bucket, _, obj = uri.removeprefix("gs://").partition("/")
    return _download_blob(bucket, obj)


def _image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return img.size


def _compare(base: bytes, base_mime: str, variant: bytes, mime: str) -> dict:
    from creative_agent import image_qa
    from creative_agent.config import config

    response = image_qa._get_qa_client().models.generate_content(
        model=config.image_qa_model,
        contents=[
            types.Part.from_text(text="Image 1 (original):"),
            types.Part.from_bytes(data=base, mime_type=base_mime),
            types.Part.from_text(text="Image 2 (variant):"),
            types.Part.from_bytes(data=variant, mime_type=mime),
            types.Part.from_text(text=COMPARISON_INSTRUCTION),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Comparison,
            temperature=0,
        ),
    )
    return parse_comparison(response.text or "")


def _likeness(
    variant: bytes, mime: str, concept: dict, state: Mapping[str, Any], photo
) -> tuple[bool | None, list[str]]:
    """The image check's verdict on the variant: (person_likeness, failed rules)."""
    from creative_agent import image_qa
    from creative_agent.config import config

    target_product = str(state.get("target_product") or "")
    result = image_qa.inspect_image(
        variant,
        mime,
        concept,
        brand=str(state.get("brand") or ""),
        target_product=target_product,
        client=image_qa._get_qa_client(),
        model=config.image_qa_model,
        person_image=photo,
    )
    failed = image_qa.qa_failed_rules(result, concept, target_product=target_product)
    return result.person_likeness, failed


async def _regenerate(state, concept, person_part) -> tuple[bytes, str] | str:
    from creative_agent import image_tools
    from creative_agent.config import config
    from creative_agent.rating_signals import strictness_flags
    from creative_agent.references import resolve_references
    from creative_agent.render_concept import fetch_references, render_concept
    from runserver.variants import _personal_uri, variant_concept

    refs = [r for r in resolve_references(state) if not _personal_uri(r[0])]
    references = await fetch_references(refs)
    aspect_ratio = image_tools._resolve_aspect_ratio(
        concept,
        image_tools.valid_aspect_ratio_override(state.get("visual_aspect_ratio")),
        config.image_aspect_ratios_allowed,
        config.image_aspect_ratio_default,
    )
    result = await render_concept(
        variant_concept(concept),
        aspect_ratio=aspect_ratio,
        references=references,
        person=person_part,
        strictness=strictness_flags(state.get("rating_strictness")),
        qa=False,
        brand=str(state.get("brand") or ""),
        target_product=str(state.get("target_product") or ""),
        fallback_without_person=False,
    )
    if result.rejected_reason:
        return f"rejected:{result.rejected_reason}"
    return result.rendered or "no_image"


async def _edit(state, base_part, base_size, person_part) -> tuple[bytes, str] | str:
    from creative_agent import image_tools
    from creative_agent.config import config
    from creative_agent.rating_signals import strictness_flags

    width, height = base_size
    aspect_ratio = nearest_aspect_ratio(
        width,
        height,
        config.image_aspect_ratios_allowed,
        config.image_aspect_ratio_default,
    )
    contents = edit_contents(
        base_part,
        person_part,
        brand=str(state.get("brand") or ""),
        strictness=strictness_flags(state.get("rating_strictness")),
    )
    rendered, reason = await image_tools._render_person_image(contents, aspect_ratio)
    return rendered if rendered is not None else f"rejected:{reason}"


async def run(
    states: Sequence[Mapping[str, Any]],
    concepts: Sequence[str],
    photos: Sequence[str],
    *,
    modes: Sequence[str],
    n: int,
    out: Path,
    pace: float,
) -> list[dict]:
    from creative_agent.render_concept import fetch_person_photo, person_image_of
    from runserver.variants import variant_concept

    out.mkdir(parents=True, exist_ok=True)
    photo_parts = []
    for i, uri in enumerate(photos, start=1):
        part = await fetch_person_photo(uri)
        if part is None:
            sys.exit(f"Could not fetch photo{i}")
        photo_parts.append((f"photo{i}", part))

    total = len(concepts) * len(photos) * len(modes) * n
    rows: list[dict] = []
    renders = 0
    for name in concepts:
        state, concept, base_uri = _locate(states, name)
        base = await asyncio.to_thread(_download, base_uri)
        base_mime = "image/png" if base_uri.lower().endswith(".png") else "image/jpeg"
        base_part = types.Part.from_bytes(data=base, mime_type=base_mime)
        base_size = _image_size(base)
        (out / f"base_{_slug(name)}.{base_mime.split('/')[-1]}").write_bytes(base)
        qa_concept = variant_concept(concept)
        for label, person_part in photo_parts:
            photo = person_image_of(person_part)
            for mode in modes:
                for rep in range(1, n + 1):
                    if renders:
                        await asyncio.sleep(pace)
                    renders += 1
                    row: dict[str, Any] = {
                        "concept": name,
                        "photo": label,
                        "mode": mode,
                        "rep": rep,
                        "error": "",
                        "seconds": 0.0,
                        "image_path": "",
                        **{k: None for k in COMPARISON_KEYS},
                        "likeness": None,
                        "qa_failed": "",
                    }
                    rows.append(row)
                    started = time.monotonic()
                    try:
                        if mode == "edit":
                            got = await _edit(state, base_part, base_size, person_part)
                        else:
                            got = await _regenerate(state, concept, person_part)
                    except Exception as exc:
                        got = f"{type(exc).__name__}: {exc}"
                    row["seconds"] = round(time.monotonic() - started, 1)
                    if isinstance(got, str):
                        row["error"] = got
                        print(f"[{renders}/{total}] {name} / {label} / {mode}: {got}")
                        continue
                    image, mime = got
                    file = (
                        f"{renders:02d}_{mode}_{_slug(name)}_{label}_{rep}."
                        f"{mime.split('/')[-1]}"
                    )
                    (out / file).write_bytes(image)
                    row["image_path"] = file
                    try:
                        cmp = await asyncio.to_thread(
                            _compare, base, base_mime, image, mime
                        )
                        row.update(cmp, differences="; ".join(cmp["differences"]))
                    except Exception as exc:
                        logging.warning(f"Comparison failed: {exc}")
                        row["differences"] = f"comparison failed: {exc}"
                    try:
                        like, failed = await asyncio.to_thread(
                            _likeness, image, mime, qa_concept, state, photo
                        )
                        row["likeness"] = like
                        row["qa_failed"] = "; ".join(failed)
                    except Exception as exc:
                        logging.warning(f"Likeness check failed: {exc}")
                    print(
                        f"[{renders}/{total}] {name} / {label} / {mode} #{rep}: "
                        f"faithful={_faithful(row)} likeness={row['likeness']} "
                        f"({row['seconds']}s)"
                    )
    return rows


def write_results(rows: Sequence[Mapping[str, Any]], out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    summary = summarise(rows)
    summary["decision"] = decision(summary)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "summary.md").write_text(summary_markdown(summary))
    return summary


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sessions", nargs="+", type=Path, required=True)
    parser.add_argument("--concepts", nargs="+", required=True)
    parser.add_argument("--photos", nargs="+", required=True)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--n", type=int, default=2)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--pace", type=float, default=DEFAULT_PACE_SECS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    states = _load_sessions(args.sessions)
    rows = asyncio.run(
        run(
            states,
            args.concepts,
            args.photos,
            modes=args.modes,
            n=args.n,
            out=args.out,
            pace=args.pace,
        )
    )
    summary = write_results(rows, args.out)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
