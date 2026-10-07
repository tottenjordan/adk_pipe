"""Render the structured CreativeBrief for the run's deliverables (pure).

- ``render_brief_markdown`` — a readable "## Creative Brief" Markdown section.
- ``insert_brief_into_report`` — places that section at the top of the research
  report body (after the report's H1 title block), for the research PDF.
- ``render_brief_summary_html`` — a small proposition + fit card for the HTML
  gallery.

Every function returns ``""`` (or the report unchanged) when there is no brief,
so runs without one render exactly as before.
"""

import html
import re
from collections.abc import Mapping
from typing import Any

from .brief_check import parse_brief

_H1 = re.compile(r"^# \S", re.MULTILINE)
_H2 = re.compile(r"^## ", re.MULTILINE)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: Any) -> list[str]:
    return [_text(v) for v in value if _text(v)] if isinstance(value, list) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _fit(bridge: Mapping[str, Any]) -> str:
    score, mode = bridge.get("fit_score"), _text(bridge.get("fit_mode"))
    if score is None and not mode:
        return ""
    return f"{score}/5 ({mode})" if mode else f"{score}/5"


def render_brief_markdown(brief: Mapping[str, Any] | str | None) -> str:
    """The brief as a "## Creative Brief" Markdown section ("" when missing).

    Sub-sections are level 3 so the section nests under the report's H1 title
    (markdown_pdf's table of contents rejects skipped heading levels).
    """
    data = parse_brief(brief)
    if data is None:
        return ""

    lines = ["## Creative Brief", ""]
    for label, key in (
        ("Single-minded proposition", "single_minded_proposition"),
        ("Insight", "insight"),
        ("Objective", "objective"),
        ("Audience", "audience"),
        ("Desired response", "desired_response"),
    ):
        if value := _text(data.get(key)):
            lines.append(f"**{label}:** {value}")
            lines.append("")

    rtbs = []
    for rtb in data.get("reasons_to_believe") or []:
        rtb = _mapping(rtb)
        if claim := _text(rtb.get("claim")):
            source = _text(rtb.get("source_id"))
            rtbs.append(f"- {claim} [{source}]" if source else f"- {claim}")
    if rtbs:
        lines += ["### Reasons to believe", *rtbs, ""]

    brand = _mapping(data.get("brand"))
    brand_lines = [
        f"- **{label}:** {value}"
        for label, value in (
            ("Tone of voice", _text(brand.get("tone_of_voice"))),
            (
                "Distinctive assets",
                "; ".join(_strings(brand.get("distinctive_assets"))),
            ),
            ("Do not", "; ".join(_strings(brand.get("do_not")))),
        )
        if value
    ]
    if brand_lines:
        lines += ["### Brand", *brand_lines, ""]

    bridge = _mapping(data.get("trend_bridge"))
    trend_lines = [
        f"- **{label}:** {value}"
        for label, value in (
            ("Fit", _fit(bridge)),
            ("Bridge", _text(bridge.get("bridge"))),
            ("Motifs", "; ".join(_strings(bridge.get("motifs")))),
            ("Risks", "; ".join(_strings(bridge.get("risks")))),
        )
        if value
    ]
    if trend_lines:
        lines += ["### Trend fit", *trend_lines, ""]

    for title, key in (("Mandatories", "mandatories"), ("Avoid", "avoid")):
        if items := _strings(data.get(key)):
            lines += [f"### {title}", *(f"- {item}" for item in items), ""]

    angles = []
    for angle in data.get("angles") or []:
        angle = _mapping(angle)
        head = " ".join(
            p for p in (_text(angle.get("angle_id")), _text(angle.get("name"))) if p
        )
        body = " ".join(
            p for p in (_text(angle.get("tension")), _text(angle.get("route"))) if p
        )
        if head or body:
            angles.append(f"- **{head}:** {body}" if head else f"- {body}")
    if angles:
        lines += ["### Creative angles", *angles, ""]

    return "\n".join(lines).rstrip() + "\n"


def insert_brief_into_report(report: str, brief: Mapping[str, Any] | str | None) -> str:
    """Place the brief section at the top of the report body (pure).

    After the report's H1 title block (before its first "## " section); a
    report without an H1 gets a title first, since the PDF table of contents
    must start at level 1. Returns ``report`` unchanged when there is no brief.
    """
    section = render_brief_markdown(brief)
    if not section:
        return report
    title = _H1.search(report)
    if title is None:
        return f"# Creative Brief & Research Report\n\n{section}\n{report}"
    first_section = _H2.search(report, title.end())
    if first_section is None:
        return f"{report.rstrip()}\n\n{section}"
    cut = first_section.start()
    return f"{report[:cut]}{section}\n{report[cut:]}"


def render_brief_summary_html(brief: Mapping[str, Any] | str | None) -> str:
    """A small proposition + trend-fit card for the HTML gallery ("" if none)."""
    data = parse_brief(brief)
    if data is None:
        return ""
    proposition = _text(data.get("single_minded_proposition"))
    fit = _fit(_mapping(data.get("trend_bridge")))
    if not (proposition or fit):
        return ""
    rows = []
    if proposition:
        rows.append(f"<p><strong>Proposition:</strong> {html.escape(proposition)}</p>")
    if fit:
        rows.append(f"<p><strong>Trend fit:</strong> {html.escape(fit)}</p>")
    return f"""
            <div class="brief-summary">
                {"".join(rows)}
            </div>
    """
