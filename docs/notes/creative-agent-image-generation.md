# creative_agent image generation — determinism fixes

First written 2026-07-12 on the `creative-eval` branch (then uncommitted
changes to `creative_agent/agent.py`, using `SequentialAgent`). Refreshed
2026-10-12: the fixes are long merged, and the P2 Workflow migration
(2026-09-29) replaced `SequentialAgent`/`AgentTool` with graph `Workflow`s
exposed as `NodeTool`s. The sections below describe the **current** code and
keep the original bugs as background.

## Background: two related bugs on creative-eval (2026-07)

### 1. Images never generated (LLM skipped the step)

Originally image rendering was a standalone `visual_generator` Agent exposed as
a root `AgentTool`, invoked as its own root step. With eval inserted right
after it, the orchestrator LLM **skipped `visual_generator`** and jumped from
visual concepts straight to evaluation — `generate_image` never ran, no images
saved. The 2026-07 fix wrapped concepts + rendering in one `SequentialAgent`
so the LLM could not skip a step.

### 2. Every image rendered 2× (parallel duplicate tool calls)

`generate_image` has an idempotency guard
(`if tool_context.state.get("_images_generated"): return`), but it only dedupes
**sequential** re-calls. gemini-3 emitted **two parallel** `generate_image`
calls in one turn; parallel function calls all read state *before* any commits
its delta, so the guard could not catch them (cost bug only). The fix was a
minimal-thinking planner plus a "call EXACTLY ONCE" instruction.

## Current shape (graph Workflows)

`creative_agent/agent.py`:

```text
visual_production_pipeline (Workflow, input_schema=PipelineRequest)
  START → visual_generation_pipeline → render_barrier → visual_generator_resilient → images_ready

visual_generation_pipeline (Workflow; shared with interactive_creative)
  START → art_director → visual_concept_drafter → visual_concept_critic
        → visual_concept_finalizer → concept_gate
  concept_gate: "ok" → visual_concepts_ready
                "revise" → visual_concept_fixer_failsoft → concept_gate (≤ CONCEPT_REVISION_ROUNDS)
```

- **No skippable root step.** The root makes a single `creative_pipeline` call
  (after `memorize`); `visual_production_pipeline` is one of its stages, and in
  that pipeline it runs in a fan-out beside `finalize.evaluate_ad_copies_node`
  (early ad-copy judging; `ad_copy_eval_join` waits for both) before
  `finalize_pipeline`. The LLM never decides whether to render.
- **`render_barrier`** is a no-output node: `visual_generator_resilient`
  validates its input against `PipelineRequest` (it is also a root tool in
  `interactive_creative`), so the concepts payload must not reach it.
  `generate_image` reads `final_visual_concepts` from state.
- **`visual_generator_resilient`** is a deterministic `FunctionNode` (no LLM;
  since 2026-10-12) that awaits `image_tools.generate_image` on the node
  `Context` (the same class as `ToolContext`: state + `save_artifact`), so its
  state writes land in the node's state delta. `retry_config=INFRA_RETRY`
  retries transient infra exceptions; the `_images_generated` idempotency guard
  keeps a re-run a no-op. It replaced a Pro `visual_generator` `LlmAgent` whose
  only job was to call `generate_image()` once (~7.6 s per run in Cloud
  Trace), with a forced tool config and a `RetryUntilKeyNode` (6 attempts)
  around its intermittent `MALFORMED_FUNCTION_CALL` empty turns (issue #116),
  which a function node cannot have. Its output (the tool's message) is the
  function response when `interactive_creative` calls it as a `NodeTool`.
- **`images_ready`** is the truthy terminal (a short confirmation, or a
  missing-output notice).

## Inside `generate_image` (`creative_agent/image_tools.py`)

One tool call renders every concept. Per concept it runs
`creative_agent/render_concept.render_concept(...)` (no `ToolContext`; returns a
`RenderResult`):

1. Render with the reference block (`references.py`: up to 3 product/logo/style
   references) and, for a concept cast with the run's consented person, the
   person photo + `ALLOW_ADULT` (`person_render.py`; a safety block falls back to
   a render without the person).
2. **Post-render image QA** (`image_qa.inspect_image`, one structured vision
   call; `qa_failed_rules`) unless `IMAGE_QA_ENABLED=false`.
3. A failing image is re-rendered with `image_qa.correction_text` appended, at
   most `IMAGE_QA_MAX_RERENDERS` times per concept and
   `IMAGE_QA_MAX_RERENDERS_PER_RUN` per run; the better attempt is kept
   (critical failures weigh first) and only it is uploaded.

At most `IMAGE_RENDER_CONCURRENCY` image-model calls (default 2, clamped 1–4;
re-renders included) are in flight behind one FIFO `asyncio.Semaphore` (image
quota); first renders are staggered by concept, and each image's QA (+
re-render) overlaps other concepts' renders; the budget is claimed and uploads
happen in concept order. (Update 2026-10-14: until #389 (2026-10-13) renders
were strictly sequential behind one FIFO `asyncio.Lock`; `IMAGE_RENDER_CONCURRENCY=1`
restores that.) Results go to
`generated_images`, still-failing images to `image_qa__issues`, QA errors
(fail-open) to `image_qa__unavailable`. Behaviour parity with the pre-refactor
tool is pinned by `tests/test_render_parity.py`. Rule details:
[creative-quality-gates.md](creative-quality-gates.md).

## interactive_creative keeps concepts and rendering separate

`interactive_creative` imports `visual_generation_pipeline` and
`visual_generator_resilient` from `creative_agent`'s public facade
(`creative_agent/__init__.py`) and deliberately runs: concepts →
CHECKPOINT 3 (`review_visual_concepts`, a `LongRunningFunctionTool`) →
optional `visual_concept_reviser` → `visual_generator_resilient`. Image
generation must stay a separate step after human review, so it does not use
`visual_production_pipeline`. Fixes to the render node reach it via the
shared import.

## The image client is Gemini, not Imagen

`generate_image` calls `client.models.generate_content(model=config.image_gen_model, …)`
with `response_modalities=["IMAGE"]` and an `ImageConfig`, and reads bytes from
`candidates[0].content.parts[].inline_data.data`. This is the Gemini image path
(`gemini-nano-banana-2.1` since 2026-10; was `gemini-3.1-flash-image`), NOT
Imagen's `generate_images`. The client is pinned to `MODEL_LOCATION`
(`global`) in code (`image_tools._get_genai_client`), since gemini-3.x image
models are served only from `global`.
