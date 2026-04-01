from google.adk.tools.long_running_tool import LongRunningFunctionTool
from google.adk.tools.tool_context import ToolContext


def review_research(tool_context: ToolContext) -> None:
    """Pause for user to review the research report. When this tool returns a response, you MUST continue to the next workflow step (ad_creative_pipeline). The response contains 'status' and 'instruction' fields — follow the instruction."""
    tool_context.actions.skip_summarization = True
    return None


def review_ad_copies(tool_context: ToolContext) -> None:
    """Pause for user to review ad copies. When this tool returns a response, you MUST continue to the next workflow step (visual_generation_pipeline). The response contains 'status' and 'instruction' fields — follow the instruction."""
    tool_context.actions.skip_summarization = True
    return None


def review_visual_concepts(tool_context: ToolContext) -> None:
    """Pause for user to review visual concepts. When this tool returns a response, you MUST continue to the next workflow step (visual_generator). The response contains 'status' and 'instruction' fields — follow the instruction."""
    tool_context.actions.skip_summarization = True
    return None


review_research_tool = LongRunningFunctionTool(review_research)
review_ad_copies_tool = LongRunningFunctionTool(review_ad_copies)
review_visual_concepts_tool = LongRunningFunctionTool(review_visual_concepts)
