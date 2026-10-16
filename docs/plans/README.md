# Plans index

Status of every implementation plan in this folder, checked against `git log` on
2026-10-12 (HEAD `5d131cc`). One line per plan: date, title, status, PRs. A plan's own
`Status:` line, when it has one, has the details. Plans are historical records: they
describe the design at the time, and later PRs may have changed the code (`CLAUDE.md`
describes the current state).

[`archive/`](archive/) holds the July 2026 plans (retry wrapper, async-job run model,
frontend Cloud Run deployment, image prompting overhaul, etc.). All of them shipped and are
kept for history only.

| Date | Plan | Status | PRs |
|---|---|---|---|
| 2026-09-28 | [Repo refresh: status report + implementation plan](2026-09-28-repo-refresh.md) | Complete (phases 0–4) | #121–#157 |
| 2026-09-29 | [P1: AgentPlatform SDK migration](2026-09-29-p1-agentplatform-sdk-migration.md) | Complete | via P1a + P1b |
| 2026-09-29 | [P1a: CRF → AgentPlatform SDK + `source_packages` spike](2026-09-29-p1a-crf-agentplatform-and-spike.md) | Complete | #163, #166 |
| 2026-09-29 | [P1b: root project → AgentPlatform SDK](2026-09-29-p1b-root-agentplatform-migration.md) | Complete | #168–#171, #173–#175 |
| 2026-09-29 | [P2: ADK graph-Workflow migration](2026-09-29-p2-adk-workflow-migration.md) | Complete | #178–#183 |
| 2026-09-29 | [P2 execution plan](2026-09-29-p2-execution.md) | Complete | #178–#183 |
| 2026-09-29 | [P3: per-user authorization for `/runs` + session routes](2026-09-29-p3-per-user-runs-authz.md) | Complete | #184, #185 |
| 2026-09-29 | [P3 execution plan](2026-09-29-p3-execution.md) | Complete | #184, #185 |
| 2026-09-29 | [P4a: infrastructure as code](2026-09-29-p4a-infrastructure-as-code.md) | Proposal (not started) | — |
| 2026-09-29 | [P4b: showcase enhancements (FallbackModel, eval CI, tracing, Model Armor, diagrams)](2026-09-29-p4b-showcase-enhancements.md) | Complete | #187, #188, #192, #194–#200 |
| 2026-09-29 | [P4b execution plan](2026-09-29-p4b-execution.md) | Complete | #194–#199 |
| 2026-10-02 | [Bandit creative experiments (JAX LinTS on CPR)](2026-10-02-bandit-experiments.md) | Complete | #219–#228 |
| 2026-10-02 | [Frontend redesign: "proof room"](2026-10-02-frontend-proof-room.md) | Complete | #203–#207 |
| 2026-10-02 | [README review and improvement plan](2026-10-02-readme-overhaul.md) | Complete | #217, #218 |
| 2026-10-02 | [Research-edit checkpoint + readable eval dimensions](2026-10-02-research-edit-and-eval-labels.md) | Complete (checkpoint 1 later became the structured-brief editor, #280) | #208, #209 |
| 2026-10-04 | [Advanced traffic controls (tunable scenarios + live preview)](2026-10-04-bandit-advanced-traffic-controls.md) | Complete | #235–#238 |
| 2026-10-05 | [Scripted behaviour shifts](2026-10-05-bandit-scripted-shifts.md) | Complete | #243, #244, #248–#252 |
| 2026-10-06 | [Continuous learning mode](2026-10-06-bandit-continuous-learning.md) | Complete | #260–#262 |
| 2026-10-06 | [Image diversity fixes 1–3](2026-10-06-image-diversity.md) | Complete | #257 (follow-up #258) |
| 2026-10-07 | [Creative quality, brief relevance and brief development](2026-10-07-creative-quality.md) | Complete | #263–#280 (11 PRs + fixes), docs #281 |
| 2026-10-07 | [Deterministic finalize step](2026-10-07-finalize-pipeline.md) | Complete | #271, #272 |
| 2026-10-08 | [Rater-gap fixes](2026-10-08-rater-gap-fixes.md) | Complete | #296 (PR 1), #297 (PR 2), #298 (PR 3) |
| 2026-10-08 | [Rating-driven learning](2026-10-08-rating-driven-learning.md) | Complete | #293 (A), #294 (B), #295 (C) |
| 2026-10-09 | [Shareable creative links](2026-10-09-shareable-links.md) | Complete | #300–#302; JPEG share images #309 |
| 2026-10-09 | [Person reference casting + personalised variants](2026-10-09-person-reference.md) | Complete; variant rendering superseded by edit mode (faithful person swaps) | #303, #304 (PR 0), #305–#308 (PRs 1–4), follow-ups #310, #311 |
| 2026-10-11 | [Person swap for cast creatives + share feed layout](2026-10-11-person-swap.md) | Complete | #314 (part A), #313 (part B) |
| 2026-10-11 | [Faithful person swaps + headshot guidance](2026-10-11-faithful-person-swaps.md) | Complete | #316 (PR 0), #317 (PR 1), #315 (PR 2) |
| 2026-10-11 | [Trend state awareness (Ideas 1–4)](2026-10-11-trend-state-awareness.md) | Complete (Ideas 5+ in `docs/research/TREND_STATE_RESEARCH.md` not yet planned) | #318 (PR 1), #319 (PR 2), #320 (PR 3), fix #321 |
| 2026-10-12 | [Sensitive trends + deterministic trend-tie check](2026-10-12-sensitive-trends-and-trend-tie.md) | Complete (CRF filter uses plain `JSON_VALUE`; BigQuery rejects `SAFE.JSON_VALUE`) | #341 (PR 1), #338 (PR 2), #340 (PR 3), #339 (PR 4), docs #342 |
