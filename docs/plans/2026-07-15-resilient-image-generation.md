# Resilient Image Generation (visual_generator retry-on-empty) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop `creative_agent` / `interactive_creative` runs from finishing "done" with an empty image gallery when the `visual_generator` model turn flakes (`MALFORMED_FUNCTION_CALL`) and never invokes `generate_image`.

**Architecture:** Reuse the existing `agent_common.RetryUntilKeyAgent` producer-retry pattern to wrap `visual_generator`, keyed on the `_images_generated` state flag that `generate_image` already sets on success. Generalize two small shared helpers so the flag (a bool) works with the wrapper and so the exhaustion banner reads sensibly for a non-research step.

**Tech Stack:** Python 3.13, Google ADK (`BaseAgent`, `SequentialAgent`, `AgentTool`), `uv`, `pytest`, `ruff`.

---

## Context (why)

A live prod run (session `2032568396381421568`) finished with a green checkmark but a **broken/empty gallery**. Root cause, traced from `trend-trawler-api` logs:

1. **18:41:59** — `visual_generator` (gemini-3.1-pro-preview) model turn returned `finish_reason=MALFORMED_FUNCTION_CALL, has_func_call=False`. It was supposed to emit exactly one `generate_image` tool call; the malformed call meant ADK saw **no call at all**, so `generate_image` never ran and **zero PNGs** were written to GCS (confirmed: `gs://trend-trawler-deploy-ae/2026_07_15_18_37_c35e/creative_output/` holds only the eval JSON, gallery HTML, and research PDF).
2. **Nothing caught it.** `visual_generator` has `retry_config=INFRA_RETRY`, but that only retries on infra *exceptions* (5xx/ServerError). `MALFORMED_FUNCTION_CALL` is a normal finish reason and raises nothing — no retry fired.
3. **18:43:19** — `save_creative_gallery_html` → `_get_high_res_img()` tried to download each PNG to upscale it, hit **404 No such object** ×4, fell back to the standard-res URL (which also 404s), and shipped 4 broken `<img>` tags.
4. Run reported `done`. No error surfaced.

This is a **model-side flake, runtime-independent** (not caused by the async-job/poll migration; it's intermittent — earlier smoke runs produced images). It is the *same class* of failure the codebase already solved for research producers: `agent_common/retry_agent.py:14` **explicitly names `MALFORMED_FUNCTION_CALL`** as a mode `RetryUntilKeyAgent` is meant to catch. `visual_generator` simply was never wrapped.

**Intended outcome:** a flaked image turn is retried (fresh model turn usually emits a valid call); `generate_image`'s existing idempotency guard prevents double-spend; and if all attempts fail, the run degrades **observably** (banner on gallery + eval report + BQ `research_gaps`) instead of silently-empty.

## Key existing code to reuse (do NOT reinvent)

- `agent_common/retry_agent.py` — `RetryUntilKeyAgent(sub_agents=[inner], output_key=..., max_attempts=3)`. Re-runs `sub_agents[0]` until `output_key` is populated in state; on exhaustion leaves the key unset and emits `<output_key>__retry_exhausted`. This is the whole mechanism — we only need to wire it up + one typing generalization.
- `creative_agent/tools.py:201` `generate_image` — already sets `tool_context.state["_images_generated"] = True` and `_generated_artifact_keys` on success, and short-circuits if `_images_generated` is already set (idempotency). `_images_generated` is our `output_key`.
- `agent_common/observability.py:112` `collect_degradation_warnings` — already turns any `*__retry_exhausted` marker into a note surfaced on the gallery banner / eval report `warnings` / BQ `research_gaps`. No wiring change needed; only a wording generalization.
- `creative_agent/agent.py:247` `enhanced_combined_searcher_resilient` — the reference example of wrapping a producer in `RetryUntilKeyAgent`. Mirror its style.

---

## Task 1: Generalize `RetryUntilKeyAgent._is_populated` to accept a truthy flag

**Problem:** `_is_populated` currently returns True *only* for non-blank **strings** (`retry_agent.py:72-75`). `_images_generated` is a **bool** `True`, so as-is the wrapper would treat every image run as "unpopulated", burn all `max_attempts`, and always emit a false `__retry_exhausted` marker. Broaden to "non-blank string, or any truthy non-string" — strings keep identical behavior (existing research usage unaffected).

**Files:**
- Modify: `agent_common/retry_agent.py:72-75`
- Test: `tests/test_retry_agent.py`

**Step 1 — Write failing tests.** In `tests/test_retry_agent.py`:
- Add a direct unit test of the static method:
  ```python
  import pytest
  from agent_common import RetryUntilKeyAgent

  @pytest.mark.parametrize(
      "value,expected",
      [
          ("REAL", True),
          ("  x ", True),
          ("", False),
          ("   ", False),
          (True, True),      # the image flag
          (False, False),
          (["k.png"], True), # non-empty artifact-keys list
          ([], False),
          (0, False),
          (None, False),
      ],
  )
  def test_is_populated_accepts_truthy_non_strings(value, expected):
      assert RetryUntilKeyAgent._is_populated(value) is expected
  ```
- Add an integration test with a producer that writes a **bool** flag (mirrors `generate_image`), reusing the existing `_run` harness and the `_FlakyProducer` pattern. Add a sibling double `_FlakyFlagProducer` (identical to `_FlakyProducer` but `value: bool = True` and it writes `{output_key: True}`), then:
  ```python
  def test_recovers_when_producer_writes_bool_flag():
      producer = _FlakyFlagProducer(name="imggen", output_key="_images_generated", fail_first=1)
      wrapper = RetryUntilKeyAgent(
          name="imggen_resilient", sub_agents=[producer],
          output_key="_images_generated", max_attempts=3,
      )
      session = _run(wrapper)
      assert producer.runs == 2
      assert session.state.get("_images_generated") is True
      assert session.state.get("_images_generated__retry_exhausted") is None

  def test_no_false_exhaustion_when_flag_set_first_try():
      producer = _FlakyFlagProducer(name="imggen", output_key="_images_generated", fail_first=0)
      wrapper = RetryUntilKeyAgent(
          name="imggen_resilient", sub_agents=[producer],
          output_key="_images_generated", max_attempts=3,
      )
      session = _run(wrapper)
      assert producer.runs == 1  # healthy path runs exactly once
  ```

**Step 2 — Run, expect FAIL** (`test_no_false_exhaustion...` fails: bool flag treated as unpopulated → runs==3):
`PYTHONPATH="$PWD" uv run pytest tests/test_retry_agent.py -q`

**Step 3 — Implement.** Replace `_is_populated`:
```python
@staticmethod
def _is_populated(value: object) -> bool:
    """Populated = a non-blank string, or any other truthy value.

    Research producers write a non-blank string summary; the image producer
    writes a boolean ``_images_generated`` flag (and a non-empty artifact-keys
    list). A blank/whitespace string, empty list, ``False``, ``0`` and ``None``
    all count as unpopulated so the wrapper retries.
    """
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)
```

**Step 4 — Run, expect PASS.** Full file green.

## Task 2: Generalize the exhaustion-note wording (step-neutral)

**Decision (confirmed with user):** the banner text must read correctly for a non-research step. Make `collect_degradation_warnings` emit a step-neutral message.

**Files:**
- Modify: `agent_common/observability.py:112-129` (the note f-string, ~lines 125-128)
- Test: `tests/test_observability.py:167-168`

**Step 1 — Update the assertions first** (`tests/test_observability.py`): change the two `.startswith("Research step '...'")` checks (lines 167-168) to `.startswith("Step '...'")`. Run → FAIL.

**Step 2 — Implement.** In `collect_degradation_warnings`, replace the appended note with:
```python
notes.append(
    f"Step '{step}' exhausted retries and produced no output."
)
```
(Drop the research-specific "downstream synthesis used partial data" tail so it reads sensibly for image gen; the research case still gets a clear, accurate note.)

**Step 3 — Run, expect PASS:** `PYTHONPATH="$PWD" uv run pytest tests/test_observability.py -q`

## Task 3: Wrap `visual_generator` in creative_agent and use it in the production pipeline

**Files:**
- Modify: `creative_agent/agent.py` — add wrapper after `visual_generator` def (~line 847), swap into `visual_production_pipeline` (~lines 868-875)
- Test: `tests/test_pipeline_structure.py`

**Step 1 — Write failing structure test.** In `tests/test_pipeline_structure.py`, add:
```python
def test_visual_production_pipeline_wraps_generator_in_retry():
    from creative_agent import agent as ca
    from agent_common import RetryUntilKeyAgent

    names = [a.name for a in ca.visual_production_pipeline.sub_agents]
    assert names == ["visual_generation_pipeline", "visual_generator_resilient"]

    w = ca.visual_production_pipeline.sub_agents[-1]
    assert isinstance(w, RetryUntilKeyAgent)
    assert w.output_key == "_images_generated"
    assert w.sub_agents[0] is ca.visual_generator
```
The finish-reason callback test (`test_creative_model_agents_have_finish_reason_callback`, lines 288-301) already lists `ca.visual_generator` directly and still holds — the wrapper is a `BaseAgent` with no model, so leave that list unchanged.

**Step 2 — Run, expect FAIL** (`visual_generator_resilient` undefined):
`PYTHONPATH="$PWD" uv run pytest tests/test_pipeline_structure.py -q`

**Step 3 — Implement.** In `creative_agent/agent.py`, after the `visual_generator` definition add:
```python
# Retry-on-empty for the image step: visual_generator (gemini-3.1-pro-preview)
# intermittently returns MALFORMED_FUNCTION_CALL and never emits the
# generate_image tool call, leaving _images_generated unset and shipping an
# empty gallery (run 2032568396381421568). retry_config only covers infra
# EXCEPTIONS, not a malformed-call finish reason — so wrap in RetryUntilKeyAgent,
# same pattern as enhanced_combined_searcher_resilient. generate_image's
# _images_generated idempotency guard makes a re-run safe (no double image spend);
# on exhaustion the wrapper emits _images_generated__retry_exhausted, which
# collect_degradation_warnings surfaces on the gallery/eval banner. Single shared
# instance (also used by interactive_creative via AgentTool) to avoid double-parenting.
visual_generator_resilient = RetryUntilKeyAgent(
    name="visual_generator_resilient",
    sub_agents=[visual_generator],
    output_key="_images_generated",
    max_attempts=3,
)
```
Then in `visual_production_pipeline.sub_agents`, replace `visual_generator` with `visual_generator_resilient` (keep `visual_generation_pipeline` first). `RetryUntilKeyAgent` is already imported (`from agent_common import build_gemini, RetryUntilKeyAgent`, line 14).

**Step 4 — Run, expect PASS.**

## Task 4: Wire `interactive_creative` to the shared resilient wrapper

`interactive_creative` invokes `visual_generator` standalone via `AgentTool` after a human-review checkpoint, so it has the identical flaw. Reuse the **same** wrapper instance (single parent — `AgentTool` does not reparent, mirroring how `visual_generator` is shared today).

**Files:**
- Modify: `interactive_creative/agent.py:7-12` (import) and `:86` (`AgentTool`); prompt references at lines 40, 71 if the exposed tool name changes
- Test: `tests/test_pipeline_structure.py`

**Step 1 — Write failing test:**
```python
def test_interactive_creative_uses_resilient_visual_generator():
    from interactive_creative import agent as ic
    from creative_agent.agent import visual_generator_resilient
    from google.adk.tools.agent_tool import AgentTool
    from agent_common import RetryUntilKeyAgent

    matching = [
        t for t in ic.root_agent.tools
        if isinstance(t, AgentTool) and t.agent is visual_generator_resilient
    ]
    assert matching, "interactive_creative must invoke the resilient image wrapper"
    assert isinstance(matching[0].agent, RetryUntilKeyAgent)
```

**Step 2 — Run, expect FAIL.**

**Step 3 — Implement.** In `interactive_creative/agent.py`: change the import (line 11) from `visual_generator` to `visual_generator_resilient`, and change `AgentTool(agent=visual_generator)` (line 86) to `AgentTool(agent=visual_generator_resilient)`. Verify the AgentTool's exposed tool name during Step 4: if it now surfaces as `visual_generator_resilient`, update the two orchestrator-prompt references (lines 40, 71) to match so the model still calls the right tool. Keep prompt/tool names consistent.

**Step 4 — Run, expect PASS.**

## Task 5: Full offline gate + import smoke

**Files:** none (verification only).

- **Lint/format:** `uvx ruff check . && uvx ruff format --check .`
- **Full test suite:** `PYTHONPATH="$PWD" uv run pytest tests/ -q`
  - Note: many tests import `creative_agent.agent` → `creative_agent.tools`, which constructs a module-level `genai.Client` at import — this needs GCP ADC on the host. If ADC is unavailable, run the credential-free subset (`tests/test_retry_agent.py`, `tests/test_observability.py`) and flag the creds-gated remainder for a host with ADC (per CLAUDE.md → Testing).
- **Import smoke:** `PYTHONPATH="$PWD" uv run python -c "import creative_agent.agent, interactive_creative.agent; print('ok')"` — confirm `visual_production_pipeline` ends with `visual_generator_resilient` and `interactive_creative` boots.

## Task 6 (USER-GATED — do only when the user asks): finalize + live validation

Per repo conventions and standing constraints:
- Branch off `main` (never commit to `main`); conventional commits; **no `Co-Authored-By` trailers**; commit `uv.lock` if it changes, never `.python-version`.
- Copy this plan to `docs/plans/2026-07-15-resilient-image-generation.md` for the repo record.
- Open a PR (body ends with the Claude Code trailer). Do **not** merge without human review.
- **Live smoke** (isolated tagged revision, prod untouched — auto-mode-allowed): deploy `trend-trawler-api` with `--no-cpu-throttling --min-instances 1 --no-traffic --tag`, run a `creative_agent` job, and confirm **4 PNGs land in the run's `creative_output/`** and the gallery renders. Then migrate traffic + prune the tag only on explicit user go-ahead.
- Update memory (`adk-pipe-work-status.md`: close the OPEN IMAGE BUG) and note that `visual_generator` is now retry-wrapped.

## Verification (end-to-end)

- **Offline (CI-safe, no creds):** `_is_populated` accepts the bool flag and rejects falsy values; a bool-writing producer recovers on retry and does **not** false-exhaust on the happy path (`tests/test_retry_agent.py`); the exhaustion note is step-neutral (`tests/test_observability.py`).
- **Structure (creds-gated import):** `visual_production_pipeline` = `[visual_generation_pipeline, visual_generator_resilient]` with `output_key="_images_generated"`; `interactive_creative` invokes the same wrapper instance (`tests/test_pipeline_structure.py`); import smoke boots both engines.
- **Behavior preserved:** healthy image run calls `generate_image` once (idempotency guard intact, no double spend); research-producer retries and their banner notes still work (wording now step-neutral but accurate).
- **Live (user-gated):** a `creative_agent` run writes 4 PNGs + a rendering gallery; a forced exhaustion surfaces the degradation banner instead of finishing silently-empty.

## Risks / call-outs

- **Shared wrapper instance / parenting:** `visual_generator_resilient` is a `sub_agent` of `visual_production_pipeline` (parent set there) and is used by `interactive_creative` only via `AgentTool` (no reparent) — mirrors today's `visual_generator` sharing. Do **not** create a second wrapper instance around the same `visual_generator` (double-parent).
- **AgentTool exposed name:** wrapping may change the tool name the interactive orchestrator sees (`visual_generator` → `visual_generator_resilient`). Keep the prompt tool references (interactive_creative lines 40, 71) consistent with the actual exposed name.
- **Wording change is shared:** research degradation notes lose the "downstream synthesis used partial data" tail. Confirmed acceptable with the user; the note stays accurate.
- **Not the async-job change:** this bug predates and is orthogonal to the poll migration; no `runserver/` or frontend changes are involved.
