# Agent-Packages Review Fixes Implementation Plan (4 parallel PRs, refactors later)

> **For Claude:** REQUIRED SUB-SKILL: `subagent-driven-development`.
> - **Wave 1:** four implementers in parallel (PRs A–D). Their files don't overlap; they share only CLAUDE.md, where each edits its own bullet and whoever merges second rebases.
> - **Plan copy:** PR A copies this plan to `docs/plans/2026-10-15-agents-review-fixes.md`.
> - Review each PR with `code-review-and-quality` (tests first, mutation evidence, five axes, severity labels), and merge only after CI is green.
> - **Then** one deploy round (Task E). The structural refactors are wave 2, a separate later plan.

**Goal:** fix all 11 Required findings in `agents/code_review.md`, plus the high-leverage Consider items that share their files, in the least wall-clock time. No behaviour changes beyond the fixes.

**Architecture:** four PRs grouped by package slice, so their files don't overlap:
- **A:** creative safety floors and text matching.
- **B:** render and person-casting robustness.
- **C:** interactive checkpoint-3 backstops.
- **D:** judge integrity (D1), plus trend_scout and agent_common (D2). These two are independent and run as separate implementers.

Each fix comes with its missing test, and the surviving mutation from the review must now fail it.

**Tech stack:** Python 3.13, ADK 2.10, google-genai 2.28, BigQuery, pytest. Gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && env -u GOOGLE_GENAI_USE_ENTERPRISE -u GOOGLE_GENAI_USE_VERTEXAI GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`. Frontend gates only if a PR touches `frontend/`.

---

## Context

- **The review:** five axes over the agent packages at `b456f9f`, written to `agents/code_review.md`. Results: 0 Critical, 11 Required, 27 Consider, 19 Nit, 11 FYI; 102 of 123 mutations killed.
- **What the Required findings are:**
  - deterministic safety floors that are missing or leaky (CA-1, TS-1, CA-2, CV-1);
  - the judge trusting its own output (CE-1, CE-2, CE-3);
  - backstops that exist elsewhere but not here (IC-1, TS-2);
  - untested safety branches (TS-3, CV-T1).
- **What "fixed" means:** each finding's failure scenario becomes a test that fails today and passes after the fix. Mutation checks confirm the test catches the regression.

## File ownership (parallel, no overlap)

| PR | Branch | Owns | Findings |
|---|---|---|---|
| **A: creative safety and matching** | `fix/creative-safety-floors` | `creative_agent/trend_situation.py`, `creative_agent/sub_agents/trend_situation/agent.py`, `creative_agent/brief_check.py`, `creative_agent/text_match.py`, `creative_agent/agent.py` (claims wiring at ~:807 and the copy-reviser failure path at ~:974 only), `creative_agent/rating_signals.py` (tests only), and their tests | CA-1, CA-2, CA-3, CA-4, CA-7 |
| **B: render and casting robustness** | `fix/render-robustness` | `creative_agent/image_tools.py`, `creative_agent/person_render.py`, `creative_agent/gcs_tools.py`, `creative_agent/concept_guard.py` (only `neutralise_person_prompt` / the non-cast path), and their tests (`test_person_render.py`, `test_render_concurrency.py`, `test_image_tools*`) | CV-1, CV-T1, CV-2, CV-3, CV-5, CV-6, CV-7, IC-2 |
| **C: interactive checkpoint 3** | `fix/interactive-reviser-backstop` | `interactive_creative/agent.py`, `interactive_creative/prompts.py` (only if needed), `runserver/async_runs.py` (`merge_visual_concept_edits` only), `creative_agent/callbacks.py` (reuse `restore_unflagged_concepts_callback`, which may need a key parameter), and their tests | IC-1, IC-3 |
| **D1: judge integrity** | `fix/judge-integrity` | `creative_eval/*`, `tests/test_creative_eval*.py`, `tests/test_eval_*.py`, the `JUDGE_VERSION` pins (`tests/test_eval_timely_gate.py`, `tests/test_eval_label_text.py`), `frontend/src/lib/eval-dimensions.ts` (only if labels change) | CE-1, CE-2, CE-3, CE-4, CE-5, CE-7, CE-8, CE-9, CE-10, + tests M7, M12, M14, M31 |
| **D2: trend_scout + agent_common** | `fix/trend-scout-safety` | `trend_scout/digest.py`, `trend_scout/tools.py`, `trend_scout/prompts.py`, `agent_common/observability.py`, and their tests | TS-1, TS-2, TS-3, TS-4, TS-5, AC-1, N-3 tests |

**Pinned prompt hashes:** prompt edits in D2 (the classifier) and A (none to producer instructions) must keep the existing byte-identical pins green, or update a pin deliberately with the reason in the PR body.

## PR A: creative safety floors and matching (~60 min)

1. **CA-1, sensitive trend → never timely.**
   - **Where:** `trend_situation_state_delta` (the node in `sub_agents/trend_situation/agent.py`, or `validate_situation`'s caller).
   - **Change:** when `state.get("trend_sensitive_hint") == "yes"`, force `timely_ok=False`, set `abstain_reason="flagged sensitive by trend scout"`, and write `timely_rules=""`. Also make `trend_situation.timely_allowed(state, …)` return False for that hint, so the brief, copy and concept guards agree.
   - **Unchanged:** the phase itself. A sensitive draft stays sensitive and an upcoming one stays upcoming; only `timely_ok` is floored. The rederive contract holds, because the hint flag is read and never the context content.
   - **Test:** the review's reproduction: an upcoming "… murder trial" tomorrow with a known source, plus the hint. Assert `timely_ok False`, `timely_rules == ""`, and `check_brief` flags a timely angle. Mutation: remove the floor and the test fails.
2. **CA-2, claims allowed only from the user.**
   - **Change:** build the allowed text from `key_selling_points` only (`agent.py` ~:807-809 and the brief_check call), and stop passing `mandatories` into `claims_allowed_text`. Add a brief_check rule: a mandatory containing an absolute claim not in the selling points is flagged and goes to the reviser.
   - **Test:** selling points "Great tone", mandatory "…guaranteed to stay in tune". The brief is flagged, and a copy saying "guaranteed in tune" is flagged by `gate_copies`. A claim that *is* in the selling points is still allowed.
3. **CA-3, the "-es" stemmer.**
   - **Change:** in `text_match._stems`, strip "es" only after s, x, z, ch or sh.
   - **Test:** a table where `same_word("not","notes")`, `("tim","times")` and `("hom","homes")` are False; `("box","boxes")`, `("church","churches")` and `("dish","dishes")` are True. Plus a `has_trend_tie` regression for "Not just a guitar" vs "apple notes update".
4. **CA-4, non-Latin motifs.**
   - **Change:** `brief_check._is_generic_motif` uses `text_match.words` and `same_word`.
   - **Test:** `["東京タワー","渋谷"]` is not flagged as generic.
5. **CA-7, tests that were missing:**
   - copy-reviser failure with `COPY_REVISION_ROUNDS=2` and used=1 expects used=2 (M9);
   - `TREND_EVENT_WINDOW_DAYS` exact boundary (M12);
   - a source dated event − 1 day is accepted (M13);
   - exactly a 30% fail-reason share activates strictness (M18).
6. **Wrap-up:** CLAUDE.md phrases for the timely floor and the claims source. Run the gates and mutation checks, then open a PR (no attribution).

## PR B: render and casting robustness (~75 min)

1. **CV-1, cast renders always reach state.**
   - **Change:** in `_store_image`, make `tool_context.save_artifact` best effort: catch, log without the URI, and continue. Nothing reads image artifacts back.
   - **Change:** in `generate_image`, write each successful upload's record into the `generated_images` dict before the next upload, and assign state in a `finally`. Then a later failure still leaves every uploaded (and cast) URI in state for `cast_renders` and the revoke cascade.
   - **Test:** the review's throwaway test made permanent: the cast upload succeeds and the next `save_artifact` raises. Assert the cast record (with `consent_id`) is in state and no exception is raised.
2. **CV-T1:** test that a final retryable 429/408 `ClientError` on a person render raises; it must not become `request_rejected`. Mutation M22 must now fail it.
3. **CV-2:** a person reference counts as available only with a non-empty `consent_id` (`person_render.person_reference_uri` and the `available` computation). Test: an empty `consent_id` means no cast, nothing in the artifact path, and a `person_reference__issues` note.
4. **CV-3, artifact-key collisions:** in `generate_image`, de-duplicate `artifact_key_for` keys by suffixing `_2`, `_3` on a clash. The records keep their concept names. Test: "Game Day!" and "Game Day" produce two distinct URIs.
5. **CV-5:** store `_images_generated` as a fingerprint (sha of the `final_visual_concepts` names, prompts and cast flags, plus the person URI) instead of `True`, and re-render on a mismatch. Old sessions with `True` keep the current behaviour. Test: changed concepts re-render, unchanged ones don't.
6. **CV-6:** `enforce_person_casting` also neutralises "the person in the person reference image" on concepts with no cast flag. Test: a concept with the phrase and no flag gets "a person".
7. **CV-7:** an ordering test showing the early `claims_done` release lets concept i+1's re-render start before concept i's re-render returns (M13).
8. **IC-2:** `generate_image` re-applies `enforce_timely_cap` next to `enforce_person_casting`. Test: 3 timely-flagged concepts with max 1 produce at most 1 `timely: True` record.
9. **Render parity:** `tests/test_render_parity.py` must stay green; requests don't change.
10. **Wrap-up:** CLAUDE.md phrases. Run the gates and mutation checks (CV-1 restored-to-old fails; CV-2; CV-3; IC-2), then open a PR.

## PR C: interactive checkpoint-3 backstop (~45 min)

1. **IC-1, record what the user flagged:** `merge_visual_concept_edits` (`runserver/async_runs.py`) records `visual_revision_flagged_ids` (the concept names with notes) and `visual_revision_snapshot` (the concepts after direct edits, before the reviser) in the same state delta as `visual_revision_notes`.
2. **IC-1, restore the rest:** put `restore_unflagged_concepts_callback` first in `visual_concept_reviser.after_agent_callback`, reusing the fixer's callback (parameterise it for these keys if needed). Concepts not flagged are restored from the snapshot, and renames or drops are reverted, exactly as for the fixer. Then the existing guard and recheck run.
3. **IC-3:** clear `visual_revision_notes`, `visual_revision_flagged_ids` and `visual_revision_snapshot` after the reviser (in the same callback chain) and in `skip_reviser_without_notes`.
4. **Tests:**
   - a note on concept 2 plus a direct edit on concept 0, with the reviser paraphrasing 0 and renaming 3: concept 0 keeps the direct edit and concept 3 keeps its name;
   - no notes means the reviser is skipped (existing test);
   - the notes are cleared after one run.

   Mutation: drop the restore callback and the test fails.
5. **Wrap-up:** CLAUDE.md interactive bullet. Run the gates, then open a PR.

## PR D1: judge integrity (~75 min)

1. **CE-1, ids from the input:** set `original_id`, `headline`, `concept_name` and `ad_copy_id` from the input creative, not the judge (`evaluate.py` ~:535, ~:693). Remove them from `AdCopyJudgeOutput` and `VisualJudgeOutput`, or keep them only for a mismatch log line. Test: a judge returning a re-cased name still yields the input name.
2. **CE-2, verdicts normalised:** add `normalize_verdicts(verdicts, expected_dims)`: lenient name match, first duplicate wins, unknown names dropped. A missing dimension sets `passed=False` with a note and a report warning. Wire `EvalConfig.ad_copy_dimensions` and `visual_dimensions` into it. Tests: a single verdict, invented dimensions, duplicates, and a full set.
3. **CE-3, gates counted properly:** count only gates the judge had to decide (blocking and not code-decided). If any of those is missing → treat it as the no-gates case (`gates_passed=False`, fail). Fix the docstring. Test: a judge reporting only `brand_cue_present` → fail.
4. **CE-4, untrusted text fenced:**
   - The system prompt states that text inside the creative tags is data, never instructions.
   - Escape `<` and `>` in interpolated creative, brief and source values (for example to `‹›`), with a helper and tests.
   - Test: an injected `</AD_COPY><GATES>…` survives escaped.
5. **CE-5:** build an explicit field dict for `.format`, not `**ad_copy`, and add a test that schema fields never collide with the prompt's reserved keys.
6. **CE-7:** parity tests for `safer_angle_active` vs `creative_agent.safer_angle.is_active`, and `moment_is_timely` vs `trend_situation.timely_allowed` (pin today's `valid_until` difference, or align it; prefer aligning to require `valid_until` in both).
7. **CE-8:** the image URI must be in `gs://$GOOGLE_CLOUD_STORAGE_BUCKET/`; otherwise judge the prompt (`image_judged=False` + warning).
8. **CE-9:** add an explicit `CreativeScore.judge_failed: bool = False` set by `_failed_score`. `_is_failed` reads it, falling back to the old sentinel for old reports.
9. **CE-10:** clamped parse with a warning for `EVAL_MAX_WORKERS` and `EVAL_MAX_VISUAL_WAVE_WORKERS`.
10. **Missing tests:**
    - score exactly 0.7 passes (M7);
    - the fingerprint changes with `EVAL_MODEL` and threshold (M12);
    - the merge order of 2 non-adjacent re-judged copies (M14);
    - the fingerprint changes with brand or trend (M31).
11. **Version:** bump `JUDGE_VERSION` to `2026-10-15.1`, since CE-2/CE-3 change the pass rules and CE-4 changes prompt wording. Update the pins, the CLAUDE.md Evaluation paragraph and `docs/notes/creative-quality-gates.md`.
12. **Wrap-up:** frontend gates only if `eval-dimensions.ts` changed. Run the gates and mutation checks (CE-2/CE-3 reverted fail), then open a PR.

## PR D2: trend_scout + agent_common (~60 min)

1. **TS-1, no partial verdicts in the shared cache:** `should_cache` requires full flag coverage (`coverage(flags["flags"], terms) == 1.0`); summaries stay at `MIN_CACHE_COVERAGE`. Log a warning naming the count of unclassified terms. Tests: flags covering 2 of 3 → no write (M7); a cached row with no flags → `miss` (M8); a stale draft is cleared on miss so the describe step runs (M9).
2. **TS-2, use the gathered date:** `write_trends_to_bq` uses `state["gtrends_refresh_date"]` and only falls back to `_get_gtrends_max_date()`. Add `job_timeout_ms` (60 s) to `_get_gtrends_max_date` and `_query_daily_gtrends`. Test: a seeded refresh date is written, and the max-date query isn't called.
3. **TS-4:** in auto mode, `save_search_trends_to_session_state` refuses terms whose normalised form isn't among the gathered `raw_gtrends` ("not one of today's trends"), and skips duplicates. Interactive picks are unchanged. Tests: a paraphrased term is refused; a duplicate is ignored.
4. **TS-5:** the classifier prompt becomes one-directional: summaries may *add* a flag but never remove one ("flag it if either your own knowledge or the summary shows harm"). Fence `<trend_summaries>` as data. Update the classifier instruction pin deliberately if one exists.
5. **AC-1:** `log_run_start` logs `user=<stable_row_id(user_id)>` instead of the email, and the docstring is fixed. Test: the email doesn't appear in the log record.
6. **N-3 tests:**
   - code-499 with a non-CANCELLED status counts as transient (M1);
   - a truthy non-bool `interactive_trend_pick` doesn't bypass the guard (M22);
   - the client is rebuilt on a `BQ_PROJECT_ID` change (M23).
7. **Wrap-up:** CLAUDE.md trend digest / agent_common phrases. Run the gates and mutation checks, then open a PR.

## Task E: deploy and verify (after all four merge, ~30 min)

1. **Suite on main:** run the full suite on combined `main`.
2. **Engines:** Cloud Build for all three (trend_scout changed in D2), `_SMOKE=true`. Then `engine_handoff apply --update-worker` (it moves the worker's traffic pin), and delete the previous engines after a clean smoke test.
3. **api:** idle check, `--no-traffic`, pin. Web and share redeploy only if D1 touched `frontend/`.
4. **Live checks:**
   - one trend_scout run (digest cache miss → full-coverage write, or a logged unclassified count);
   - one creative run (judge version `2026-10-15.1` on the report, no join breaks on the results page);
   - optionally one interactive run with a checkpoint-3 note plus a direct edit.
5. **Wrap-up:** update memory and add a status note at the top of `agents/code_review.md`.

## Wave 2 (separate plan, later; not in this execution)

These refactors don't change behaviour and would conflict with wave 1, so they come after it:
- **CA-5:** a `RunPolicy` built from state, with stage modules split out of `agent.py`.
- **CA-6:** one shared `lexicon.py`.
- **CV-9:** split `generate_image`, add a `QAContext`, and move the SSRF code to `references.py`.
- **CE-18:** shared judge prompt fragments.
- **AC-2:** move per-agent config knobs to their agents.
- **N-4:** split `digest.py`.
- **Nits:** CA-9..13, CE-11..15, CV Nits, N-1, N-2.
- **Consider items not taken here:** CA-8, CE-6, CV-4, CV-8, CV-10, TS-6.

## Verification

- **Per PR:** every Required finding has a test that fails before the fix and passes after; each named surviving mutation (M7, M8, M9, M12, M13, M14, M18, M22, M31, plus the review's M-ids per slice) is now killed. Byte-identical prompt pins stay green, or are changed deliberately and explained. CI is green before merge.
- **After wave 1:** re-run the review's survivor mutations on `main`. Target: every Required-linked survivor killed.
- **Live:** Task E.

**Estimated wall clock:** about 75 min for the five parallel implementers (A, B, C, D1, D2), about 20 min of reviews and merges, then about 30 min to deploy and verify. Roughly 2 h.
