"""interactive_creative checkpoint-1 contract: an edited research report
(``report_edited: true`` in the resume response) makes the root agent re-save
the research PDF instead of re-running research."""

import re

from interactive_creative.prompts import ROOT_AGENT_INSTR
from interactive_creative.review_tools import review_research


def _checkpoint1_step() -> str:
    match = re.search(r"^\s*3\. \*\*CHECKPOINT 1:\*\*.*$", ROOT_AGENT_INSTR, re.M)
    assert match, "CHECKPOINT 1 workflow step not found"
    return match.group(0)


def test_checkpoint1_step_resaves_pdf_when_report_edited():
    step = _checkpoint1_step()
    assert "report_edited" in step
    assert "save_draft_report_artifact" in step
    assert "Do NOT re-run the research pipeline" in step


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
