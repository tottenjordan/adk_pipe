"""creative_agent root workflow contract.

- Deterministic tail (2026-10-07): the Pro root ended runs early by returning
  empty turns after long tool results, and it made five separate decisions
  after rendering (creative_eval_agent + four persistence tools) plus a
  save_draft_report_artifact call after research. The research PDF is now saved
  inside combined_research_pipeline and the eval + persistence run as one
  finalize_pipeline graph, so the root makes exactly four workflow calls:
  research → ad copies → visuals → finalize, then the final text.
- Final message: the rubric requires the answer to reference the produced
  creatives and the exported artifacts, not just the gs:// URI.
"""

import re

from creative_agent.prompts import ROOT_AGENT_INSTR

WORKFLOW_TOOLS = (
    "combined_research_pipeline",
    "ad_creative_pipeline",
    "visual_production_pipeline",
    "finalize_pipeline",
)

RETIRED_TOOLS = (
    "save_draft_report_artifact",
    "creative_eval_agent",
    "save_eval_report_to_gcs",
    "save_creative_gallery_html",
    "write_trends_to_bq",
    "write_eval_report_to_bq",
)


def _workflow_steps() -> dict[int, str]:
    block = ROOT_AGENT_INSTR.split("<WORKFLOW>", 1)[1].split("</WORKFLOW>", 1)[0]
    steps: dict[int, str] = {}
    current = None
    for line in block.splitlines():
        m = re.match(r"\s*(\d+)\. (.*)", line)
        if m:
            current = int(m.group(1))
            steps[current] = m.group(2)
        elif current is not None:
            steps[current] += "\n" + line
    return steps


def _step_calling(tool: str) -> int:
    """The step that calls ``tool``: the first one naming it (a later step may
    refer back to its result)."""
    hits = [n for n, text in _workflow_steps().items() if f"`{tool}`" in text]
    assert hits, tool
    return hits[0]


def test_workflow_is_four_pipeline_calls_in_order():
    steps = [_step_calling(t) for t in WORKFLOW_TOOLS]
    assert steps == sorted(steps) == [1, 2, 3, 4]


def test_retired_tools_are_not_mentioned():
    for tool in RETIRED_TOOLS:
        assert tool not in ROOT_AGENT_INSTR, tool


def test_research_step_says_the_pdf_is_saved_inside_it():
    assert "PDF" in _workflow_steps()[_step_calling("combined_research_pipeline")]


def test_final_message_summarizes_outputs_and_uri():
    final = _workflow_steps()[max(_workflow_steps())]
    for phrase in (
        "ad copies",
        "visual concepts",
        "research report",
        "HTML gallery",
        "`finalize_pipeline` result",
    ):
        assert phrase in final
    assert "{gcs_bucket}/{gcs_folder}/{agent_output_dir}" in final


def test_root_is_told_not_to_stop_between_steps():
    assert "never an empty or text-only response" in ROOT_AGENT_INSTR
    assert "until `finalize_pipeline` has returned" in ROOT_AGENT_INSTR


CAMPAIGN_KEYS = {
    "brand",
    "target_audience",
    "target_product",
    "key_selling_points",
    "target_search_trends",
}


def test_no_new_state_tokens():
    # ADK treats {name} as a state token; only the URI keys (required) and the
    # optional campaign fields shown in <CURRENT_STATE> may appear.
    required = set(re.findall(r"\{(\w+)\}", ROOT_AGENT_INSTR))
    optional = set(re.findall(r"\{(\w+)\?\}", ROOT_AGENT_INSTR))
    assert required == {"gcs_bucket", "gcs_folder", "agent_output_dir"}
    assert optional == CAMPAIGN_KEYS


def test_campaign_fields_already_in_state_are_not_rememorized():
    block = ROOT_AGENT_INSTR.split("<CURRENT_STATE>", 1)[1].split("</CURRENT_STATE>")[0]
    for key in CAMPAIGN_KEYS:
        assert f"{{{key}?}}" in block
    instructions = ROOT_AGENT_INSTR.split("<INSTRUCTIONS>", 1)[1]
    assert "Do NOT re-memorize" in instructions
    assert "missing from BOTH" in instructions
