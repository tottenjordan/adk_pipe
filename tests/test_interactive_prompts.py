"""interactive_creative checkpoint-1 contract: an edited research report
(``report_edited: true`` in the resume response) makes the root agent re-save
the research PDF instead of re-running research."""

import re

from interactive_creative.prompts import ROOT_AGENT_INSTR
from interactive_creative.review_tools import review_research


def _checkpoint1_step() -> str:
    match = re.search(r"^\s*2\. \*\*CHECKPOINT 1:\*\*.*$", ROOT_AGENT_INSTR, re.M)
    assert match, "CHECKPOINT 1 workflow step not found"
    return match.group(0)


def test_checkpoint1_step_resaves_pdf_when_report_edited():
    step = _checkpoint1_step()
    assert "report_edited" in step
    assert "save_draft_report_artifact" in step
    assert "Do NOT re-run the research pipeline" in step


def _workflow() -> str:
    return ROOT_AGENT_INSTR.split("<WORKFLOW>", 1)[1].split("</WORKFLOW>", 1)[0]


def test_research_pdf_is_saved_by_the_pipeline_not_the_root():
    """The research pipeline saves the PDF itself; the root re-saves it only on
    report_edited (checkpoint 1), never as a separate step after research."""
    first = re.search(r"^\s*1\. .*$", _workflow(), re.M)
    assert first and "combined_research_pipeline" in first.group(0)
    assert "do NOT call `save_draft_report_artifact`" in first.group(0)
    calls = [
        line
        for line in _workflow().splitlines()
        if "`save_draft_report_artifact`" in line
    ]
    assert len(calls) == 2  # step 1's "do NOT call" + the report_edited re-save


def test_finalize_pipeline_replaces_eval_and_persistence_steps():
    workflow = _workflow()
    for gone in (
        "creative_eval_agent",
        "save_eval_report_to_gcs",
        "save_creative_gallery_html",
        "write_trends_to_bq",
        "write_eval_report_to_bq",
    ):
        assert gone not in ROOT_AGENT_INSTR, gone
    render = workflow.index("`visual_generator_resilient` to generate")
    assert workflow.index("`finalize_pipeline`") > render
    assert "ALL 9 steps" in ROOT_AGENT_INSTR and "until step 9" in ROOT_AGENT_INSTR
    last = re.search(r"^\s*9\. .*$", workflow, re.M)
    assert last and "{gcs_bucket}/{gcs_folder}/{agent_output_dir}" in last.group(0)


def test_review_research_docstring_mentions_report_edited():
    assert "report_edited" in (review_research.__doc__ or "")


def test_root_instruction_has_no_unexpected_state_tokens():
    # ADK treats {name} / {name?} as state tokens; the edit clause must not add any.
    assert "{" not in _checkpoint1_step()


CAMPAIGN_KEYS = {
    "brand",
    "target_audience",
    "target_product",
    "key_selling_points",
    "target_search_trends",
}


def test_campaign_fields_already_in_state_are_not_rememorized():
    """Same contract as creative_agent's root: seeded campaign fields are shown
    via optional tokens and only the missing ones are memorized."""
    block = ROOT_AGENT_INSTR.split("<CURRENT_STATE>", 1)[1].split("</CURRENT_STATE>")[0]
    assert set(re.findall(r"\{(\w+)\?\}", block)) == CAMPAIGN_KEYS
    instructions = ROOT_AGENT_INSTR.split("<INSTRUCTIONS>", 1)[1]
    assert "Do NOT re-memorize" in instructions
    assert "missing from BOTH" in instructions
