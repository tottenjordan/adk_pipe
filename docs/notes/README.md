# Session notes

Durable notes captured across working sessions — non-obvious insights that
outlive a single conversation and aren't recoverable from the repo, git history,
`CLAUDE.md`, or existing docs. Update the relevant file rather than adding
duplicates; delete notes that go stale. Each note is dated and states the branch
it was written on, since some describe uncommitted working-tree state.

- [local-testing.md](local-testing.md) — running/validating agents locally: the
  `deployment.async_app` launcher + `/runs` polling (why bare `adk api_server` is
  not enough), eval concurrency, the headless ADK Runner, initialState seeding,
  where results land in GCS/BQ/state.
- [frontend.md](frontend.md) — React crash from nested session-state values and
  its confusing backend cascade; why leaving the run page no longer cancels a run
  (polled async `/runs` jobs); the same-origin proxy.
- [creative-agent-image-generation.md](creative-agent-image-generation.md) —
  the image-step determinism fixes (skipped step, 2× duplicate render) and the
  current `visual_production_pipeline` Workflow (render barrier, resilient
  generator, `render_concept` + image QA); why `interactive_creative` renders
  separately.
- [creative-quality-gates.md](creative-quality-gates.md) — the calibrated rules
  behind the brief/copy/concept gates, post-render image QA, eval gates (current
  gate set + `JUDGE_VERSION`), person casting, brand history, the trend situation
  validator and the timely slot (CLAUDE.md keeps the summary).
- [judge-calibration.md](judge-calibration.md) — the human-rating protocol for
  calibrating the `creative_eval` judge (~50 ratings across ≥5 runs; reading
  agreement, Cohen's kappa and Spearman's rho; `scripts/eval_calibration.py`).
- [person-reference-calibration.md](person-reference-calibration.md) — the
  2026-10-09 go/no-go calibration for casting a consented person (styles,
  `ALLOW_ADULT`, likeness check, photo guidance and minimum resolution) behind
  `PERSON_SAFE_STYLES` / `MAX_CAST_CONCEPTS`.
- [person-swap-edit-mode.md](person-swap-edit-mode.md) — the 2026-10-11 spike
  comparing edit vs regenerate for personalised variants / person swaps (edit
  mode won: fidelity 75% vs 0%), behind `render_concept.render_edit`.
- [agent-engine-vs-cloud-run.md](agent-engine-vs-cloud-run.md) — 2026-07 design
  note on where the agent executes (in-process on Cloud Run vs Agent Engine);
  its first recommendation (async job model) has shipped, the Agent Engine
  migration has not.
- [ambient-agents-vs-cloud-functions.md](ambient-agents-vs-cloud-functions.md) —
  compares our Cloud Run Functions fan-out with ADK's Ambient Agent trigger
  endpoints; why we keep the current executor (10-min ceiling + idempotency), the
  target event-native architecture (scheduled trawler + BQ-triggered creative,
  with the "BQ has no per-row event" caveat), and the proposed parallel experiment.
