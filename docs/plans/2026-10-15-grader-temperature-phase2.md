# Grader Temperature Test (Phase 2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: `subagent-driven-development`. PR 1 (seams) and PR 2 (harness) run **in parallel**; they share only the two keyword APIs below. Then do Tasks 3–5 in order. First step: copy this plan to `docs/plans/2026-10-15-grader-temperature-phase2.md` and commit it in PR 1. Review with `code-review-and-quality` and merge only after CI is green.

**Goal:** decide, with evidence, whether the two graders should move from their tuned temperatures to the Gemini 3 default of 1.0:
- the creative_eval judge, at 0.3 today;
- post-render image QA, at 0 today.

**Architecture:** run offline on stored creatives, with no new pipeline runs.
- **Inputs:** load the full session state of 10 human-rated production runs straight from the Agent Engine session store.
- **Judge:** re-judge every creative 3× at each temperature with the current judge code.
- **Image QA:** re-inspect every rendered image 3× at each temperature.
- **Analysis:** measure stability (flip rates, score spread) and agreement with your human ratings (Cohen's kappa), then apply a pre-registered rule.
- **Code change:** two small keyword seams make the temperatures injectable without changing any default.

**Tech stack:** `creative_eval` (sync genai client, gemini-3.1-pro-preview), `creative_agent/image_qa.py` (gemini-3.8-flash), ADK `VertexAiSessionService`, `runserver/calibration.py` (kappa), `experiments/temperature_ab/analyze.py` (bootstrap).

---

## Context

**Why:**
- The source-driven audit found Google recommends temperature 1.0 for every Gemini 3 model. The agent-producer A/B (Phase 1) covers the agents.
- The graders were left out on purpose. They are deterministic checkers, so the question for them is not creativity but whether verdicts stay **stable** across repeats and **agree with humans**.
- A noisier judge would make pass rates and calibration less trustworthy. A noisier image QA would change which renders get redone.

**What exists:**
- **Ratings:** 313 human-rated creatives in `creative_ratings`, 41 sessions, each with ad-copy and visual verdicts (pass/fail, fail reasons, 1–5 score). All of them come from production runs.
- **Judge entry point:** `creative_eval/evaluate.py` `evaluate_all_concurrently(...)` can be called directly with a state-derived input set. The temperature is hard-coded (`temperature=0.3` at ~:530 and ~:689).
- **Image QA entry point:** `creative_agent/image_qa.py` `inspect_image(...)` (temperature 0 at ~:780) runs standalone on downloaded bytes. `qa_failed_rules(...)` turns its result into a pass or fail.
- **Pieces to reuse:**
  - `creative_eval.agent._campaign_context` / `_state_items` (state → judge inputs)
  - `runserver/ratings.py` `creative_index` / `_find_eval` (rating ↔ creative matching)
  - `runserver/calibration.py` `cohen_kappa` / `spearman`
  - `experiments/temperature_ab/analyze.py` `bootstrap_diff`
  - `experiments/person_reference/swap_compare.py:287–320` (standalone image QA template)

**Decisions (user, 2026-10-15):**
- **Dataset:** 10 rated production sessions, which is about 76 creatives: about 38 ad copies and 38 visuals.
- **Judge:** 3 repeats × 2 temperatures, about 460 judge calls at about 4 a minute (Pro quota is 5 RPM project-wide), so about 2 hours.
- **Image QA:** the same visuals, 3 × 2 = about 230 Flash calls, a few minutes.
- **Precision:** agreement intervals will be wide. Treat the result as a screen for clear harm, not fine tuning.

**Confound handled:** the stored verdicts came from older judge versions. So the **baseline is the current code at 0.3, run 3×**, not the stored verdict. The stored verdict is reported only as context.

## Pre-registered decision rule (fixed before any call)

**Judge:** adopt 1.0 only if **both** hold. Otherwise keep 0.3.
1. **Stability:** pass/fail flip rate across the 3 repeats (the share of creatives whose `passed` is not unanimous) ≤ the 0.3 rate + 0.05. The gate-level flip rate (any blocking gate not unanimous) must pass the same test.
2. **Agreement:** kappa of the majority verdict vs the human verdict ≥ the 0.3 kappa − 0.05. Raw agreement and the bootstrap CI of the difference are reported alongside.

If 1.0 is adopted, bump `JUDGE_VERSION`: it changes scoring behaviour, which past calibration can't carry over.

**Image QA:** adopt 1.0 only if **both** hold. Otherwise keep 0.
1. **Stability:** rule flip rate (the share of images whose `qa_failed_rules` set is not identical across repeats) ≤ the 0 rate + 0.05.
2. **Agreement:** kappa of the QA pass (majority) vs the human visual verdict ≥ the 0 kappa − 0.05. This proxy is noisy, because humans judge more than QA rules do. Fail-reason overlap (`product_not_visible`, `text_problem`, `unwanted_logo`) is reported as context.

**Any check that can't be computed** (for example, too few human fails) → INCONCLUSIVE → keep the current temperature.

## API contract (fixed so PRs 1 and 2 can run in parallel)

- `creative_eval.config.EvalConfig.judge_temperature: float = 0.3`. Both judge calls use `config.judge_temperature`. There is no env knob; it's set in code by the harness only.
- `creative_agent.image_qa.inspect_image(..., temperature: float = 0.0)` is keyword-only. The production call sites don't pass it.

## PR 1: temperature seams (~30 min)

**Files:** `creative_eval/config.py`, `creative_eval/evaluate.py` (~:530, ~:689), `creative_agent/image_qa.py` (~:721, ~:780), tests in `tests/test_creative_eval.py` / `tests/test_image_qa.py`, and the plan copy.

**Steps (TDD):**
1. Write failing tests:
   - a fake client records `config.temperature` for both judge kinds: 0.3 by default and 1.0 when `EvalConfig(judge_temperature=1.0)`;
   - `inspect_image` sends 0 by default and the value passed when given.
2. Implement.
3. Check defaults are unchanged:
   - `tests/test_render_parity.py` (golden) and the existing `cfg.temperature == 0` assertion still pass;
   - the judge fingerprint is unchanged, because temperature isn't in it and the default stays 0.3;
   - no `JUDGE_VERSION` bump, because behaviour is unchanged.
4. Mutation check: hard-code 0.3 again, and the injection test must fail.
5. Run the gates in a clean env, then open a PR (no attribution) and merge after CI.

## PR 2: offline harness (~60 min)

**Files:** `experiments/grader_temperature/{__init__,select,fetch,rejudge,reinspect,analyze}.py`, `tests/test_grader_temperature.py`, `.gitignore` (`experiments/grader_temperature/data/`, `results/`), and an `experiments/README.md` section.

**Modules** (pure logic with injectable I/O; tests use fakes only):

- **`select.py`:** query `creative_ratings` (parameterised, `app_name='creative_agent'`). Pick 10 sessions deterministically:
  - each must have both kinds rated;
  - prefer the most recent;
  - stratify so the set has at least 12 human fails across both kinds, which matters for kappa;
  - seeded and printed;
  - write `data/selection.json` with the session ids and rating rows.
- **`fetch.py`:** load each session's full state with ADK `VertexAiSessionService(project, location, agent_engine_id=<SESSION_SERVICE_URI engine>)` `get_session(app_name, user_id, session_id)`, read-only. Cache to `data/state/<sid>.json`. A missing session is logged and skipped, with a replacement session picked.
- **`rejudge.py`:**
  - **Inputs:** build the judge inputs from the cached state exactly as `creative_eval/agent.py` does (`_campaign_context`, `_state_items`, `parse_brief`, `generated_images`, `trend_situation`, `sources`, `person_reference`, safer-angle helpers).
  - **Calls:** `evaluate_all_concurrently(..., config=EvalConfig(judge_temperature=t, max_eval_workers=2, max_visual_wave_workers=2))` with one `with _get_client(config)` client per batch.
  - **Order:** arms interleaved per session (0.3, 1.0, 1.0, 0.3, …), so quota and time of day hit both equally.
  - **Output:** one result file per (session, temperature, repeat) at `results/judge/<sid>/<t>-r<k>.json` (resumable; skip existing). Failed calls (`evaluation_failed`) are recorded and excluded from the stats.
- **`reinspect.py`:**
  - **Inputs:** for each visual, download the PNG from `generated_images[c].gcs_uri` once to `data/img/` (≤ 20 MB cap). Build the inputs the way production does: `brand`, `target_product`, `strictness_flags(rating_strictness)`, `safer_angle_active`, and the person photo only for cast concepts. Skip cast concepts whose consent is revoked.
  - **Calls:** `inspect_image(..., temperature=t)` 3× per temperature.
  - **Output:** store the `ImageQAResult` plus `qa_failed_rules` in `results/qa/<sid>/<concept>/<t>-r<k>.json` (resumable).
  - **Caveat:** QA runs on the final kept render only; first attempts were overwritten.
- **`analyze.py`:**
  - Match each human rating to its creative with `runserver.ratings.creative_index` / `_find_eval`.
  - **Judge, per temperature:** pass flip rate, gate flip rate, per-gate flip counts, overall-score SD (mean over creatives), majority verdict, kappa and agreement vs human (`calibration.cohen_kappa`), and Spearman between judge score and human 1–5 score.
  - **QA, per temperature:** rule flip rate, QA-pass kappa vs the human visual verdict, and fail-reason overlap.
  - **Bootstrap:** CIs of 1.0 − baseline by resampling creatives (`bootstrap_diff`).
  - **Output:** apply the pre-registered rule and write `results/summary.md` + `records.csv`.

**Tests:**
- Selection stratification and determinism.
- State → judge-input mapping on a fixture state, which must match the agent's own mapping.
- Resume.
- The flip-rate and majority maths.
- The rating ↔ creative matching.
- The rule with all checks, including INCONCLUSIVE.
- Mutation checks on the rule comparisons and the flip definition.

**Gates:** as in PR 1. Open a PR (no attribution) and merge after CI.

## Task 3: run (after both PRs merge, ~2 h, unattended)

1. **Preconditions:**
   - Confirm the Phase 1 A/B and the safer-angle live checks are done. All three share the Pro quota.
   - Do an api idle check, and prefer a quiet window.
2. **Select and fetch:**
   - `python -m experiments.grader_temperature.select --n 10 --seed 7`
   - then `fetch`
   - Check that every selected session loaded.
3. **QA first:** `reinspect` (fast, Flash).
4. **Judge:** `rejudge`. Resumable, so re-running fills only the missing results.
5. **Monitor:** watch for 429s; the genai retry backs off. If a lot of calls fail, pause and resume later.

## Task 4: analyze and write up

- **Analysis:** `analyze` writes the verdict per grader against the rule.
- **Write-up:** `docs/experiments/2026-10-1x-grader-temperature.md`, covering the setup, the rule, the results table, the verdict, and the limits:
  - n = 10 sessions;
  - human ratings come from one rater;
  - QA runs on final renders only;
  - the QA-vs-human comparison is a proxy.
- Open a PR.

## Task 5: adopt (only if a grader passes), deploy, clean up

- **If the judge moves to 1.0:**
  - Change the `EvalConfig.judge_temperature` default and bump `JUDGE_VERSION` (+ the test pins `tests/test_eval_timely_gate.py`, `tests/test_eval_label_text.py`), with a CLAUDE.md note.
  - Redeploy the creative and interactive engines (Cloud Build, `_SMOKE=true`, then `engine_handoff apply --update-worker`) and the api (idle check, pin).
  - Calibration then restarts at the new version.
- **If image QA moves to 1.0:** change the default in `inspect_image`, update the pinned assertion, and record the deliberate request change in the render-parity golden. Same deploy.
- **If neither passes:** record the result in CLAUDE.md, a one-line note next to each hard-coded temperature, and memory. No deploy.
- **Clean up:** delete `experiments/grader_temperature/data/`, which holds cached state and images (it includes cast images of a consented person, so don't keep it), and update memory.

## Verification

- **PR 1:** defaults are unchanged (render-parity golden, existing temperature assertions, the judge fingerprint), the injection tests pass, and the mutation check is killed.
- **PR 2:** every module is unit-tested offline. A dry run against one cached session with fake clients produces a full summary.
- **Run:** every planned (session, temperature, repeat) result exists. Failed calls are fewer than 5% and listed.
- **Outcome:** a clear verdict per grader under the pre-registered rule, with CIs, in the write-up.

**Estimated wall clock:**

| Step | Time |
|---|---|
| PRs 1 and 2, in parallel | ~60 min |
| Reviews and merges | ~15 min |
| Run (unattended) | ~2 h |
| Analysis and write-up | ~20 min |
| Any adoption and deploy | ~45 min |
