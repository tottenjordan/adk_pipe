"""Rendering the structured creative brief into the research PDF + HTML gallery."""

import asyncio
import copy
import json

from creative_agent import gcs_tools, tools
from creative_agent.brief_render import (
    insert_brief_into_report,
    render_brief_markdown,
    render_brief_summary_html,
)
from tests._fakes import FakeToolContext
from tests.test_creative_agent_graph import _BRIEF


def test_missing_brief_renders_nothing():
    for empty in (None, "", "not json", {}, []):
        assert render_brief_markdown(empty) == ""
        assert render_brief_summary_html(empty) == ""


def test_render_brief_markdown_sections():
    md = render_brief_markdown(_BRIEF)
    assert md.startswith("## Creative Brief\n")
    for text in (
        "**Single-minded proposition:** Rocket Skates finally make you faster.",
        "**Insight:** Coyotes want to win the chase, but every gadget backfires.",
        "**Objective:**",
        "**Audience:**",
        "**Desired response:**",
        "- Roadrunners are trending [src-1]",
        "- Fast, per the brief [brief]",
        "**Tone of voice:** Deadpan slapstick confidence",
        "**Distinctive assets:** the ACME crate",
        "**Do not:** mock the customer",
        "**Fit:** 4/5 (direct)",
        "**Bridge:**",
        "**Motifs:** a roadrunner dust cloud; desert mesa road",
        "**Risks:** cartoon violence",
        "### Mandatories\n- show the ACME logo",
        "### Avoid\n- cliff falls",
        "- **A1 Finally fast:** t1 r1",
    ):
        assert text in md, text
    # Sub-sections are level 3 under the level-2 section (valid PDF TOC nesting).
    assert "\n# " not in md


def test_render_accepts_json_string_and_skips_empty_fields():
    brief = copy.deepcopy(_BRIEF)
    brief["mandatories"] = []
    brief["reasons_to_believe"] = [{"claim": "Uncited", "source_id": None}]
    md = render_brief_markdown(json.dumps(brief))
    assert "### Mandatories" not in md
    assert "- Uncited\n" in md + "\n"


def test_insert_after_the_report_title():
    report = "# Campaign\n**Search Trend: x**\n\n## Executive Summary\nBody\n"
    out = insert_brief_into_report(report, _BRIEF)
    assert out.startswith("# Campaign\n**Search Trend: x**\n")
    assert out.index("## Creative Brief") < out.index("## Executive Summary")


def test_insert_without_brief_is_identity():
    assert insert_brief_into_report("# R\n## S\n", None) == "# R\n## S\n"


def test_insert_into_report_without_title_adds_one():
    """markdown_pdf's TOC must start at level 1, so a report with no H1 (or an
    empty one) gets a title before the level-2 brief section."""
    out = insert_brief_into_report("", _BRIEF)
    assert out.startswith("# ")
    assert "## Creative Brief" in out


def test_inserted_report_renders_to_pdf(tmp_path):
    from markdown_pdf import MarkdownPdf, Section

    for report in ("# Campaign\n## Executive Summary\nBody\n", "", "Body only"):
        pdf = MarkdownPdf(toc_level=4)
        pdf.add_section(Section(f" {insert_brief_into_report(report, _BRIEF)}\n"))
        pdf.save(str(tmp_path / "r.pdf"))


def test_save_draft_report_artifact_includes_the_brief(monkeypatch, tmp_path):
    seen: list[str] = []

    class _Section:
        def __init__(self, text, *a, **k):
            seen.append(text)

    class _Pdf:
        def __init__(self, *a, **k):
            self.meta: dict = {}

        def add_section(self, *a, **k):
            return None

        def save(self, path):
            with open(path, "wb") as f:
                f.write(b"%PDF-1.4 fake")

    monkeypatch.setattr(gcs_tools, "MarkdownPdf", _Pdf)
    monkeypatch.setattr(gcs_tools, "Section", _Section)
    monkeypatch.setattr(gcs_tools, "_upload_blob_to_gcs", lambda **k: "ok")
    ctx = FakeToolContext(
        {
            "gcs_folder": "f",
            "agent_output_dir": "creative_output",
            "final_report_with_citations": "# Report\n## Executive Summary\nx",
            "creative_brief": _BRIEF,
        }
    )

    assert asyncio.run(gcs_tools.save_draft_report_artifact(ctx))["status"] == (
        "success"
    )
    (text,) = seen
    assert text.index("# Report") < text.index("## Creative Brief")
    assert text.index("## Creative Brief") < text.index("## Executive Summary")


def test_brief_summary_html_is_small_and_escaped():
    brief = copy.deepcopy(_BRIEF)
    brief["single_minded_proposition"] = "Fast <b>skates</b>"
    html = render_brief_summary_html(brief)
    assert 'class="brief-summary"' in html
    assert "Fast &lt;b&gt;skates&lt;/b&gt;" in html
    assert "4/5 (direct)" in html
    assert "Coyotes want" not in html  # proposition + fit only


def test_gallery_includes_the_brief_summary(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    written: list[str] = []

    def _fake_upload(source_file_name, destination_blob_name):
        with open(source_file_name) as f:
            written.append(f.read())
        return "ok"

    monkeypatch.setattr(tools, "_upload_blob_to_gcs", _fake_upload)
    ctx = FakeToolContext(
        {
            "gcs_folder": "f",
            "agent_output_dir": "creative_output",
            "final_visual_concepts": {"visual_concepts": []},
            "ad_copy_critique": {"ad_copies": []},
            "brand": "b",
            "target_audience": "a",
            "target_product": "p",
            "key_selling_points": "k",
            "target_search_trends": "t",
            "creative_brief": _BRIEF,
        }
    )
    asyncio.run(tools.save_creative_gallery_html(ctx))
    (html,) = written
    assert "Rocket Skates finally make you faster." in html
    assert ".brief-summary" in html  # styled in the gallery template


def test_brief_summary_html_escapes_script_in_model_text():
    brief = {
        **_BRIEF,
        "single_minded_proposition": "<script>alert(1)</script> skates win.",
        "trend_bridge": {**_BRIEF["trend_bridge"], "fit_mode": "<b>direct</b>"},
    }
    html = render_brief_summary_html(brief)
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html


def test_render_brief_markdown_compact_variant_has_no_heading():
    full = render_brief_markdown(_BRIEF)
    compact = render_brief_markdown(_BRIEF, heading=False)
    assert full.startswith("## Creative Brief")
    assert "## Creative Brief" not in compact
    assert compact.startswith("**Single-minded proposition:**")
    assert render_brief_markdown(None, heading=False) == ""


def test_current_moment_renders_first_in_the_trend_fit_block():
    moment = "Upcoming: Roadrunner Cup final, 2026-10-12. (sources: src-1)"
    md = render_brief_markdown(_BRIEF, heading=False, current_moment=moment)
    assert f"### Trend fit\n- **Current moment:** {moment}\n- **Fit:**" in md
    # Without one (or blank) the brief renders exactly as before.
    assert render_brief_markdown(_BRIEF, current_moment="  ") == render_brief_markdown(
        _BRIEF
    )
    assert "Current moment" not in render_brief_markdown(_BRIEF)
    # No brief: still nothing (the moment alone is not a brief).
    assert render_brief_markdown(None, current_moment=moment) == ""


def test_gallery_escapes_model_and_user_text(monkeypatch, tmp_path):
    """Model/user text (headline, brand, ...) must not inject markup into the gallery."""
    monkeypatch.chdir(tmp_path)
    written: list[str] = []

    def _fake_upload(source_file_name, destination_blob_name):
        with open(source_file_name) as f:
            written.append(f.read())
        return "ok"

    monkeypatch.setattr(tools, "_upload_blob_to_gcs", _fake_upload)
    payload = "<script>alert(1)</script>"
    concept = {
        "concept_name": "Concept A",
        "visual_style": "flat",
        "trend": "t",
        "trend_reference": 'say "hi"',
        "concept_summary": 'A "quoted" <b>summary</b>',
        "markets_product": "m",
        "audience_appeal": "a",
        "selection_rationale": "r",
        "image_generation_prompt": "p",
        "headline": f"Win {payload}",
        "social_caption": "c",
    }
    ad_copy = {
        "headline": f"Win {payload}",
        "body_text": "b",
        "social_caption": "c",
        "call_to_action": "Go",
        "trend_connection": "t",
        "audience_appeal_rationale": "a",
        "detailed_performance_rationale": "d",
    }
    ctx = FakeToolContext(
        {
            "gcs_folder": "f",
            "agent_output_dir": "creative_output",
            "final_visual_concepts": {"visual_concepts": [concept]},
            "ad_copy_critique": {"ad_copies": [ad_copy]},
            "brand": f"Acme {payload}",
            "target_audience": "a",
            "target_product": "p",
            "key_selling_points": "k",
            "target_search_trends": "t",
        }
    )
    result = asyncio.run(tools.save_creative_gallery_html(ctx))
    assert result["status"] == "success"
    (html,) = written
    assert payload not in html
    assert "<b>summary</b>" not in html
    assert "<h1>Acme &lt;script&gt;alert(1)&lt;/script&gt; p</h1>" in html
    assert (
        '<h4 class="image-title">Win &lt;script&gt;alert(1)&lt;/script&gt;</h4>' in html
    )
    assert 'title="Win &lt;script&gt;alert(1)&lt;/script&gt;"' in html
    assert 'alt="A &quot;quoted&quot; &lt;b&gt;summary&lt;/b&gt;"' in html
    assert "<dd>Win &lt;script&gt;alert(1)&lt;/script&gt;</dd>" in html


def test_timely_angle_renders_in_the_trend_fit_block():
    brief = copy.deepcopy(_BRIEF)
    brief["trend_bridge"]["timely_angle"] = (
        "Game-day rituals before tomorrow's kickoff."
    )
    md = render_brief_markdown(brief, heading=False)
    assert (
        "- **Risks:** cartoon violence\n"
        "- **Timely angle:** Game-day rituals before tomorrow's kickoff." in md
    )
    assert "Timely angle" not in render_brief_markdown(_BRIEF)


def test_gallery_labels_the_timely_creative(monkeypatch, tmp_path):
    """The timely creative gets a small escaped "Timely" label; evergreen ones none."""
    monkeypatch.chdir(tmp_path)
    written: list[str] = []

    def _fake_upload(source_file_name, destination_blob_name):
        with open(source_file_name) as f:
            written.append(f.read())
        return "ok"

    monkeypatch.setattr(tools, "_upload_blob_to_gcs", _fake_upload)
    base = {
        "visual_style": "flat",
        "trend": "t",
        "trend_reference": "r",
        "concept_summary": "s",
        "markets_product": "m",
        "audience_appeal": "a",
        "selection_rationale": "r",
        "image_generation_prompt": "p",
        "headline": "h",
        "social_caption": "c",
    }
    timely = {
        **base,
        "concept_name": "Countdown",
        "uses_trend_state": True,
        "timely_reason": "Game <b>tomorrow</b>.",
    }
    evergreen = {**base, "concept_name": "Evergreen"}
    ad_copy = {
        "headline": "h",
        "body_text": "b",
        "social_caption": "c",
        "call_to_action": "Go",
        "trend_connection": "t",
        "audience_appeal_rationale": "a",
        "detailed_performance_rationale": "d",
    }
    ctx = FakeToolContext(
        {
            "gcs_folder": "f",
            "agent_output_dir": "creative_output",
            "final_visual_concepts": {"visual_concepts": [timely, evergreen]},
            "ad_copy_critique": {"ad_copies": [ad_copy, ad_copy]},
            "brand": "Acme",
            "target_audience": "a",
            "target_product": "p",
            "key_selling_points": "k",
            "target_search_trends": "t",
            # The render record's valid_until wins over the run's moment.
            "generated_images": {
                "Countdown": {
                    "timely": True,
                    "valid_until": "2026-10-12T23:59:59-04:00",
                }
            },
            "trend_situation": {
                "timely_ok": True,
                "valid_until": "2026-10-20T10:00:00+00:00",
            },
        }
    )
    assert asyncio.run(tools.save_creative_gallery_html(ctx))["status"] == "success"
    (html,) = written
    assert html.count('class="timely-label"') == 1
    assert (
        '<p class="timely-label">Timely: Game &lt;b&gt;tomorrow&lt;/b&gt;. · valid '
        "until Oct 12, 23:59 UTC-04:00</p>" in html
    )


def test_timely_label_falls_back_to_the_run_moment(monkeypatch, tmp_path):
    """A record without valid_until (rendered before the field) uses the run's
    trend_situation.valid_until."""
    monkeypatch.chdir(tmp_path)
    written: list[str] = []

    def _fake_upload(source_file_name, destination_blob_name):
        with open(source_file_name) as f:
            written.append(f.read())
        return "ok"

    monkeypatch.setattr(tools, "_upload_blob_to_gcs", _fake_upload)
    concept = {
        "visual_style": "flat",
        "trend": "t",
        "trend_reference": "r",
        "concept_summary": "s",
        "markets_product": "m",
        "audience_appeal": "a",
        "selection_rationale": "r",
        "image_generation_prompt": "p",
        "headline": "h",
        "social_caption": "c",
        "concept_name": "Countdown",
        "uses_trend_state": True,
        "timely_reason": "Game tomorrow.",
    }
    ad_copy = {
        "headline": "h",
        "body_text": "b",
        "social_caption": "c",
        "call_to_action": "Go",
        "trend_connection": "t",
        "audience_appeal_rationale": "a",
        "detailed_performance_rationale": "d",
    }
    ctx = FakeToolContext(
        {
            "gcs_folder": "f",
            "agent_output_dir": "creative_output",
            "final_visual_concepts": {"visual_concepts": [concept]},
            "ad_copy_critique": {"ad_copies": [ad_copy]},
            "brand": "Acme",
            "target_audience": "a",
            "target_product": "p",
            "key_selling_points": "k",
            "target_search_trends": "t",
            "generated_images": {"Countdown": {"gcs_uri": "gs://b/x.png"}},
            "trend_situation": {
                "timely_ok": True,
                "valid_until": "2026-10-14T08:05:00Z",
            },
        }
    )
    assert asyncio.run(tools.save_creative_gallery_html(ctx))["status"] == "success"
    (html,) = written
    assert (
        '<p class="timely-label">Timely: Game tomorrow. · valid until '
        "Oct 14, 08:05 UTC</p>" in html
    )


def test_timely_label_formats_in_the_valid_until_offset():
    concept = {"uses_trend_state": True, "timely_reason": "Kickoff <soon>"}
    assert tools._build_timely_label(concept, "2026-10-13T02:30:00+05:30") == (
        '<p class="timely-label">Timely: Kickoff &lt;soon&gt; · valid until '
        "Oct 13, 02:30 UTC+05:30</p>"
    )


def test_timely_label_keeps_old_output_without_a_usable_valid_until():
    concept = {"uses_trend_state": True, "timely_reason": "Game tomorrow."}
    old = '<p class="timely-label">Timely: Game tomorrow.</p>'
    for value in ("", None, "not a date", "2026-10-13T10:00:00", 42):
        assert tools._build_timely_label(concept, value) == old
    assert tools._build_timely_label(concept) == old
    assert tools._build_timely_label({"timely_reason": "x"}, "2026-10-13T10:00Z") == ""
