from google.adk.tools.long_running_tool import LongRunningFunctionTool
from google.adk.tools.tool_context import ToolContext


def review_trends(tool_context: ToolContext) -> None:
    """Pause the run so a human can pick which gathered trends to keep.

    Opt-in checkpoint (gated by the `interactive_trend_pick` session flag). It
    pauses the run after `gather_trends_agent` has populated `state["raw_gtrends"]`
    (~25 Google Search terms) so the user can choose the subset to keep instead of
    letting `pick_trends_agent` auto-pick 3.

    When the run resumes, this tool call receives a function response of the shape:
        {
            "status": str,                 # "selected"
            "selected_trends": list[str],  # the terms the user chose to keep
            "instruction": str,            # optional free-text user note (may be "")
        }
    Then follow the orchestrator instruction: save each selected term with
    `save_search_trends_to_session_state`, then research and write up exactly
    those trends. Do not persist anything from this step.
    """
    tool_context.actions.skip_summarization = True
    return None


review_trends_tool = LongRunningFunctionTool(review_trends)
