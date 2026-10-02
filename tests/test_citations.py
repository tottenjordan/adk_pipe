"""Tests for the shared citation renderer (creative_agent.citations).

Note: the renderer inserts a leading space before each link (verbatim from the
original composer callback), so a tag preceded by a space yields two spaces.
"""

from creative_agent.citations import render_citations

SOURCES = {"src-1": {"title": "Vogue", "url": "https://v.example"}}


def test_replaces_known_tag_with_markdown_link():
    assert (
        render_citations('Glow up <cite source="src-1"/>.', SOURCES)
        == "Glow up  [Vogue](https://v.example)."
    )


def test_drops_unknown_tag_and_fixes_punctuation_spacing():
    assert render_citations('Hi <cite source="src-9"/> .', SOURCES) == "Hi."


def test_falls_back_to_domain_then_id():
    s = {"src-2": {"domain": "d.example", "url": "u"}}
    assert render_citations('x <cite source="src-2"/>', s) == "x  [d.example](u)"
    s3 = {"src-3": {"url": "u3"}}
    assert render_citations('y <cite source="src-3"/>', s3) == "y  [src-3](u3)"


def test_braces_and_empty_input_safe():
    assert render_citations("{not_a_key}", {}) == "{not_a_key}"
    assert render_citations("", SOURCES) == ""
