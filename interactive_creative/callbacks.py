"""Callbacks specific to interactive_creative."""

import json
from collections.abc import Mapping
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from agent_common import is_populated

_EMPTY_CONCEPTS_JSON = json.dumps({"visual_concepts": []})


def skip_reviser_without_notes(
    callback_context: CallbackContext,
) -> types.Content | None:
    """`before_agent_callback` for `visual_concept_reviser`.

    Skips the model when there are no checkpoint-3 revision notes or no finalized
    concepts to revise, so direct edits already merged into
    `final_visual_concepts` are never paraphrased away by an LLM re-emit. Returns
    None (run the reviser) only when both are present.

    The skip reply must be JSON valid against the reviser's `output_schema`
    (`VisualConceptFinalList`): ADK validates a before-agent reply against it for
    both the `output_key` write and the `AgentTool` result, so plain prose would
    raise. It therefore echoes the current concepts verbatim (re-saving the same
    value) or, when there are none, an empty `visual_concepts` list.
    """
    state = callback_context.state
    notes = state.get("visual_revision_notes")
    concepts = state.get("final_visual_concepts")
    has_notes = isinstance(notes, str) and bool(notes.strip())
    has_concepts = is_populated(concepts) and (
        not isinstance(concepts, dict) or is_populated(concepts.get("visual_concepts"))
    )
    if has_notes and has_concepts:
        return None
    if not has_concepts:
        text = _EMPTY_CONCEPTS_JSON
    elif isinstance(concepts, str):
        text = concepts
    else:
        text = json.dumps(concepts)
    return types.Content(role="model", parts=[types.Part(text=text)])


# The checkpoint-2 user-revision tool (a FailSoftNode around creative_agent's
# ad_copy_reviser, exposed to the root as a NodeTool).
USER_REVISER_TOOL = "ad_copy_user_reviser"
# Set by user_copy_revision_failed so the graph's terminal reports the failure.
USER_REVISION_FAILED_KEY = "ad_copy_user_revision_failed"

# The copy-revision inputs prepare_copy_revision writes for the reviser (same
# keys and cleared values as creative_agent's copy gate).
_COPY_REVISION_CLEARED: dict[str, Any] = {
    "ad_copy_issues": "",
    "ad_copy_flagged_ids": None,
    "ad_copy_critique__before_revision": None,
}


def clear_copy_revision_inputs(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: Any,
) -> None:
    """`after_tool_callback` on the interactive root.

    After the checkpoint-2 `ad_copy_user_reviser` call clears the
    per-copy issues, flagged ids and pre-revision snapshot that
    `prepare_copy_revision` wrote, so no stale revision input outlives the one
    user revision. `ad_copy_feedback` is kept on purpose: the visual steps read
    it, and the ad-copy pipeline (whose copy gate shares the reviser) does not
    run again after checkpoint 2. Returns None (the tool response is unchanged).
    """
    if tool.name != USER_REVISER_TOOL:
        return None
    for key, value in _COPY_REVISION_CLEARED.items():
        tool_context.state[key] = value
    return None


def user_copy_revision_failed(
    state_before: Mapping[str, Any], exc: Exception
) -> dict[str, Any]:
    """`on_error` of the checkpoint-2 `ad_copy_user_reviser` FailSoftNode.

    The user revision is optional polish on copies already in state, so a
    reviser exception (e.g. SCHEMA_RETRY exhausted) must not fail the run
    (inside ad_creative_pipeline the same agent sits behind a FailSoftNode
    too). Keep the pre-revision copies and clear the revision inputs; the root
    then re-presents the (unchanged) copies."""
    return {
        **_COPY_REVISION_CLEARED,
        "ad_copy_critique": state_before.get("ad_copy_critique"),
        USER_REVISION_FAILED_KEY: True,
    }
