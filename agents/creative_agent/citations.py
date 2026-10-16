"""Citation rendering shared by the composer callback and runserver edits.

Converts ``<cite source="src-N"/>`` tags into Markdown links using the
``sources`` map built by ``collect_research_sources_callback``. Kept free of
ADK imports so the runserver can reuse it for checkpoint-1 report edits.
"""

import logging
import re

_CITE_TAG_RE = re.compile(r'<cite\s+source\s*=\s*["\']?\s*(src-\d+)\s*["\']?\s*/>')
_PUNCT_SPACING_RE = re.compile(r"\s+([.,;:])")


def render_citations(report: str, sources: dict) -> str:
    """Replace citation tags in ``report`` with Markdown links.

    Unknown source ids are dropped (with a warning) and whitespace before
    punctuation is collapsed.
    """

    def tag_replacer(match: re.Match) -> str:
        short_id = match.group(1)
        if not (source_info := sources.get(short_id)):
            logging.warning(f"Invalid citation tag found and removed: {match.group(0)}")
            return ""
        display_text = source_info.get("title", source_info.get("domain", short_id))
        return f" [{display_text}]({source_info['url']})"

    processed_report = _CITE_TAG_RE.sub(tag_replacer, report)
    return _PUNCT_SPACING_RE.sub(r"\1", processed_report)
