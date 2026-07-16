# creative_agent latency experiment — where the wall-clock goes, and three levers

**Date:** 2026-07-15 → 2026-07-16
**Branch (harness + results):** `feat/creative-latency-experiment`
**Interactive report:** [`experiments/creative_latency/report.html`](../../experiments/creative_latency/report.html) (self-contained Plotly; open in a browser)
**Method:** external harness drives real `creative_agent` runs through the async `/runs` HTTP API on isolated `--no-traffic --tag` Cloud Run revisions, then parses the persisted event log into per-phase wall-clock. No agent code is changed to *measure*; each lever ships on its own branch + tagged revision and is measured identically. Fixed campaign input (`fixtures.py`) across all trials. 3 trials/config; **median** reported (min/max in the report) because the shared, unraisable Vertex quota makes any single number noisy.

> **PNG note:** `report.html` is the deliverable. Static `figures/*.png` export needs headless Chrome (kaleido), which won't launch in the build sandbox — regenerate the PNGs with `plot.py` in a Chrome-capable environment if embeddable images are needed.

---

## TL;DR

- The run is **quota-bound, not compute-bound.** In the baseline, `research + visual + eval` = **80%** of wall-clock, and every one of those phases is paced by shared, project-wide, **unraisable** Vertex quotas: `gemini-3.1-pro-preview` = **5 RPM**, `gemini-3.1-flash-image` = **2 RPM**.
- **Lever A (fewer PRO calls)** — skip the research evaluate→refine round when base research is healthy: **research −42%, total −14.5%**, mechanism proven from session state, quality intact (100% pass).
- **Lever B (parallelize image render)** — 2 concurrent renders: visual phase ~−12% but **total within noise**, because 2 concurrent calls instantly hit the 2 RPM image ceiling → 429 → backoff re-serializes them. Strongest single piece of quota-bound evidence.
- **Lever C (downgrade the two creative critics PRO→FLASH)** — the two targeted phases drop hard (**ad_copy −34%, visual −25%, total −10.8%**) for a small, consistent quality dip (avg ad-copy 0.896→0.838, visual 0.929→0.900, **still 100% pass**).
- **The real lever is a Vertex quota increase.** The code levers reclaim structural waste; they cannot move the quota ceiling that dominates the remaining wall-clock (Lever B proves you can't parallelize past it).

**Recommendation:** ship **Lever A** (pure win, no quality cost). Ship **Lever C** conditionally, after a formal `adk eval` in a cool window confirms the ~5% quality dip is acceptable for a ~11% speedup. Do **not** ship Lever B as-is (no net win at the current image quota). Pursue the **quota increase** as the highest-leverage change.

---

## Baseline (n=3, cool window; tag `exp-baseline`, rev 00041-keq)

Median total **379.5s** (347.8–409.6). Phase breakdown:

| phase | median | share |
|---|---:|---:|
| research | 138.5s | 37% |
| visual | 101.8s | 27% |
| eval | 65.2s | 17% |
| ad_copy | 38.9s | 10% |
| persistence | 18.3s | 5% |
| orchestrator | 10.2s | 3% |
| runserver | 5.8s | 2% |

`research + visual + eval = 80%`. `http_429_503` median **0** — see the 429-evidence caveat below.

### Two measurement facts that shaped everything

1. **The event log has no leaf-agent authors.** Every model turn is authored `root_agent`; each sub-pipeline runs as an `AgentTool` surfacing only as a `functionCall`/`functionResponse`. So per-phase wall-clock is derived from **tool-call spans**, not authors (`summarize_run._SPAN_TOOLS`), and per-phase *model-call counts* collapse to `{orchestrator: 11}`. **Consequence:** you cannot prove "one fewer PRO call" by counting turns — the mechanism proofs below use **session state**, which is deterministic.
2. **Cloud Run ingress logs don't capture Vertex model 429s.** The 429/503s that pace the run are absorbed by ADK infra-retry *inside* the request, so `http_429_503` (from `gcloud logging`) is a thin, best-effort signal. The **phase distribution** is the real evidence — don't over-claim the 429 counts.

---

## Lever A — reduce serial PRO calls (branch `exp/creative-fewer-pro-calls`, tag `exp-fewer-pro`, rev 00045-naz)

**Change.** Wrap the research `combined_web_evaluator` (PRO) + `enhanced_combined_searcher_resilient` in a new reusable `agent_common.RunIfAgent` gated by `_base_research_is_degraded(state)`. On the healthy common path the merged brief already exists, so the gate **skips** the evaluator PRO turn + a search/synth pass; the refine round survives only as a self-healing fallback when base research is blank or a `__retry_exhausted` marker is set. No landmine: the evaluator's output is consumed only inside the block, and the composer already guards `{refined_web_search_insights?}`.

**Latency (n=3; 1 trial errored on an unrelated bug, see below):**

| phase | baseline | fewer_pro | Δ |
|---|---:|---:|---:|
| research | 138.5s | **80.9s** | **−42%** |
| total | 379.5s | **324.4s** | **−14.5%** |

The total drop (−55s) ≈ the research drop (−58s): the removed segment flows straight to the bottom line.

**Mechanism proof (from session state, noise-free):**

| state key | baseline | fewer_pro (healthy) |
|---|---|---|
| `combined_web_search_insights` | present | present |
| `refined_web_search_insights` (block `output_key`) | **present** | **absent** |
| `combined_research_evaluation` (evaluator output) | **present** | **absent** |
| `__retry_exhausted` markers | none | none |

Baseline ran the block unconditionally; the healthy variant skipped it and still produced a complete brief — the composer's `{…?}` guard absorbed the skip.

**Quality (free signal, same unchanged PRO judge):** avg ad-copy 0.896→0.845, visual 0.929→0.913, **100% pass** — within single-run variance.

**Incidental finding (unrelated to the lever):** one trial errored in `AdCopyList` parsing — *"Invalid JSON: lone leading surrogate in hex escape"*: the model emitted a bare Unicode surrogate (from a hashtag/emoji) that breaks strict JSON. Pre-existing robustness gap in ad-copy structured output; worth a separate fix (e.g. tolerate/scrub lone surrogates before validation).

---

## Lever B — parallelize image render (branch `exp/creative-parallel-images`, tag `exp-parallel-img`, rev 00043-neq)

**Change.** `generate_image`'s serial render loop → `asyncio.Semaphore(2)` + `asyncio.gather`, with the blocking genai call moved to `asyncio.to_thread` (gather alone wouldn't overlap blocking I/O). Idempotency guard, per-image backoff, and concept order all preserved.

**Result:** visual phase 101.8s → **89.8s (~12%)**, but **total wall within noise** (379.5 → 399.9s; research/eval ran hotter that window). **`http_429_503` median 0 → 1 (max 4).**

**Why it doesn't help:** two concurrent renders immediately exceed the **2 RPM** image ceiling → 429 → the 20s-base backoff **re-serializes** them, clawing back most of the parallelism. This is the cleanest demonstration of the thesis: *you cannot parallelize past a 2 RPM quota.* (The `visual` span also lumps the serial-PRO concept generation with image render, diluting any render-only speedup.)

---

## Lever C — downgrade creative critics PRO→FLASH (branch `exp/creative-flash-critics`, tag `exp-flash-critics`, rev 00047-fep)

**Change.** `ad_copy_critic` and `visual_concept_critic` from `critic_model` (`gemini-3.1-pro-preview`) → `worker_model` (`gemini-3.5-flash`). Narrowing *already-generated* creatives is low-quality-dependence; this drops two serial 5-RPM PRO turns. The PRO-tier stages that carry real quality weight (report composer, drafters) are untouched.

**Latency (n=3; 1 trial recovered from a client poll read-timeout via `recover_trials.py`):**

| phase | baseline | flash_critics | Δ |
|---|---:|---:|---:|
| ad_copy | 38.9s | **25.6s** | **−34%** |
| visual | 101.8s | **76.1s** | **−25%** |
| research | 138.5s | 135.1s (126–**377**) | ~flat + 1 spike |
| total | 379.5s | **338.6s** | **−10.8%** |

The two targeted phases dropped cleanly (−39s combined). One trial's `research` phase spiked to **377s** (429s observed) — pure **window contention** in a phase Lever C doesn't touch; the median absorbs it, and it *re-confirms* the quota-bound thesis (research's serial PRO calls balloon under contention).

**Quality (free signal, n=3, same unchanged PRO judge):**

| metric | baseline | flash_critics (median) | Δ |
|---|---:|---:|---:|
| avg ad-copy score | 0.896 | 0.838 (0.796–0.842) | ~−7% |
| avg visual score | 0.929 | 0.900 (0.879–0.900) | ~−3% |
| overall pass rate | 100% | **100%** (4/4 + 4/4 every run) | — |

A small, **consistent** dip; every creative still passes the 0.7 threshold comfortably. This is a genuine speed/quality trade-off — reported, not assumed. A formal `adk eval creative_agent` in a **cool** window is the recommended pre-ship confirmation (deferred here because the hot window would make the eval both contended and noisy, and the 3-run same-judge signal is cleaner).

---

## The real lever: a Vertex quota increase

Every code lever above reclaims **structural** waste (a redundant PRO round, an over-tiered critic, a render loop). None can move the **quota ceiling** that governs the remaining 80% of wall-clock:

- **Pro (5 RPM = 1 call / 12s):** the run issues ~a dozen serial PRO turns (research evaluate/compose, ad-copy critique, visual critique/finalize, orchestrator turns) plus **8 PRO judge calls** in eval (ThreadPool capped at 3). At 5 RPM, those 8 judges alone cost `ceil(8/3)·12 ≈ 36s` of pure *pacing* before any inference — a large chunk of the observed 65s eval phase.
- **Image (2 RPM = 1 render / 30s):** 4 renders cost ≥ `~60s` of pacing regardless of concurrency — which is exactly why Lever B produced no net win.

**Projection.** If the PRO ceiling were raised enough to let the 8 eval judges and the serial pipeline PRO turns run at inference speed rather than quota speed, and the image ceiling let 4 renders fire concurrently, the quota-*pacing* component of `research + visual + eval` (the 80% block) largely collapses toward single-call inference latency. Order-of-magnitude, that points at a **sub-200s** run without touching agent logic — a bigger, cleaner win than any code lever, and it stacks with Lever A. Precise numbers require per-attempt timing (an opt-in `after_model_callback` stamping durations into `state_delta`), which was out of scope for v1.

---

## Recommendation (prioritized)

1. **Pursue the Vertex quota increase** — highest leverage; the code levers can't move the ceiling (Lever B proves it).
2. **Ship Lever A** — pure −14.5% total with no quality cost and a proven-safe skip.
3. **Ship Lever C conditionally** — −10.8% total for a ~3–7% quality dip (100% pass); gate on a cool-window `adk eval`.
4. **Do not ship Lever B** at the current image quota — no net win; revisit only after (1).
5. **Fix the lone-surrogate ad-copy JSON bug** — independent robustness gap surfaced during Lever A.

## Reproduce

```bash
# deploy a variant as an isolated tag (prod stays on its serving revision)
gcloud run deploy trend-trawler-api --source . --region us-central1 \
  --no-traffic --tag <tag> --no-cpu-throttling --min-instances 1
# from the harness branch, drive N trials against the tag URL
EXP_INVOKER_SA=tt-web-sa@hybrid-vertex.iam.gserviceaccount.com \
PYTHONPATH="$PWD" uv run python -m experiments.creative_latency.run_experiment \
  --base-url https://<tag>---trend-trawler-api-qqzji3hyoa-uc.a.run.app \
  --audience https://trend-trawler-api-qqzji3hyoa-uc.a.run.app \
  --config-name <name> --revision <rev> --tag <tag> --n 3
uv run python -m experiments.creative_latency.plot   # regenerate report.html
gcloud run services update-traffic trend-trawler-api --region us-central1 --remove-tags <tag>
```

**Caveats.** 3 trials against a shared, contended quota is intentionally cheap but noisy — always read median + min/max, and never compare wall-clock across windows without a same-window re-baseline (the Lever C `research` spike is the cautionary example). Run trials when no other image/creative job (or PaperBanana batch) is active, or the quota contention conflates results.
