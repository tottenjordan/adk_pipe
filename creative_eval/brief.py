"""The creative brief as judge input — pure, ADK-free.

``creative_agent`` imports ``creative_eval`` (never the reverse), so the brief
is read here from its plain state form (a ``CreativeBrief`` dict or its JSON
string) rather than through ``creative_agent.brief_check``.

The rendered block is passed to the judge prompts as ONE ``str.format``
argument, so brief text containing ``{`` / ``}`` is substituted verbatim and
never parsed as a placeholder.
"""

import json
from collections.abc import Mapping
from typing import Any

NO_BRIEF_BLOCK = (
    "No creative brief is available for this run. Judge the brief-dependent "
    'gates as passed with the note "no brief".'
)
_NONE = "(none)"


def parse_brief(raw: Any) -> Mapping[str, Any] | None:
    """The brief as a non-empty mapping (a dict or its JSON string), else None."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, Mapping) and raw else None


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _texts(value: Any) -> list[str]:
    items = value if isinstance(value, list) else []
    return [t for t in (_text(v) for v in items) if t]


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else f"- {_NONE}"


def format_brief_for_judge(brief: Mapping[str, Any] | None) -> str:
    """The judge's ``<CREATIVE_BRIEF>`` block for a parsed brief (pure).

    Covers what the gates check against: proposition, reasons to believe,
    mandatories, avoid (+ brand don'ts), the trend bridge and the brand cues.
    Missing or malformed fields render as "(none)"; ``None`` →
    :data:`NO_BRIEF_BLOCK`.
    """
    if brief is None:
        return NO_BRIEF_BLOCK
    raw_rtbs = brief.get("reasons_to_believe")
    rtbs = [
        _text(r.get("claim")) if isinstance(r, Mapping) else _text(r)
        for r in (raw_rtbs if isinstance(raw_rtbs, list) else [])
    ]
    bridge = _mapping(brief.get("trend_bridge"))
    brand = _mapping(brief.get("brand"))
    fit_score = bridge.get("fit_score")
    fit = f"{fit_score}/5" if isinstance(fit_score, int) else "unknown"
    fit_mode = _text(bridge.get("fit_mode")) or "unknown"
    return "\n".join(
        [
            f"Single-minded proposition: "
            f"{_text(brief.get('single_minded_proposition')) or _NONE}",
            "Reasons to believe:",
            _bullets([r for r in rtbs if r]),
            "Mandatories:",
            _bullets(_texts(brief.get("mandatories"))),
            "Avoid:",
            _bullets(_texts(brief.get("avoid"))),
            "Brand don'ts:",
            _bullets(_texts(brand.get("do_not"))),
            f"Trend bridge: fit {fit} ({fit_mode}) — "
            f"{_text(bridge.get('bridge')) or _NONE}",
            f"Brand tone of voice: {_text(brand.get('tone_of_voice')) or _NONE}",
            "Brand distinctive assets: "
            + ("; ".join(_texts(brand.get("distinctive_assets"))) or _NONE),
        ]
    )


def angle_line(brief: Mapping[str, Any] | None, angle_id: str) -> str:
    """``"A2 — name: route"`` for the copy's brief angle, else the bare id (pure)."""
    angle_id = _text(angle_id)
    if not angle_id:
        return _NONE
    angles = (brief or {}).get("angles")
    for angle in angles if isinstance(angles, list) else []:
        if isinstance(angle, Mapping) and _text(angle.get("angle_id")) == angle_id:
            name, route = _text(angle.get("name")), _text(angle.get("route"))
            detail = ": ".join(p for p in (name, route) if p)
            return f"{angle_id} — {detail}" if detail else angle_id
    return angle_id
