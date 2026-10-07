"""interactive_creative checkpoint contracts: at checkpoint 1 an edited brief or
research report (``brief_edited`` / ``report_edited: true`` in the resume
response) makes the root agent re-save the research PDF instead of re-running
research; at checkpoint 2 a revision request with feedback revises the copies
once and re-presents them once."""

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
    assert "brief_edited" in step
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
    assert len(calls) == 2  # step 1's "do NOT call" + the brief/report-edited re-save


def test_save_draft_report_artifact_is_forbidden_unless_report_edited():
    """Regression: the PDF re-save is conditional on report_edited and is the only
    allowed call (an unconditional re-save would overwrite the pipeline's PDF)."""
    step = _checkpoint1_step()
    condition = step.index(
        "If the response has `brief_edited: true` or `report_edited: true`"
    )
    call = step.index("call `save_draft_report_artifact`")
    assert condition < call
    assert "this is the only time to call it" in step[call:]
    tool_line = next(
        line
        for line in ROOT_AGENT_INSTR.splitlines()
        if line.lstrip().startswith("3. `save_draft_report_artifact`")
    )
    assert "Only after the user edited the brief or the report at checkpoint 1" in (
        tool_line
    )


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


def test_review_research_docstring_mentions_report_and_brief_edits():
    doc = review_research.__doc__ or ""
    assert "report_edited" in doc
    assert "brief_edited" in doc
    assert "'creative_brief'" in doc  # the resume edits field
    assert "full brief object" in doc


def _checkpoint2_step() -> str:
    workflow = _workflow()
    start = workflow.index("4. **CHECKPOINT 2:**")
    return workflow[start : workflow.index("5. Use `visual_generation_pipeline`")]


def test_checkpoint2_revises_once_then_re_presents_once():
    step = _checkpoint2_step()
    prep = step.index("prepare_copy_revision(feedback=<feedback>)")
    revise = step.index("call `ad_copy_user_reviser` once")
    again = step.index("call `review_ad_copies` ONE more time")
    assert prep < revise < again
    assert "FIRST `review_ad_copies` call" in step
    assert '"revision_requested"' in step and "`feedback` is non-empty" in step
    # The second review always proceeds: no loop.
    assert "SECOND `review_ad_copies` call, whatever its status" in step
    assert "never call `review_ad_copies` a third time" in step
    assert 'memorize(key="ad_copy_feedback"' in step
    assert "{" not in step


def test_root_tool_list_names_the_copy_revision_tools():
    tools = ROOT_AGENT_INSTR.split("<AVAILABLE_TOOLS>", 1)[1].split(
        "</AVAILABLE_TOOLS>"
    )[0]
    assert "`prepare_copy_revision`" in tools
    assert "`ad_copy_user_reviser`" in tools
    assert "creative brief" in tools


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
