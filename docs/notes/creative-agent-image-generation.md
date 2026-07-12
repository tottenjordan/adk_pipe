# creative_agent image generation — determinism fixes

Written 2026-07-12 on the `creative-eval` branch. These are **uncommitted
working-tree changes** to `creative_agent/agent.py` (not yet on `main`).

## Background: two related bugs on creative-eval

The `creative-eval` branch inserted `creative_eval_agent` into the root workflow
right after visual concepts. Two image-generation bugs surfaced:

### 1. Images never generated (LLM skipped the step)

Originally image rendering was a standalone `visual_generator` Agent exposed as
a root `AgentTool`, invoked as its own WORKFLOW step. With eval inserted right
after it, the orchestrator LLM **skipped `visual_generator`** and jumped from
visual concepts straight to `creative_eval_agent` — `generate_image` never ran,
no images saved.

**Fix:** make image rendering deterministic by wrapping concepts + rendering in
a `SequentialAgent` so the LLM can't skip a step:

```python
visual_production_pipeline = SequentialAgent(
    name="visual_production_pipeline",
    sub_agents=[visual_generation_pipeline, visual_generator],
)
```

Root now calls `AgentTool(agent=visual_production_pipeline)` (one WORKFLOW step)
and the standalone `visual_generator` AgentTool was removed from root tools.
`visual_generation_pipeline` stays **concepts-only**
(`SequentialAgent([drafter, critic, finalizer])`) because it is *shared* with
`interactive_creative`.

### 2. Every image rendered 2× (parallel duplicate tool calls)

After the deterministic fix, each image rendered twice. `generate_image` has an
idempotency guard (`if tool_context.state.get("_images_generated"): return`),
but it only dedupes **sequential** re-calls. gemini-3 emitted **two parallel**
`generate_image` calls in one turn; parallel function calls all read state
*before* any of them commits its delta, so the guard can't catch them. Output
was still correct (guard eventually True, 6 unique concepts → 6 PNGs) — it was a
cost/waste bug only.

**Fix:** give `visual_generator` a zero-budget planner + a "call exactly once"
instruction so it emits a single deterministic tool call:

```python
planner=BuiltInPlanner(
    thinking_config=types.ThinkingConfig(thinking_budget=0, include_thoughts=False)
),
instruction="""... Call the `generate_image` tool EXACTLY ONCE — a single
    function call, never in parallel and never more than once. ..."""
```

`thinking_budget=0` is the same lever used on the `trend_trawler` root agent to
stop gemini-3 MAX_TOKENS runaway and multiple/parallel tool calls in mechanical
single-tool steps.

## Do NOT touch interactive_creative for this

`interactive_creative/agent.py` imports `visual_generation_pipeline` and
`visual_generator` from `creative_agent.agent` and deliberately runs:
concepts → CHECKPOINT 3 (`review_visual_concepts` LongRunningFunctionTool) →
`visual_generator` images. Image gen MUST stay a separate step *after* the human
review checkpoint there, so `interactive_creative` keeps them separate on
purpose and was left unmodified. The `thinking_budget=0` fix on
`visual_generator` propagates to it for free via the shared import.

## The image client is Gemini, not Imagen

`generate_image` (in `creative_agent/tools.py`) uses
`client.models.generate_content(model=config.image_gen_model,
contents=prompt, config=GenerateContentConfig(response_modalities=["IMAGE"]))`
and reads bytes from `candidates[0].content.parts[].inline_data.data`. This is
the Gemini image path (`gemini-3.1-flash-image`), NOT Imagen's
`generate_images` / `generated_images`. `gemini-3.1-flash-image` needs
`GOOGLE_CLOUD_LOCATION=global` (module-level `genai.Client(vertexai=True,
location=config.LOCATION)` where LOCATION=global).
