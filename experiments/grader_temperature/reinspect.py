"""Re-inspect each selected session's rendered images at several image-QA temperatures.

For every visual concept with a rendered image (``generated_images[c].gcs_uri``)
the image is downloaded once to ``data/img/`` (≤ 20 MB) and inspected
``--repeats`` times per temperature with the production inputs
(``creative_agent/image_tools.py`` ``generate_image`` / ``_inspect_qa``): brand,
target product, ``strictness_flags(state["rating_strictness"])``, the safer
angle (``safer_angle_active == "yes"``), a logo reference when the run had one,
and the consented person photo only for a cast concept
(``generated_images[c].cast``) — when that photo can't be read the concept is
skipped and noted. Each call → ``results/qa/<sid>/<concept-slug>/<t>-r<k>.json``
(the ``ImageQAResult`` dump + ``qa_failed_rules``); existing results are
skipped (resumable). A call that raises writes nothing. Person photo URIs are
never logged or stored.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.grader_temperature.reinspect \\
      --temps 0.0,1.0 --repeats 3
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import (
    IMG_DIR,
    QA_DIR,
    SELECTION_PATH,
    STATE_DIR,
    concept_slug,
    generated_images,
    load_selection,
    load_state,
    parse_temps,
    plan_order,
    run_filename,
    write_json,
)

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class Downloader:
    """``gs://`` reads through an injectable ``storage.Client`` (size-capped)."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def __call__(self, uri: str, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
        if not uri.startswith("gs://"):
            raise ValueError("not a gs:// URI")
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client()
        bucket, _, name = uri.removeprefix("gs://").partition("/")
        blob = self._client.bucket(bucket).blob(name)
        blob.reload()
        if blob.size is not None and blob.size > max_bytes:
            raise ValueError(f"object larger than {max_bytes} bytes")
        data = blob.download_as_bytes()
        if not data or len(data) > max_bytes:
            raise ValueError("empty or too large")
        return data


def qa_context(state: Mapping[str, Any]) -> dict[str, Any]:
    """The run-level image-QA inputs, as production derives them."""
    from creative_agent.rating_signals import strictness_flags
    from creative_agent.references import resolve_references

    return {
        "brand": state.get("brand") or "",
        "target_product": state.get("target_product") or "",
        "strictness": strictness_flags(state.get("rating_strictness")),
        "safer_angle_active": state.get("safer_angle_active") == "yes",
        # Production: a logo reference that was fetched; offline: one configured.
        "has_logo_reference": any(
            role == "logo" for _, role in resolve_references(state)
        ),
    }


def qa_targets(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per rendered visual concept: ``{concept, uri, mime, cast}`` (state order)."""
    from creative_eval.agent import _state_items
    from creative_eval.evaluate import image_mime_type

    images = generated_images(state)
    out = []
    for concept in _state_items(state, "final_visual_concepts", "visual_concepts"):
        name = concept.get("concept_name")
        record = images.get(name) if isinstance(name, str) else None
        uri = record.get("gcs_uri") if isinstance(record, Mapping) else None
        if not isinstance(uri, str) or not uri.startswith("gs://"):
            continue
        out.append(
            {
                "concept": concept,
                "uri": uri,
                "mime": image_mime_type(uri),
                "cast": isinstance(record, Mapping) and record.get("cast") is True,
            }
        )
    return out


def person_photo(
    state: Mapping[str, Any], download: Callable[[str], bytes]
) -> tuple[bytes, str] | None:
    """The run's consented person photo ``(bytes, mime)``, or None (never logs it)."""
    from creative_agent.person_render import person_reference_uri
    from creative_eval.evaluate import image_mime_type

    uri = person_reference_uri(state.get("person_reference"))
    if not uri:
        return None
    try:
        return download(uri), image_mime_type(uri)
    except Exception as exc:
        logger.warning("Person photo unavailable (%s)", type(exc).__name__)
        return None


def _cached_image(
    sid: str, target: Mapping[str, Any], img_dir: Path, download: Callable[[str], bytes]
) -> bytes:
    ext = Path(target["uri"]).suffix or ".png"
    path = img_dir / sid / f"{concept_slug(target['concept']['concept_name'])}{ext}"
    if path.exists():
        return path.read_bytes()
    data = download(target["uri"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def run(
    session_ids: Sequence[str],
    temps: Sequence[float],
    repeats: int,
    *,
    state_dir: Path = STATE_DIR,
    img_dir: Path = IMG_DIR,
    out_dir: Path = QA_DIR,
    download: Callable[[str], bytes] | None = None,
    inspect_fn: Callable[..., Any] | None = None,
    failed_rules: Callable[..., list[str]] | None = None,
    qa_client: Any = None,
    model: str | None = None,
) -> dict[str, list]:
    """Inspect every planned (session, concept, temperature, repeat) not on disk.

    Returns ``{written, skipped, errors, notes}`` (``notes``: concepts skipped
    for a missing image or person photo, sessions without cached state).
    """
    if inspect_fn is None or failed_rules is None:
        from creative_agent import image_qa

        inspect_fn = inspect_fn or image_qa.inspect_image
        failed_rules = failed_rules or image_qa.qa_failed_rules
    if model is None:
        from creative_agent.config import config

        model = config.image_qa_model
    download = download or Downloader()
    report: dict[str, list] = {"written": [], "skipped": [], "errors": [], "notes": []}

    for sid in session_ids:
        state = load_state(sid, state_dir)
        if state is None:
            report["notes"].append({"session_id": sid, "note": "no cached state"})
            continue
        ctx = qa_context(state)
        targets = qa_targets(state)
        photo: tuple[bytes, str] | None = None
        if any(t["cast"] for t in targets):
            photo = person_photo(state, download)
        for target in targets:
            name = target["concept"]["concept_name"]
            slug_dir = out_dir / sid / concept_slug(name)
            if target["cast"] and photo is None:
                report["notes"].append(
                    {
                        "session_id": sid,
                        "concept_name": name,
                        "note": "person photo unreadable; skipped",
                    }
                )
                continue
            pending = [
                (t, k)
                for _, t, k in plan_order([sid], temps, repeats)
                if not (slug_dir / run_filename(t, k)).exists()
            ]
            report["skipped"] += [
                str(slug_dir / run_filename(t, k))
                for _, t, k in plan_order([sid], temps, repeats)
                if (t, k) not in pending
            ]
            if not pending:
                continue
            try:
                image = _cached_image(sid, target, img_dir, download)
            except Exception as exc:
                report["notes"].append(
                    {
                        "session_id": sid,
                        "concept_name": name,
                        "note": f"image unreadable: {exc}",
                    }
                )
                continue
            if qa_client is None:
                from creative_agent.image_qa import _get_qa_client

                qa_client = _get_qa_client()
            for t, k in pending:
                person = photo if target["cast"] else None
                try:
                    result = inspect_fn(
                        image,
                        target["mime"],
                        target["concept"],
                        brand=ctx["brand"],
                        target_product=ctx["target_product"],
                        client=qa_client,
                        model=model,
                        has_logo_reference=ctx["has_logo_reference"],
                        strictness=ctx["strictness"],
                        person_image=person,
                        safer_angle_active=ctx["safer_angle_active"],
                        temperature=t,
                    )
                except Exception as exc:
                    logger.error("QA %s/%s t=%s r%d failed: %s", sid, name, t, k, exc)
                    report["errors"].append(
                        {
                            "session_id": sid,
                            "concept_name": name,
                            "temperature": t,
                            "repeat": k,
                        }
                    )
                    continue
                path = slug_dir / run_filename(t, k)
                write_json(
                    path,
                    {
                        "session_id": sid,
                        "concept_name": name,
                        "temperature": t,
                        "repeat": k,
                        "model": model,
                        "cast": target["cast"],
                        "result": result.model_dump(),
                        "failed_rules": failed_rules(
                            result,
                            target["concept"],
                            target_product=ctx["target_product"],
                        ),
                    },
                )
                report["written"].append(str(path))
    return report


def main(argv: Sequence[str] | None = None) -> dict[str, list]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--temps", default="0.0,1.0")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--selection", type=Path, default=SELECTION_PATH)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--img-dir", type=Path, default=IMG_DIR)
    parser.add_argument("--out-dir", type=Path, default=QA_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    selection = load_selection(args.selection)
    report = run(
        selection["session_ids"],
        parse_temps(args.temps),
        args.repeats,
        state_dir=args.state_dir,
        img_dir=args.img_dir,
        out_dir=args.out_dir,
    )
    write_json(args.out_dir / "notes.json", report["notes"])
    print(
        f"written {len(report['written'])}, skipped {len(report['skipped'])}, "
        f"errors {len(report['errors'])}, notes {len(report['notes'])}"
    )
    for note in report["notes"]:
        print(f"  note: {note}")
    return report


if __name__ == "__main__":
    main()
