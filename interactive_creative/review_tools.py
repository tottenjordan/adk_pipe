"""Checkpoint tools for interactive_creative.

The three human-review pauses (``LongRunningFunctionTool``s) plus
``prepare_copy_revision``, the deterministic state prep for the one
user-requested ad-copy revision at checkpoint 2.
"""

from typing import Any

from google.adk.tools.long_running_tool import LongRunningFunctionTool
from google.adk.tools.tool_context import ToolContext

from creative_agent import user_copy_revision_inputs

# How many user-requested ad-copy revisions one session gets at checkpoint 2.
# The root re-presents the revised copies once and then proceeds regardless, so
# the counter is the deterministic backstop against a review → revise loop.
USER_COPY_REVISION_LIMIT = 1
USER_COPY_REVISIONS_KEY = "ad_copy_user_revisions_used"


def review_research(tool_context: ToolContext) -> None:
    """Pause for user to review the creative brief and the research report. When this tool returns a response, you MUST continue to the next workflow step (ad_creative_pipeline) — do not re-run the research; any feedback is carried forward via `memorize` (key 'research_feedback'). The response contains 'status' ('approved' | 'revision_requested'), optional 'feedback' (free text), optional 'brief_edited' (boolean; true when the user edited the structured creative brief — sent as resume `edits` with field 'creative_brief' whose value is the full brief object, validated and already applied to session state as 'creative_brief' and 'creative_brief_md'), optional 'report_edited' (boolean; true when the user edited the research report — resume `edits` field 'combined_final_cited_report', already applied to session state), and 'instruction'. When 'brief_edited' or 'report_edited' is true, call `save_draft_report_artifact` again so the PDF matches."""
    tool_context.actions.skip_summarization = True
    return None


def review_ad_copies(tool_context: ToolContext) -> None:
    """Pause for user to review ad copies. The response contains 'status' ('approved' | 'revision_requested'), optional 'feedback' (free text), and 'instruction'. After the FIRST review, if status is 'revision_requested' with non-empty feedback, call `prepare_copy_revision(feedback=...)`, then `ad_copy_user_reviser`, then this tool ONCE more so the user sees the revised copies. Otherwise (and always after the second review) carry any feedback forward via `memorize` (key 'ad_copy_feedback') and continue to the next workflow step (visual_generation_pipeline)."""
    tool_context.actions.skip_summarization = True
    return None


def review_visual_concepts(tool_context: ToolContext) -> None:
    """Pause for user to review visual concepts. When this tool returns a response, you MUST call `visual_concept_reviser`, then `visual_generator_resilient`. The response contains 'status' and 'instruction'; any direct concept edits the user made are already applied to session state ('final_visual_concepts'), and any free-text revision notes are summarized in 'visual_revision_notes'."""
    tool_context.actions.skip_summarization = True
    return None


def prepare_copy_revision(feedback: str, tool_context: ToolContext) -> dict[str, Any]:
    """Prepare the one user-requested ad-copy revision at checkpoint 2. Call it only after the FIRST `review_ad_copies` response with status 'revision_requested' and non-empty feedback, passing that feedback verbatim. When it returns status 'ready', call `ad_copy_user_reviser` once, then `review_ad_copies` once more. When it returns status 'skipped', do not call `ad_copy_user_reviser`; continue to visual_generation_pipeline."""
    state = tool_context.state
    feedback = (feedback or "").strip()
    used = int(state.get(USER_COPY_REVISIONS_KEY) or 0)
    inputs = user_copy_revision_inputs(state.get("ad_copy_critique"), feedback)
    reason = ""
    if not feedback:
        reason = "No feedback to apply."
    elif used >= USER_COPY_REVISION_LIMIT:
        reason = "The ad copies were already revised once."
    elif inputs is None:
        reason = "There are no ad copies to revise."
    if reason or inputs is None:
        return {
            "status": "skipped",
            "reason": reason or "There are no ad copies to revise.",
            "next_step": "visual_generation_pipeline",
        }
    # Every copy is flagged with the feedback (it applies to all of them): the
    # reviser rewrites only listed copies and its restore_unflagged safety net
    # reverts any copy outside ad_copy_flagged_ids.
    for key, value in inputs.items():
        state[key] = value
    state[USER_COPY_REVISIONS_KEY] = used + 1
    return {
        "status": "ready",
        "copies_to_revise": len(inputs["ad_copy_flagged_ids"]),
        "next_step": "Call ad_copy_user_reviser once, then review_ad_copies once more.",
    }


review_research_tool = LongRunningFunctionTool(review_research)
review_ad_copies_tool = LongRunningFunctionTool(review_ad_copies)
review_visual_concepts_tool = LongRunningFunctionTool(review_visual_concepts)
