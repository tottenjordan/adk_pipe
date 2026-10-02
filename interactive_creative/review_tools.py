from google.adk.tools.long_running_tool import LongRunningFunctionTool
from google.adk.tools.tool_context import ToolContext


def review_research(tool_context: ToolContext) -> None:
    """Pause for user to review the research report. When this tool returns a response, you MUST continue to the next workflow step (ad_creative_pipeline) — do not re-run the research; any feedback is carried forward via `memorize` (key 'research_feedback'). The response contains 'status' ('approved' | 'revision_requested'), optional 'feedback' (free text), optional 'report_edited' (boolean; true when the user edited the report, which is already applied to session state — call `save_draft_report_artifact` again so the PDF matches), and 'instruction'."""
    tool_context.actions.skip_summarization = True
    return None


def review_ad_copies(tool_context: ToolContext) -> None:
    """Pause for user to review ad copies. When this tool returns a response, you MUST continue to the next workflow step (visual_generation_pipeline); any feedback is carried forward via `memorize` (key 'ad_copy_feedback'). The response contains 'status' ('approved' | 'revision_requested'), optional 'feedback' (free text), and 'instruction'."""
    tool_context.actions.skip_summarization = True
    return None


def review_visual_concepts(tool_context: ToolContext) -> None:
    """Pause for user to review visual concepts. When this tool returns a response, you MUST call `visual_concept_reviser`, then `visual_generator_resilient`. The response contains 'status' and 'instruction'; any direct concept edits the user made are already applied to session state ('final_visual_concepts'), and any free-text revision notes are summarized in 'visual_revision_notes'."""
    tool_context.actions.skip_summarization = True
    return None


review_research_tool = LongRunningFunctionTool(review_research)
review_ad_copies_tool = LongRunningFunctionTool(review_ad_copies)
review_visual_concepts_tool = LongRunningFunctionTool(review_visual_concepts)
