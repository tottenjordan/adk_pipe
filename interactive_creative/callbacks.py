"""Callbacks specific to interactive_creative."""

import json

from google.adk.agents.callback_context import CallbackContext
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
