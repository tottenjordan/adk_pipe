"""Citation rendering shared by the composer callback and runserver edits.

Converts ``<cite source="src-N"/>``, ``<cite source="ent-N"/>``,
``<cite source="src-1, ent-2"/>``, and inline ``[ent-N]`` citations into
Markdown links or ``[Enterprise: Title]`` markers using the ``sources`` and
``enterprise_sources`` maps built during research. Kept free of ADK imports so
the runserver can reuse it for checkpoint-1 report edits.
"""

import logging
import re
from collections.abc import Mapping
from typing import Any

_CITE_TAG_RE = re.compile(
    r'<cite\s+source\s*=\s*["\']([^"\']*)["\']\s*/>'
    r"|<cite\s+source\s*=\s*((?:src|ent)-\d+)\s*/>"
)
_BRACKET_ENT_RE = re.compile(r"\[(ent-\d+)\](?!\()")
_PUNCT_SPACING_RE = re.compile(r"\s+([.,;:])")
_LINE_BREAK_RE = re.compile(r"[\r\n]+")
_ENTERPRISE_BIBLIO_HEADING = "### Enterprise Knowledge Sources"
_UNSAFE_URL_SCHEMES = ("javascript:", "data:", "vbscript:")


def _is_http_url(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _escape_markdown_url(url: str) -> str:
    return url.strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _sanitize_markdown_label(raw: str, fallback: str) -> str:
    collapsed = _LINE_BREAK_RE.sub(" ", raw)
    cleaned = collapsed.replace("[", "").replace("]", "").strip()
    return cleaned or fallback


def _sanitize_enterprise_label(raw: str, fallback: str) -> str:
    text = _LINE_BREAK_RE.sub(" ", raw).strip()
    if not text:
        return fallback
    if text.lower().startswith("gs://"):
        stripped = text[5:].rstrip("/")
        if not stripped:
            return fallback
        text = stripped.rsplit("/", 1)[-1] or stripped
    return _sanitize_markdown_label(text, fallback)


def _enterprise_display_text(short_id: str, source_info: Mapping[str, Any]) -> str:
    for candidate in (source_info.get("title"), source_info.get("domain")):
        cleaned = _sanitize_enterprise_label(str(candidate or ""), "")
        if cleaned:
            return cleaned
    return short_id


def _format_enterprise_inline(short_id: str, source_info: Mapping[str, Any]) -> str:
    display_text = _enterprise_display_text(short_id, source_info)
    url = str(source_info.get("url") or "").strip()
    if _is_http_url(url):
        return f"[{display_text}]({_escape_markdown_url(url)})"
    return f"[Enterprise: {display_text}]"


def _format_enterprise_biblio_line(
    short_id: str, source_info: Mapping[str, Any]
) -> str:
    display_text = _enterprise_display_text(short_id, source_info)
    url = str(source_info.get("url") or "").strip()
    if _is_http_url(url):
        return f"- **{short_id}**: [{display_text}]({_escape_markdown_url(url)})"
    return f"- **{short_id}**: {display_text}"


def render_citations(
    report: str,
    sources: Mapping[str, Any] | None = None,
    enterprise_sources: Mapping[str, Any] | None = None,
) -> str:
    """Replace citation tags in ``report`` with Markdown links or enterprise badges.

    Unknown source ids are dropped (with a warning) and whitespace before
    punctuation is collapsed. When one or more ``ent-N`` sources are cited, an
    ``### Enterprise Knowledge Sources`` bibliography block is appended.
    """
    if not report:
        return ""

    web_sources: Mapping[str, Any] = sources or {}
    ent_map: dict[str, Any] = {
        k: v
        for k, v in web_sources.items()
        if isinstance(k, str) and k.startswith("ent-") and isinstance(v, Mapping)
    }
    if enterprise_sources:
        for k, v in enterprise_sources.items():
            if isinstance(k, str) and isinstance(v, Mapping):
                ent_map[k] = v
    all_sources: dict[str, Any] = {**web_sources, **ent_map}
    cited_ent_ids: list[str] = []

    def tag_replacer(match: re.Match[str]) -> str:
        raw_ids = match.group(1) if match.group(1) is not None else match.group(2)
        parts = [p.strip() for p in (raw_ids or "").split(",") if p.strip()]
        if not parts:
            logging.warning(f"Invalid citation tag found and removed: {match.group(0)}")
            return ""
        rendered: list[str] = []
        for short_id in parts:
            source_info = all_sources.get(short_id)
            if not isinstance(source_info, Mapping) or not source_info:
                logging.warning(
                    f"Invalid citation tag found and removed: {match.group(0)}"
                )
                continue
            if short_id.startswith("ent-"):
                cited_ent_ids.append(short_id)
                rendered.append(_format_enterprise_inline(short_id, source_info))
            else:
                url = str(source_info.get("url") or "").strip()
                if not url or url.lower().startswith(_UNSAFE_URL_SCHEMES):
                    logging.warning(
                        f"Invalid or unsafe citation URL ignored for {short_id}: {match.group(0)}"
                    )
                    continue
                raw_label = str(
                    source_info.get("title") or source_info.get("domain") or short_id
                )
                display_text = _sanitize_markdown_label(raw_label, short_id)
                rendered.append(f"[{display_text}]({_escape_markdown_url(url)})")
        if not rendered:
            return ""
        return " " + ", ".join(rendered)

    def bracket_ent_replacer(match: re.Match[str]) -> str:
        short_id = match.group(1)
        source_info = ent_map.get(short_id)
        if not isinstance(source_info, Mapping) or not source_info:
            logging.warning(f"Invalid citation tag found and removed: {match.group(0)}")
            return ""
        cited_ent_ids.append(short_id)
        return _format_enterprise_inline(short_id, source_info)

    processed_report = _CITE_TAG_RE.sub(tag_replacer, report)
    processed_report = _BRACKET_ENT_RE.sub(bracket_ent_replacer, processed_report)
    processed_report = _PUNCT_SPACING_RE.sub(r"\1", processed_report)

    if cited_ent_ids and _ENTERPRISE_BIBLIO_HEADING not in processed_report:
        unique_ent_ids = list(dict.fromkeys(cited_ent_ids))
        biblio_lines = [
            _ENTERPRISE_BIBLIO_HEADING,
            *(
                _format_enterprise_biblio_line(ent_id, ent_map[ent_id])
                for ent_id in unique_ent_ids
                if ent_id in ent_map
            ),
        ]
        if len(biblio_lines) > 1:
            processed_report = (
                processed_report.rstrip() + "\n\n" + "\n".join(biblio_lines)
            )

    return processed_report


replace_citations = render_citations
