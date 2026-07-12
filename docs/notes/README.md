# Session notes

Durable notes captured across working sessions — non-obvious insights that
outlive a single conversation and aren't recoverable from the repo, git history,
`CLAUDE.md`, or existing docs. Update the relevant file rather than adding
duplicates; delete notes that go stale. Each note is dated and states the branch
it was written on, since some describe uncommitted working-tree state.

- [local-testing.md](local-testing.md) — running/validating agents locally: the
  ~12-min UI request timeout, the headless ADK Runner, where results land in
  GCS/BQ/state, artifact/session services.
- [creative-agent-image-generation.md](creative-agent-image-generation.md) —
  `creative_agent` image-gen determinism fixes (skipped step + 2× duplicate
  render) on the `creative-eval` branch, and why `interactive_creative` is left
  separate.
- [frontend.md](frontend.md) — React crash from nested session-state values and
  its confusing backend cascade; the same-origin proxy.
- [ambient-agents-vs-cloud-functions.md](ambient-agents-vs-cloud-functions.md) —
  compares our Cloud Run Functions fan-out with ADK's Ambient Agent trigger
  endpoints; why we keep the current executor (10-min ceiling + idempotency), the
  target event-native architecture (scheduled trawler + BQ-triggered creative,
  with the "BQ has no per-row event" caveat), and the proposed parallel experiment.
