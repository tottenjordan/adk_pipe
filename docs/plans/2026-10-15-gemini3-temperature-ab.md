# Gemini 3 Temperature A/B Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: `subagent-driven-development`. Run Tasks 1 and 2 in parallel (disjoint files), then do Tasks 3–6 in order. First step of execution: copy this plan to `docs/plans/2026-10-15-gemini3-temperature-ab.md` and commit it with Task 1.

**Goal:** measure whether following Google's Gemini 3 guidance ("keep temperature at its default 1.0") improves or harms the agents, then adopt the winner. The guidance comes with a warning that lower values can cause looping and degraded output.

**Architecture:**
- **Profile knob:** one env profile, `GEMINI_TEMPERATURE_PROFILE`: `tuned` (default) is today's per-agent values, `default` sets every gemini-3 text agent to 1.0. It is read at import through `agent_common`.
- **Arms:** one tagged, no-traffic api revision per arm, both on the same image. Only that env differs. Both write to the isolated eval dataset and bucket.
- **Driver:** a small serial interleaved driver in `experiments/temperature_ab/` that reuses the existing harness helpers. It runs about 10 `creative_agent` and 10 `trend_scout` runs per arm.
- **Analysis and adoption:** an analysis step applies the pre-registered decision rule below. The eval CI then runs once on the winning profile as a regression gate.

**Tech stack:** ADK 2.10 agents (google-genai 2.28), Cloud Run tagged revisions, the existing `experiments/creative_latency` + `experiments/quota_spread` harness, BigQuery `trend_trawler_eval`, `.github/workflows/adk-eval.yml` + `tests/eval/efficiency_gate.py`.

---

## Context

**What prompted this:** the source-driven audit (2026-10-15) found that the Gemini 3 docs strongly recommend temperature 1.0 and warn that lower values "may lead to unexpected behavior, such as looping or degraded performance" (https://ai.google.dev/gemini-api/docs/gemini-3). That matches symptoms we have worked around before: empty root turns, MAX_TOKENS before a tool call, and MALFORMED_FUNCTION_CALL.

**Current settings:**
- 19 agents set an explicit temperature, inline as `types.GenerateContentConfig(temperature=…)` literals with no env knob. They range from 0.0 to 1.5:
  - **Low:** trend_scout root 0.01, digest classifier 0.0, trend_situation synthesizer 0.2, digest synthesizer 0.2, picker 0.4, interactive reviser 0.4.
  - **High:** drafters 1.5, searcher 1.5, trend_scout synthesizer 1.5.
  - **Middle:** critics, revisers and the brief writer 0.7–0.9.
- 13 agents already use the default.
- Image QA (0) and the judge (0.3) are direct genai calls, **out of scope** for this phase (see Phase 2).

**Decisions made:** two arms (current vs all 1.0), and the DoE-harness vehicle with a single eval-CI gate. The eval CI alone is too weak a vehicle (n=2 per run, ~25 min per creative leg, rubrics that only check completion).

**Confounds removed by design:**

| Confound | Why it matters | Fix |
|---|---|---|
| Brand history | Reads the brand's last 5 `creative_evals` rows, so experiment runs would change one another's prompts | `BRAND_HISTORY_ENABLED=false` on both arms |
| Prod tables | Runs would pollute your real data and brand history | Both arms write to `trend_trawler_eval` + the eval bucket |
| Digest cache | A cache hit skips the 0.0-temperature classifier, the most sensitive change | `TREND_DIGEST_ENABLED=false` on both arms, so the classifier runs every time (the describe step isn't exercised; noted) |
| Quota time of day | Pro quota is shared across the project | Arms interleaved ABAB, run overnight |
| Prompt overfit | One campaign could favour one arm | Two campaigns alternated, balanced across arms |

## Pre-registered decision rule (fixed before any run)

Per app, compare `default` to `tuned`. Means are reported with a 95% bootstrap CI (10k resamples). With n≈10 only large effects are detectable; the rule is non-inferiority plus robustness, not "prove better".

**Adopt `default` if all of these hold:**
1. **Completion:** no more failed or unfinished runs than `tuned` + 1. A creative run counts only when `finalize_done` is set; a trend_scout run counts when `selected_gtrends` holds 3 trends.
2. **Robustness:** abnormal or empty turns per run, from the log scrape, are no higher. Schema re-draws (`ValidationError` retries) and `__retry_exhausted` markers are no higher.
3. **Quality, creative_agent:**
   - `gates_pass_rate` mean difference ≥ −0.05.
   - Judge `mean_score` ≥ −0.03.
   - `__issues` residual count not higher by more than 1 per run.
4. **Diversity, creative_agent:** headline mean pairwise Jaccard similarity not higher by more than 0.05. Guards against the drafters losing variety at 1.5 → 1.0.
5. **Stability, trend_scout:** classifier flag agreement on repeated runs of the same day's 25 terms is ≥ `tuned`'s. Sensitive flags must not get noisier.
6. **Latency:** median wall time not more than +20%.

**If the rule fails:** keep `tuned`. If only check 4 fails, the follow-up is a single-agent arm that keeps the drafters at 1.5; it's out of scope here.

## Task 1: temperature profile seam (parallel with Task 2)

**Files:**
- Create: `agent_common/sampling.py`, `tests/test_temperature_profile.py`.
- Modify: each agent site listed above (`creative_agent/agent.py`, `creative_agent/sub_agents/trend_situation/agent.py`, `interactive_creative/agent.py`, `trend_scout/agent.py`, `trend_scout/digest.py`), `deployment/deploy_agent.py` (`ENV_VAR_DICT`), `deployment/deploy.env`, CLAUDE.md (one bullet).

**Step 1: write the failing tests.**
- `agent_common.sampling.temperature(0.7)` returns 0.7 with the profile unset or `tuned`, and 1.0 with `default`.
- An unknown value falls back to `tuned` with one warning, like the `CAMPAIGN_RESEARCH_PLACEMENT` fallback in `creative_agent/config.py:434-437`.
- A subprocess test imports `creative_agent.agent`, `interactive_creative.agent` and `trend_scout.agent` with `GEMINI_TEMPERATURE_PROFILE=default`. It walks every `LlmAgent` (gc walk, as in the audit) and asserts `generate_content_config.temperature in (None, 1.0)`. With `tuned` it asserts today's exact per-agent values, as a golden dict keyed by agent name. This byte-identity check keeps the default unchanged.

**Step 2: implement.**
```python
# agent_common/sampling.py
"""Gemini sampling profile: today's tuned per-agent temperatures, or the Gemini 3 default (1.0).

Read once at import (Agent Engine pickles agents with the deployer's env; Cloud
Run reads it at revision start). https://ai.google.dev/gemini-api/docs/gemini-3
"""
import logging, os

PROFILES = ("tuned", "default")
GEMINI3_DEFAULT_TEMPERATURE = 1.0

def _profile() -> str:
    raw = os.getenv("GEMINI_TEMPERATURE_PROFILE", "tuned").strip().lower() or "tuned"
    if raw not in PROFILES:
        logging.getLogger(__name__).warning("unknown GEMINI_TEMPERATURE_PROFILE %r; using 'tuned'", raw)
        return "tuned"
    return raw

PROFILE = _profile()

def temperature(tuned: float) -> float:
    """The agent's temperature under the active profile."""
    return tuned if PROFILE == "tuned" else GEMINI3_DEFAULT_TEMPERATURE
```
- **Wrap every agent site:** replace `temperature=0.7` with `temperature=temperature(0.7)` at each of the 19 agent sites. Leave `creative_agent/image_qa.py:719` and `creative_eval/evaluate.py:494,644` untouched; Phase 2 handles them.
- **Ship the knob:** add `GEMINI_TEMPERATURE_PROFILE` (default `tuned`) to `ENV_VAR_DICT` and `deploy.env`. `tests/test_deploy_env_contract.py` must pass.

**Step 3: verify and commit.**
- Run the gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`.
- Mutation check: drop one site's wrapper, and the default-profile walk must fail.
- Commit and open a PR (no attribution). Merge after CI. The default is unchanged in behaviour, so engines don't need a redeploy for this.

## Task 2: A/B driver and metrics (parallel with Task 1)

**Files:**
- Create: `experiments/temperature_ab/{__init__,fixtures,run_ab,metrics,analyze}.py`, `experiments/temperature_ab/arms.example.json`, `tests/test_temperature_ab.py`.
- Modify: `experiments/creative_latency/run_trial.py` (auth helpers only), `experiments/README.md`.

**Reuse, don't rewrite:**
- From `experiments/creative_latency/run_trial.py`: `create_session`, `start_run`, `poll_to_terminal`, `summarize_run`, and `count_429s` with its `_build_log_filter` pattern.
- From `experiments/quota_spread/quality.py`: `extract_quality`.
- From `experiments/quota_spread/run_doe.py`: `plan_cell_order` for the interleaving.

**Steps:**
1. **P3 auth fix (TDD).** The harness predates per-user authz, so the api would answer 401 under enforce.
   - Mint the impersonated `tt-web-sa` ID token with `include_email=True`.
   - Send `X-TT-User: temperature-ab@jordantotten.altostrat.com`, a dedicated user so runs stay out of your run history.
   - `create_session` seeds `state` with the campaign fields, the way the frontend does (`brand`, `target_audience`, `target_product`, `key_selling_points`, `target_search_trends`). It has seeded `initialState` since #263.
   - `start_run` takes an `app` parameter.
   - Unit test with a fake transport asserting the headers and body.
2. **`fixtures.py`:** two campaigns, taken from `tests/eval/evalsets/creative_agent_evalset.json` (PRS Guitars, ANR skincare), each with an evergreen trend. trend_scout uses the same two brands in auto-pick mode, which is the default.
3. **`metrics.py`, pure functions with tests on fixture states:**
   - **creative:** `finalize_done`, `gates_pass_rate` and `mean_score` (extend `extract_quality`), `n_issues` (the sum of the `*__issues` list lengths), `n_retry_exhausted`, image-QA attempts (`generated_images[*].attempts`), and the headline pairwise Jaccard (word sets, mean over pairs).
   - **trend_scout:** completion (3 selected trends), the `trend_flags` term→sensitive map, and `info_gtrends__retry_exhausted`.
   - **Log scrape** (per run window and revision, `count_429s`-style): counts of `empty/abnormal model turn`, `returned an empty turn`, `ValidationError`, and 429s.
4. **`run_ab.py`:**
   - Arguments: `--arms arms.json --apps creative_agent,trend_scout --reps 10 --gap-secs 60`.
   - Serial, interleaved by `plan_cell_order` (arm × app × campaign balanced, ABAB).
   - Writes one JSON record per run to `experiments/temperature_ab/results/<arm>/<app>/<session>.json`: state, metrics, timings, revision.
   - Resumable: it skips cells whose record exists.
   - `--dry-run` prints the order.
5. **`analyze.py`:**
   - Reads the records and writes a per-app arm comparison table: mean, median, the bootstrap CI of `default` − `tuned`, and pass/fail against each pre-registered check.
   - Classifier agreement is the share of terms with an identical flag across same-day runs within an arm.
   - Writes `summary.md` + `records.csv`.
   - Unit-test the bootstrap and the decision rule on synthetic records, including a mutation check.
6. Gates as in Task 1. Commit and open a PR (no attribution). Merge after CI.

## Task 3: deploy the two arm revisions (ops, after Tasks 1–2 merge)

1. Build the api image from main: root `cloudbuild.yaml` with `SHORT_SHA`, then take the digest.
2. Deploy twice:
   - Command: `gcloud run deploy trend-trawler-api --image <digest> --no-traffic --tag tab-tuned`, and again with `--tag tab-default`.
   - Env on each arm, added with `--update-env-vars`:
     - `GEMINI_TEMPERATURE_PROFILE=tuned` or `default`
     - `BQ_DATASET_ID=trend_trawler_eval`
     - `GOOGLE_CLOUD_STORAGE_BUCKET=<eval bucket>`
     - `BRAND_HISTORY_ENABLED=false`
     - `TREND_DIGEST_ENABLED=false`
   - Check the exact BQ and bucket env names against the api's current env with `gcloud run services describe`.
   - **Traffic stays pinned to the serving revision.** Verify `status.traffic` afterwards; the api has a traffic pin.
3. Keep a detached run's instance alive: the driver polls continuously. Also set revision-level `--min-instances=1` on both tags, so an instance isn't scaled away mid-run.
4. Write `experiments/temperature_ab/arms.json` (gitignored) with `base_url`, `audience`, `revision` and `tag` per arm.

## Task 4: pilot, then the full run

1. **Pilot:** `run_ab --reps 1`, about 4 runs and ~35 min.
   - Check that every record has the full state.
   - Check that the log scrape finds the revision.
   - Check that the eval dataset got the rows and `trend_trawler` didn't.
   - Fix anything, then delete the pilot records.
2. **Full run overnight:** `run_ab --reps 10`, which is 20 creative + 20 trend_scout runs, ~5–6 h unattended.
   - Before starting, confirm no user runs are live (api idle check). Pro quota is shared.
   - Rerun a missing cell with the resumable driver.

## Task 5: analyze and write up

1. `analyze.py` writes the comparison against the pre-registered rule.
2. Write it up in `docs/experiments/2026-10-1x-gemini3-temperature-ab.md`, modelled on `docs/experiments/2026-07-17-quota-bucket-spread-doe.md`:
   - setup, confounds and the rule;
   - the results table and verdict;
   - limits: n≈10, describe step not exercised, judge fixed at 0.3.
3. Optional: mirror the records to Vertex Experiments by adapting `experiments/quota_spread/upload_to_vertex.py`.
4. Open a PR with the write-up and `summary.md`.

## Task 6: adopt, deploy, CI gate, teardown

1. **If `default` wins:**
   - Open a PR replacing the wrapped literals with `1.0` (or dropping the explicit temperature), and delete the profile knob, which is no longer needed.
   - Update the pinned test values (`tests/test_pipeline_structure.py:1877` reviser 0.7, plus the Task 1 golden).
   - Then redeploy the engines (Cloud Build, `_SMOKE=true`) and the api. Run `engine_handoff apply --update-worker` and **verify the worker's traffic pin moved**.
   - **If `tuned` wins:** keep the knob at its default, or remove it, and record the result.
2. **CI gate:** dispatch `adk-eval.yml` (`agent=all`) on the adopted code.
   - The efficiency gate must pass, or a deliberate `--update-baseline` PR must land if token or call counts shifted by design.
   - Cases must pass `tool_use_check`.
3. **Teardown:**
   - Remove the `tab-tuned` / `tab-default` tags and their revisions' min-instances.
   - Optionally clear the experiment rows from `trend_trawler_eval`.
   - Update memory: runbook plus a temperature-decision note.

## Phase 2 (later, separate plan): judge + image QA

These are deterministic graders, so the question is stability and agreement, not creativity. They're tested **offline**, with no new runs:
- **Judge:** re-judge the stored creatives from the Task 4 runs 3× at 0.3 vs 1.0.
  - Measure the verdict flip rate (gates and pass) and score SD.
  - Measure agreement with human ratings (`creative_ratings`, kappa via `runserver/calibration.py`) where ratings exist.
  - Any change bumps `JUDGE_VERSION`, with the test pins `tests/test_eval_timely_gate.py:109` and `tests/test_eval_label_text.py:24`.
- **Image QA:** re-inspect the stored renders 3× at 0 vs 1.0, and measure the rule flip rate.
- Adopt only if flips don't increase.

## Verification

- **Tasks 1–2:** unit tests plus mutation checks. Prod behaviour is unchanged with the profile unset, as asserted by the `tuned` golden walk. CI must be green before merge.
- **Task 3:** `status.traffic` is unchanged (prod revision at 100%). The tags answer `/list-apps` with the impersonated token.
- **Task 4:** the pilot has records with the full state, the eval-dataset rows and the log counts, with nothing written to prod tables.
- **Task 6:** the adopted engines' smoke test passes, the eval CI gate passes, the worker traffic is verified, and the tags are removed.
