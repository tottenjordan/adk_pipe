# Gemini 3 temperature A/B (2026-10-15)

**Verdict: FAIL. Keep the tuned per-agent temperatures** (`GEMINI_TEMPERATURE_PROFILE=tuned`, the default). Two pre-registered checks failed.

Plan: [`docs/plans/2026-10-15-gemini3-temperature-ab.md`](../plans/2026-10-15-gemini3-temperature-ab.md). Driver and analysis: `experiments/temperature_ab/`.

## Question

The Gemini 3 docs strongly recommend leaving `temperature` at its default of 1.0, and warn that lower values "may lead to unexpected behavior, such as looping or degraded performance" (https://ai.google.dev/gemini-api/docs/gemini-3). Our agents use hand-tuned values between 0.0 and 1.5. Does switching every agent that sets a temperature to 1.0 help or hurt?

## Setup

- **Arms:**
  - **Tag `tab-tuned`:** api revision `trend-trawler-api-00283-dim` with `GEMINI_TEMPERATURE_PROFILE=tuned`, i.e. today's values.
  - **Tag `tab-default`:** revision `-00284-qej` with `GEMINI_TEMPERATURE_PROFILE=default`, i.e. 1.0 for all 21 agent sites.
  - Both are the same image (main `4686fef`) and differ only in this env var.
  - The judge (0.3) and image QA (0) were fixed in both arms. They are out of scope here; see Phase 2.
- **Isolation:**
  - Both arms wrote to `trend_trawler_eval` and the eval bucket.
  - `BRAND_HISTORY_ENABLED=false`, so runs didn't steer each other.
  - `TREND_DIGEST_ENABLED=false`, so the 0.0 sensitivity classifier ran on every trend_scout run instead of hitting the cache.
  - User: `temperature-ab@…`.
  - A check confirmed that no row reached the production tables.
- **Runs:**
  - Two fixed campaigns, PRS Guitars and ANR skincare, the same as the eval CI.
  - Counterbalanced ABBA order, one run at a time, 60 s apart.
  - The original plan was 10 runs per arm per app. To save time this was cut to 4 `creative_agent` + 6 `trend_scout` runs per arm, 20 runs in all.
  - All 20 completed. A separate 8-run pilot was not counted.
- **Signals:**
  - Completion: `finalize_done`, or 3 selected trends.
  - From the log scrape: empty or abnormal turns, schema re-draws (`ValidationError`) and 429s.
  - The judge's `gates_pass_rate`, mean score and pass rate.
  - Residual `__issues`.
  - Headline Jaccard similarity.
  - Classifier flag agreement across same-day runs.
  - Wall time.

## Pre-registered rule and result

| check | result | detail |
|---|---|---|
| completion | PASS | failed/unfinished: default 0/10, tuned 0/10 |
| robustness | PASS | 0 empty/abnormal turns, 0 schema re-draws and 0 retry exhaustion in **both** arms |
| creative quality | **FAIL** | `gates_pass_rate` −0.19 (limit −0.05; 95% CI [−0.44, +0.06]); mean score −0.01 (OK); `n_issues` +0.25 (OK) |
| creative diversity | PASS | headline Jaccard +0.001 |
| trend_scout stability | **FAIL** | classifier flag agreement: default 0.92 vs tuned 1.00 |
| latency | PASS | creative median 495 s vs 471 s; trend_scout 134 s vs 117 s (both within +20%) |

**Creative quality (n=4 per arm):**

| | `gates_pass_rate` | pass rate | judge score |
|---|---|---|---|
| tuned | 0.875 | 0.81 | 0.817 |
| default | 0.688 | 0.69 | 0.806 |

## Reading

- **Gemini's warning didn't show up here.** Neither arm had a single empty turn, abnormal finish or schema re-draw. The looping and degradation that the warning describes did not appear under our hand-tuned temperatures. The 0.01 trend_scout root and 0.0 classifier behaved cleanly.
- **The sensitivity classifier is the clearest effect.** At temperature 0 it gave identical flags across repeated runs of the same day's 25 terms. At 1.0, 8% of term flags changed from run to run. Those flags decide which trends are never auto-picked and which the batch path skips, so they need to be repeatable. This alone justifies keeping 0.0 there.
- **Creative quality is suggestive, not conclusive.** With 4 runs per arm, the drop in check pass rate (−0.19) fails the threshold, but its CI includes zero. The direction (drafters going from 1.5 to 1.0, critics and revisers from 0.7 to 1.0) gives no reason to switch.
- **The rest is a wash:** latency, completion, headline variety and residual issues are close between the arms.

## Limits

- **Sample size:** a small n, especially for the creative runs. This is a screen for clear harm, not a precise effect estimate.
- **Fixed inputs:** two fixed campaigns, on one day's trend list.
- **Digest describe step:** not exercised, because the cache was off.
- **429s and the stray run:** one run from a stopped driver overlapped the start of the next run for about 2 minutes. The 429s were logged (tuned 0.5/run vs default 0 for creative) and are noise, not a temperature effect.

## Follow-ups

- **The profile knob:** `GEMINI_TEMPERATURE_PROFILE` stays, defaulting to `tuned`. Its pinned golden in `tests/test_temperature_profile.py` documents the tuned values.
- **Phase 2:** the judge and image-QA temperature test runs offline on human-rated creatives (`docs/plans/2026-10-15-grader-temperature-phase2.md`).
- **Possible later A/B:** a single-agent arm (for example the drafters alone at 1.0) could be tested on its own if variety becomes a concern.
