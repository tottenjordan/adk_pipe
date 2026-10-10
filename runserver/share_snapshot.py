"""The frozen, allowlisted snapshot behind a shareable creative link (pure).

``build_snapshot`` turns a finished creative run (session state + eval report)
into ``snapshot.json`` v1, the only data the public share viewer ever reads::

    {"version": 1, "token", "created_at", "scope": "slate"|"creative",
     "brand", "product", "trend", "include_eval",
     "creatives": [{"index", "image": "<i>.jpg", "aspect_ratio", "alt",
                    "visual_style", "headline", "body", "caption", "cta", "tone",
                    "eval"?: {...} | null}]}

Allowlist only: every value is copied field by field into plain strings, so
prompts, rationales, judge notes, rating notes, emails and session/user ids can
never leak (the owner and session live only in the ``creative_shares`` row). The
source ``gs://`` image URIs are returned beside the snapshot for the server-side
re-encode step and are never written into it.

Visual concepts are paired with their ad copy and ad-copy eval exactly like the
results page (``frontend/src/lib/eval-matching.ts`` ``buildProofs``): by
``ad_copy_id`` → ``original_id``, then by headline, then by index position; the
visual eval matches by ``concept_name``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_common.config import BaseAgentConfiguration
from agent_common.sanitize import scrub_lone_surrogates
from creative_eval.dimensions import ADVISORY_GATES, gate_label
from runserver.ratings import _as_obj, _id_text, _items

SNAPSHOT_VERSION = 1
# Share images are re-encoded JPEGs (runserver/share_images.py); snapshots made
# before that name ``<i>.png``, which the viewer still serves.
SHARE_IMAGE_EXT = "jpg"
CREATIVE_FIELDS = (
    "index",
    "image",
    "aspect_ratio",
    "alt",
    "visual_style",
    "headline",
    "body",
    "caption",
    "cta",
    "tone",
)
TEXT_MAX_CHARS = 4000
# The image tool's aspect-ratio rules (creative_agent/image_tools._resolve_aspect_ratio).
ASPECT_RATIOS = BaseAgentConfiguration.image_aspect_ratios_allowed
DEFAULT_ASPECT_RATIO = "9:16"


class SnapshotError(ValueError):
    """A share can't be built; ``reason`` is the API error reason."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# A consent id -> its record when it is active AND owned by the share owner (the
# caller), else None. ``shares.py`` resolves the run's cast consent ids up front so
# this module stays pure.
ConsentLookup = Callable[[str], Mapping[str, Any] | None]
PERSON_NOT_SHAREABLE = "person_not_shareable"


@dataclass(frozen=True)
class BuiltSnapshot:
    snapshot: dict[str, Any]
    # Parallel to snapshot["creatives"]: source gs:// image and concept name.
    image_uris: list[str]
    concept_names: list[str]
    # Slate shares only: cast creatives left out, ``[{concept_name, reason}]``.
    skipped: list[dict[str, str]] = field(default_factory=list)
    # The consents of the cast creatives included (recorded on the share row only,
    # never in the snapshot) so revoking a consent revokes the share.
    person_consent_ids: list[str] = field(default_factory=list)


def _text(value: Any) -> str:
    """A plain, trimmed string ("" for anything that isn't a string)."""
    if not isinstance(value, str):
        return ""
    return scrub_lone_surrogates(value).strip()[:TEXT_MAX_CHARS]


def _trend(state: Mapping[str, Any], report: Mapping[str, Any] | None) -> str:
    raw = state.get("target_search_trends")
    if isinstance(raw, list):
        return ", ".join(t for t in (_text(x) for x in raw) if t)
    return _text(raw) or _text((report or {}).get("target_search_trend"))


def _ids_equal(a: Any, b: Any) -> bool:
    ta, tb = _id_text(a), _id_text(b)
    return ta is not None and ta == tb


def match_by_id_headline_index(
    items: Sequence[Mapping[str, Any]], vc: Mapping[str, Any], vc_index: int
) -> Mapping[str, Any] | None:
    """Port of eval-matching.ts ``matchByIdHeadlineIndex``: the item whose
    ``original_id`` is the concept's ``ad_copy_id``, else the one with the
    concept's headline, else the item at the concept's index position."""
    for it in items:
        if _ids_equal(it.get("original_id"), vc.get("ad_copy_id")):
            return it
    headline = vc.get("headline")
    if isinstance(headline, str):
        for it in items:
            if it.get("headline") == headline:
                return it
    return items[vc_index] if vc_index < len(items) else None


def _aspect_ratio(vc: Mapping[str, Any], override: Any) -> str:
    if isinstance(override, str) and override in ASPECT_RATIOS:
        return override
    candidate = vc.get("aspect_ratio")
    return candidate if candidate in ASPECT_RATIOS else DEFAULT_ASPECT_RATIO


def _image_uri(generated: Any, name: str) -> str:
    record = generated.get(name) if isinstance(generated, Mapping) else None
    uri = record.get("gcs_uri") if isinstance(record, Mapping) else None
    return uri.strip() if isinstance(uri, str) else ""


def _is_cast(generated: Any, name: str) -> bool:
    """The render shows the run's consented person (``generated_images[c].cast``)."""
    record = generated.get(name) if isinstance(generated, Mapping) else None
    return isinstance(record, Mapping) and record.get("cast") is True


def _consent_id(generated: Any, name: str) -> str | None:
    record = generated.get(name) if isinstance(generated, Mapping) else None
    cid = record.get("consent_id") if isinstance(record, Mapping) else None
    return cid if isinstance(cid, str) and cid else None


def cast_consent_ids(state: Mapping[str, Any]) -> list[str]:
    """The distinct ``consent_id`` values of the run's cast renders (sorted)."""
    generated = _as_obj(state.get("generated_images"))
    if not isinstance(generated, Mapping):
        return []
    return sorted(
        {
            cid
            for name in generated
            if _is_cast(generated, name) and (cid := _consent_id(generated, name))
        }
    )


def _shareable_consent(
    generated: Any, name: str, consent_lookup: ConsentLookup | None
) -> str | None:
    """The consent id when a cast creative may go public: its consent is active,
    owned by the share owner (``consent_lookup``) and has ``allow_public_share``;
    None otherwise (deny by default)."""
    cid = _consent_id(generated, name)
    if cid is None or consent_lookup is None:
        return None
    record = consent_lookup(cid)
    if not isinstance(record, Mapping) or record.get("allow_public_share") is not True:
        return None
    return cid


def _score_block(ev: Mapping[str, Any] | None) -> dict[str, Any] | None:
    score = ev.get("score") if isinstance(ev, Mapping) else None
    if not isinstance(score, Mapping):
        return None
    overall = score.get("overall_score")
    passed = score.get("passed")
    return {
        "passed": passed if isinstance(passed, bool) else None,
        "score": (
            round(float(overall), 3)
            if isinstance(overall, (int, float)) and not isinstance(overall, bool)
            else None
        ),
    }


def _checks(ev: Mapping[str, Any] | None, kind: str) -> list[dict[str, Any]]:
    score = ev.get("score") if isinstance(ev, Mapping) else None
    gates = score.get("gates") if isinstance(score, Mapping) else None
    if not isinstance(gates, list):
        return []
    out = []
    for g in gates:
        if not (
            isinstance(g, Mapping)
            and isinstance(g.get("gate"), str)
            and isinstance(g.get("passed"), bool)
        ):
            continue
        out.append(
            {
                "kind": kind,
                "gate": g["gate"],
                "label": gate_label(g["gate"]),
                "passed": g["passed"],
                "advisory": g.get("advisory") is True or g["gate"] in ADVISORY_GATES,
            }
        )
    return out


def _eval_block(
    copy_ev: Mapping[str, Any] | None, visual_ev: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Both verdicts for one creative (judge notes and rationales dropped);
    None when the judge didn't evaluate either half."""
    copy, visual = _score_block(copy_ev), _score_block(visual_ev)
    if copy is None and visual is None:
        return None
    halves = [b for b in (copy, visual) if b is not None]
    verdicts = [b["passed"] for b in halves if b["passed"] is not None]
    scores = [b["score"] for b in halves if b["score"] is not None]
    return {
        "passed": all(verdicts) if verdicts else None,
        "score": round(sum(scores) / len(scores), 3) if scores else None,
        "checks": _checks(copy_ev, "copy") + _checks(visual_ev, "visual"),
        "copy": copy,
        "visual": visual,
    }


def build_snapshot(
    state: Mapping[str, Any],
    report: Any,
    *,
    token: str,
    concept_names: Sequence[str] | None,
    include_eval: bool,
    now: str,
    consent_lookup: ConsentLookup | None = None,
) -> BuiltSnapshot:
    """The v1 snapshot for the whole slate (``concept_names=None``) or the named
    concepts (kept in pipeline order). Concepts without a rendered image are
    skipped. A concept that casts a person (``generated_images[c].cast``) is
    included only when its ``consent_id`` resolves via ``consent_lookup`` to a
    record with ``allow_public_share`` (no lookup = never); a slate leaves the
    others out and lists them in ``skipped``. Raises ``SnapshotError`` for an
    unknown concept name, a named cast concept that isn't shareable
    (``person_not_shareable``) or when no creative with an image remains."""
    report = _as_obj(report)
    if not isinstance(report, Mapping):
        report = None
    concepts = _items(state.get("final_visual_concepts"), "visual_concepts")
    copies = _items(state.get("ad_copy_critique"), "ad_copies")
    copy_evals = _items(report, "ad_copy_evaluations") if report else []
    visual_evals = _items(report, "visual_concept_evaluations") if report else []
    generated = _as_obj(state.get("generated_images"))
    override = state.get("visual_aspect_ratio")

    known = {vc.get("concept_name") for vc in concepts}
    wanted: set[str] | None = None
    if concept_names is not None:
        wanted = set(concept_names)
        if unknown := sorted(n for n in wanted if n not in known):
            raise SnapshotError(
                "unknown_concept", f"not a creative in this run: {unknown[:4]}"
            )
        if any(
            _is_cast(generated, n)
            and _shareable_consent(generated, n, consent_lookup) is None
            for n in wanted
        ):
            raise SnapshotError(
                PERSON_NOT_SHAREABLE,
                "a creative shows a person whose consent doesn't cover public links",
            )

    brand = _text(state.get("brand")) or _text((report or {}).get("brand"))
    creatives: list[dict[str, Any]] = []
    uris: list[str] = []
    names: list[str] = []
    skipped: list[dict[str, str]] = []
    consent_ids: list[str] = []
    for vc_index, vc in enumerate(concepts):
        name = vc.get("concept_name")
        if not isinstance(name, str) or (wanted is not None and name not in wanted):
            continue
        if name in names or not (uri := _image_uri(generated, name)):
            continue
        if _is_cast(generated, name):
            cid = _shareable_consent(generated, name, consent_lookup)
            if cid is None:
                skipped.append({"concept_name": name, "reason": PERSON_NOT_SHAREABLE})
                continue
            if cid not in consent_ids:
                consent_ids.append(cid)
        copy = match_by_id_headline_index(copies, vc, vc_index) or {}
        i = len(creatives)
        headline = _text(copy.get("headline")) or _text(vc.get("headline"))
        creative: dict[str, Any] = {
            "index": i,
            "image": f"{i}.{SHARE_IMAGE_EXT}",
            "aspect_ratio": _aspect_ratio(vc, override),
            "alt": _text(vc.get("concept_summary"))
            or f"{brand or 'Brand'} ad image: {headline or name}",
            "visual_style": _text(vc.get("visual_style")),
            "headline": headline,
            "body": _text(copy.get("body_text")),
            "caption": _text(copy.get("social_caption"))
            or _text(vc.get("social_caption")),
            "cta": _text(copy.get("call_to_action")) or _text(vc.get("call_to_action")),
            "tone": _text(copy.get("tone_style")),
        }
        if include_eval:
            visual_ev = next(
                (e for e in visual_evals if e.get("concept_name") == name), None
            )
            copy_ev = match_by_id_headline_index(copy_evals, vc, vc_index)
            creative["eval"] = _eval_block(copy_ev, visual_ev)
        creatives.append(creative)
        uris.append(uri)
        names.append(name)

    if not creatives:
        raise SnapshotError("no_images", "no rendered images to share in this run")
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "token": token,
        "created_at": now,
        "scope": "creative"
        if concept_names is not None and len(names) == 1
        else "slate",
        "brand": brand,
        "product": _text(state.get("target_product"))
        or _text((report or {}).get("target_product")),
        "trend": _trend(state, report),
        "include_eval": bool(include_eval),
        "creatives": creatives,
    }
    return BuiltSnapshot(
        snapshot=snapshot,
        image_uris=uris,
        concept_names=names,
        skipped=skipped,
        person_consent_ids=consent_ids,
    )
