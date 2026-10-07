"""creative_agent root workflow contract (nightly-eval findings, 2026-10-03..05).

- Persistence batching: when the root issued save_eval_report_to_gcs,
  save_creative_gallery_html and write_trends_to_bq one per turn, the run took
  two extra root turns (~37k prompt tokens each, ~+25% total tokens) vs the
  batched runs; the efficiency gate tripped on that variance. The three are
  independent, so the prompt asks for them as parallel calls in one turn, with
  write_eval_report_to_bq (which reads their state) strictly after.
- Final message: the rubric requires the answer to reference the produced
  creatives and the exported artifacts, not just the gs:// URI.
"""

import re

from creative_agent.prompts import ROOT_AGENT_INSTR

PARALLEL_PERSISTENCE = (
    "save_eval_report_to_gcs",
    "save_creative_gallery_html",
    "write_trends_to_bq",
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
    hits = [n for n, text in _workflow_steps().items() if f"`{tool}`" in text]
    assert len(hits) == 1, (tool, hits)
    return hits[0]


def test_independent_persistence_tools_are_one_parallel_step():
    steps = {_step_calling(t) for t in PARALLEL_PERSISTENCE}
    assert len(steps) == 1
    assert "parallel" in _workflow_steps()[steps.pop()]


def test_write_eval_report_to_bq_runs_after_the_parallel_step():
    """It reads creative_row_uuid (write_trends_to_bq) and eval_report_gcs_uri
    (save_eval_report_to_gcs), so it must not share their turn."""
    parallel = _step_calling("save_eval_report_to_gcs")
    assert _step_calling("write_eval_report_to_bq") == parallel + 1
    assert _step_calling("creative_eval_agent") < parallel


def test_final_message_summarizes_outputs_and_uri():
    final = _workflow_steps()[max(_workflow_steps())]
    for phrase in ("ad copies", "visual concepts", "research report", "HTML gallery"):
        assert phrase in final
    assert "{gcs_bucket}/{gcs_folder}/{agent_output_dir}" in final


def test_root_is_told_not_to_stop_between_steps():
    assert "never an empty or text-only response" in ROOT_AGENT_INSTR


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
