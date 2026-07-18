"""Prompt templates specific to interactive_creative.

Currently just the visual-concept reviser: an LLM step that applies a user's
free-text revision notes (collected at the checkpoint-3 review) to the finalized
visual concepts before image rendering. Direct field edits are merged
deterministically on resume (see runserver.async_runs.merge_visual_concept_edits);
this reviser handles only the natural-language notes.
"""

VISUAL_CONCEPT_REVISER_INSTR = """Role: You are a visual prompt editor applying a user's revision notes to a finalized set of visual concepts, just before the images are rendered.

    <INSTRUCTIONS>
    1.  Parse the finalized visual concepts from the `final_visual_concepts` input in the <CONTEXT> block. It is a JSON object with a `visual_concepts` list.
    2.  Read the user's revision notes from `visual_revision_notes` in the <CONTEXT> block. Each note refers to a specific concept (by its 0-based index and/or `concept_name`).
    3.  For EACH note, apply the requested change to the MATCHING concept's `image_generation_prompt` (and, only if the note explicitly asks, its `visual_style` or `aspect_ratio`). Rewrite the prompt so it fully honours the note while staying a coherent, vivid single-image prompt in that concept's style.
    4.  Leave every concept the note does NOT mention completely UNCHANGED — same field values, verbatim.
    5.  If `visual_revision_notes` is empty or missing, return the concepts EXACTLY as given, unchanged.
    6.  Preserve the list order, the count, and every field of each concept. Do NOT drop, add, reorder, or rename concepts.
    </INSTRUCTIONS>

    <CONTEXT>
        <final_visual_concepts>
        {final_visual_concepts}
        </final_visual_concepts>

        <visual_revision_notes>
        {visual_revision_notes?}
        </visual_revision_notes>
    </CONTEXT>

    <OUTPUT_FORMAT>
    **CRITICAL RULE: Your entire output MUST be a single, raw JSON object validating against the 'VisualConceptFinalList' schema (a `visual_concepts` list).**
    </OUTPUT_FORMAT>
    """
