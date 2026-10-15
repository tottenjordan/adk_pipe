# Agents Review Wave 2 Implementation Plan (7 parallel PRs)

> **For Claude:** REQUIRED SUB-SKILL: `subagent-driven-development`.
> - **Seven implementers in parallel** (R1–R7). Every file has exactly one owning PR (table below); CLAUDE.md and `tests/README.md` are shared, each PR editing only its own bullets, and whoever merges later rebases.
> - **Plan copy:** R1 copies this plan to `docs/plans/2026-10-15-agents-review-wave2.md`.
> - Review each PR with `code-review-and-quality` (tests first, mutation evidence, five axes, severity labels) and merge only after CI is green. Then do one deploy round (Task 8).

**Goal:** finish everything left in `agents/code_review.md` after wave 1, with the shortest possible wall clock:
- the structural refactors (CA-5, CA-6, CV-9, CE-18, AC-2, N-4);
- the deferred Consider items (CA-8, CE-6, CV-4, CV-8, CV-10, TS-6);
- the Nits;
- the four wave-1 follow-ups (finalize `judge_failed`, the `COPY_CLAIMS_RULE` wording, stale `_images_generated` comments, the revoke-cascade folder scan).

**Architecture:** split by file ownership so no two PRs touch the same source file.
- **Refactor PRs** (R1, R2, R4-part, R6-part) must be behaviour-preserving, proven by:
  - the unchanged suite (5,133 tests);
  - the byte-identical instruction sha pins;
  - the render-parity golden;
  - re-killing the wave-1 mutations in the moved code.
- **Behaviour changes** (CV-4, CE-6, TS-6, CA-8, CV-10, the claims wording, the folder scan) go in **separate commits** with their own failing-first tests, so they're reviewable apart from the moves.

**Tech stack:** Python 3.13, ADK 2.10, google-genai 2.28, pytest. Gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && env -u GOOGLE_GENAI_USE_ENTERPRISE -u GOOGLE_GENAI_USE_VERTEXAI GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8` (if `env -u` is blocked, set those vars to empty strings).

---

## Context

**What's left after wave 1:** wave 1 (#445–#449, main `2857473`, judge `2026-10-15.1`) fixed all 11 Required findings. Left over:
- **Structural debt:**
  - `creative_agent/agent.py` (1523 lines) mixes graph wiring with gate policy, and threads run policy as 10–17 loose kwargs.
  - Three near-identical lexicon engines.
  - `generate_image` is about 238 lines, and `_inspect_and_rerender` takes 16 parameters.
  - `evaluate.py` is 1092 lines, with duplicated judge prompt text.
  - Per-agent knobs sit in `BaseAgentConfiguration`.
  - `digest.py` mixes four concerns.
- **Consider items deferred because they would have collided with the fixes.**

**Why it matters:** every recent feature (timely, rating strictness, safer angle) added another `if feature_active` argument through 3–4 layers. These refactors stop that growth before the next feature. Each refactor removes a concept rather than relocating it, which the code-review skill requires.

**Constraints:**
- No behaviour change in refactor commits.
- The `creative_agent/__init__.py` facade keeps its `__all__` (interactive_creative depends on it).
- Agent Engine pickles the agents, so no import-time side effects.
- Never add `jax` to runtime deps.
- No attribution in commits or PRs.

## File ownership (parallel, no overlap)

| PR | Branch | Owns (exclusive) | Items |
|---|---|---|---|
| **R1: creative_agent stages + RunPolicy** | `refactor/creative-stages-run-policy` | `creative_agent/agent.py` (→ split), new `creative_agent/{run_policy,research_stage,copy_stage,visual_stage}.py`, `creative_agent/copy_gate.py`, `creative_agent/brief_check.py`, `creative_agent/text_match.py`, `creative_agent/safer_angle.py`, `creative_agent/__init__.py` (facade unchanged), their tests | CA-5 (RunPolicy, stage split, safer-angle tie logic moved from copy_gate into safer_angle), CA-6 part 2 (stopword/pronoun sets and `_MIN_TOKEN_CHARS` unified in `text_match`), CA-11 (`_apply_timely_cap` helper), the stale `_images_generated` comment in agent.py |
| **R2: lexicon engine** | `refactor/lexicon` | new `creative_agent/lexicon.py`, `creative_agent/claims.py`, `creative_agent/temporal_terms.py`, `creative_agent/endorsement.py`, their tests | CA-6 part 1 (one `Lexicon(terms, idioms, families, boundary)` type behind the three modules' public functions, which keep identical signatures and results), CA-9 (dead idioms removed, or real cases added) |
| **R3: prompts + research hygiene** | `fix/prompts-research-hygiene` | `creative_agent/prompts.py`, `creative_agent/callbacks.py`, `creative_agent/sub_agents/**`, `creative_agent/brand_history.py`, prompt tests / sha pins | `COPY_CLAIMS_RULE` wording (deliberate pin update), CA-10 (prompts interpolate the `copy_gate` constants: read-only import of `MAX_HEADLINE_CHARS`, CTA words etc.), CA-13 (a global brace guard over every `*_INSTR`), CA-8 (situation synthesizer gets only its own searcher's sources), CA-12 (drop the unused `target_trend` column) |
| **R4: render pipeline** | `refactor/render-pipeline` | `creative_agent/image_tools.py`, `creative_agent/render_concept.py`, `creative_agent/image_qa.py`, `creative_agent/references.py`, `creative_agent/gcs_tools.py`, `creative_agent/person_render.py`, `creative_agent/tools.py`, render tests + the parity golden (read-only) | CV-9 (`generate_image` → `_plan_renders` / `_run_pipelined` / `_persist_results`; a frozen `QAContext` replacing `_inspect_and_rerender`'s 16 params; `render_edit` and `render_concept` sharing one rerender helper; SSRF and reference-fetch code moved to `references.py`), CV-4 (an exhausted per-concept render error recorded like no-image, not aborting the batch), CV-8 (parallel uploads, records kept in concept order), CV Nits (upload `content_type` from the model's mime; drop the hard-coded `?authuser=3`; the M15 max-2 test; a comment or removal for the M14 no-op) |
| **R5: judge + finalize** | `refactor/judge-prompts-finalize` | `creative_eval/*`, `creative_agent/finalize.py`, eval/finalize tests | CE-18 (shared judge prompt fragments; `evaluate_visual_concept` and `evaluate_all_creatives` split into helpers), CE-11..15 (dead `_score_from_verdicts` strengths, the triple `unreported_gates_warning`, schema names in prompts, one report builder instead of two, `advisory` on NOT_REPORTED), CE-6 (`trend_motif_visible` passes in code with "no trend" when the run has no trend), the follow-up (`finalize.py` reads `score.judge_failed`), CV-10 (a successful finalize step clears its stale `<key>__issues`) |
| **R6: trend_scout + agent_common** | `refactor/trend-scout-common` | `trend_scout/*`, `agent_common/*`, `creative_agent/config.py`, `interactive_creative/config.py` (if any), their tests | N-4 (`digest.py` → `digest_store.py`, pure + BigQuery, plus `digest.py`, agents + graph), N-2 (public `trend_context` helpers: `norm_term`, `strip_fence`, `is_true`), N-1 (no `logging.basicConfig` in library modules), AC-2 (per-agent knobs moved to `creative_agent.config` / `trend_scout.config`; one `parse_on_flag`; docstring fixed), TS-6 (`understand_trends_synthesizer` gets `output_schema` + the surrogate scrub), the stale `_images_generated` comment in `retry_node.py`, F-1 (document `rpm_quota` as a safety cap) |
| **R7: revoke cascade scan** | `fix/revoke-folder-scan` | `runserver/person_refs.py`, `runserver/person_refs_store.py` (only if needed), their tests | The renamed-cast-render gap: `delete_session_renders` also scans `{gcs_folder}/{agent_output_dir}/` of each recorded session for blobs whose metadata `consent_id` / `source_consent_id` equals the revoked consent, and deletes them (bounded listing, metadata-checked, idempotent) |

**Cross-PR reads, not writes:**
- R3 imports R1-owned constants (`copy_gate.MAX_HEADLINE_CHARS` etc.) but doesn't edit `copy_gate.py`.
- R1's stage modules import prompts, which R3 owns, and don't edit them.
- R5 and R6 don't touch `creative_agent/agent.py`.

## R1: creative_agent stages + RunPolicy (critical path, ~2 h)

Commits, in order. Each one keeps the suite green.

1. **Plan copy:** copy this plan to `docs/plans/2026-10-15-agents-review-wave2.md`.
2. **`run_policy.py`:** a frozen `RunPolicy` dataclass built once per gate call by `RunPolicy.from_state(state, config)`:
   - `timely_ok`, `max_timely`, `strictness` (`rating_signals.strictness_flags`), `trend_sensitive`, `safer_angle`, `safer_angle_text`, `claims_allowed_text`;
   - the brief's `avoid` / `mandatories` / `motifs` / `risks`;
   - `brand`, `target_product`, `trend`.

   Unit tests cover every field's derivation from state, matching today's ad-hoc reads (one golden state per feature combination).
3. **Call sites use the policy:** `_copy_issues`, `_deterministic_issues`, `_concept_issues`, `brief_gate_decision`, `cap_and_residual_copies` and `copy_gate_decision` build a `RunPolicy` and pass `policy=` to `gate_copies` / `check_brief` / concept checks. Those functions take `policy: RunPolicy` instead of the loose kwargs.
   - Keep the old kwargs as a thin deprecated adapter **only if** external callers exist. Grep for them; tests are internal and should be updated, so prefer deleting the kwargs.
   - Remove the `agent.safer_angle_active` duplicate (use `safer_angle.is_active`) and the hard-coded `"yes"` in `_trend_sensitive`.
4. **`_apply_timely_cap(state)`:** this helper replaces the duplicated blocks (CA-11).
5. **Safer-angle tie logic moves:** from `copy_gate.py` (about 150 lines: `safer_angle_tie_words`, `GENERIC_SAFER_ANGLE_WORDS`, the named-subject check) to `safer_angle.py`; `copy_gate` calls it.
6. **Shared word sets:** stopwords, pronouns and fillers live in `text_match` (`STOPWORDS`, `PRONOUNS`, `FILLER`, `MIN_TOKEN_CHARS`), and `copy_gate`, `brief_check` and `safer_angle` use them (CA-6 part 2). Prove the sets are identical where they were meant to be. Where two private sets differed on purpose, keep a named difference and document it.
7. **Stage split:** move the agent definitions and gate/routing nodes from `agent.py` into `research_stage.py` (research + brief), `copy_stage.py` (ad copy pipeline + copy gate nodes) and `visual_stage.py` (visual generation + concept gate + render node wiring).
   - `agent.py` keeps only the composition root: `creative_pipeline`, `root_agent`, `app`, and re-exports for the names tests and `interactive_creative` import.
   - The facade `creative_agent/__init__.py` `__all__` stays identical; `tests/test_public_api.py` must pass.
   - Target: `agent.py` under 300 lines and each stage under 600.
8. **Stale comment:** fix the agent.py comment that calls `_images_generated` a boolean.

**Proof of no behaviour change:**
- the full suite, the instruction sha pins and `tests/test_pipeline_structure.py` pass unchanged (only import paths are updated);
- `tests/test_root_history.py` and the graph tests are green;
- re-apply wave-1 mutations M2 (ceil half), M6 (hint), M9 (reviser budget) and the CA-1 floor in the moved code, and confirm each still fails a test.

## R2: lexicon engine (~60 min)

1. **`lexicon.py`:** `Lexicon(terms, idioms=(), families=(), boundary=…)` with `find(text) -> list[str]`. It is whole-word, fold-insensitive, with spaces and hyphens interchangeable, and blanks idioms first. This reproduces the three current `_pattern()` variants exactly; write a table test over each module's current behaviour **before** refactoring.
2. **The three modules:** `claims.unsupported_claims`, `temporal_terms.temporal_terms` and `endorsement.endorsement_terms` become thin wrappers over a module-level `Lexicon`. Signatures and outputs are identical, and all existing tests pass unchanged.
3. **CA-9:** delete the dead idioms ("skin in the game", "24/7", "win-win" where already excluded by lookarounds), or add the real case each one guards, as a test.
4. **Mutation check:** blank the idioms in the engine, and the idiom tests fail.

## R3: prompts + research hygiene (~60 min)

1. **Claims wording:** `COPY_CLAIMS_RULE` says claims are allowed only when the user's selling points state them (wave 1 made the gates enforce this). Update the sha pins deliberately, with the reason in the PR body.
2. **CA-10:** the critic and reviser prompts interpolate the `copy_gate` limits (headline chars, CTA words, caption limit) through f-strings at import; there are no braces at runtime. The reviser lists the CTA as a valid place to name the product. Update the pins.
3. **CA-13:** a global test that every `*_INSTR` / prompt constant in `creative_agent`, `interactive_creative` and `trend_scout` has only `{identifier}` / `{identifier?}` tokens, no other braces, and that every token resolves to a known state key. Mutation: add `{"a": 1}` to a prompt, and the test fails.
4. **CA-8:** `collect_research_sources_callback` records each searcher's own source ids (`<agent>_source_ids`), and the trend-situation synthesizer prompt reads only `{trend_situation_sources?}`, a formatted subset, instead of the whole `{sources?}`. Test that sources from other branches are absent. Update that instruction's pin.
5. **CA-12:** drop the unused `target_trend` from `brand_history.build_history_query`, and test the SELECT column list.

## R4: render pipeline (~90 min)

1. **Split `generate_image`:**
   - `_plan_renders(state) -> RenderPlan`: references, casting, timely cap, fingerprint.
   - `_run_pipelined(plan, ...) -> list[RenderResult]`: the semaphore, pacing, the budget chain.
   - `_persist_results(plan, results, tool_context) -> state delta`: uploads, records, the `finally` write.

   `generate_image` becomes about 40 lines.
2. **`QAContext`:** a frozen dataclass holding brand, product, strictness, person_image, original_image, safer_angle, has_logo, model and client, replacing `_inspect_and_rerender`'s 16 params. `render_concept` and `render_edit` share one `_rerender_loop(qa_ctx, render_fn, budget)`.
3. **Move the SSRF code:** the SSRF and reference-fetch code (`_is_public_ip`, redirect handling, size caps; `image_tools.py` ~:77-216) moves to `references.py`. Existing SSRF tests must pass with their imports updated.
4. **CV-4 (behaviour, own commit):** a concept whose render raises after all retries is recorded as `image_qa__issues` ("render failed: …") and skipped. The others still render and upload. Test: concept 2 raises and concepts 1, 3 and 4 are uploaded; previously the whole batch raised.
5. **CV-8 (own commit):** uploads gathered concurrently (bounded at 4), with records assembled in concept order. Test the order and the bounded concurrency.
6. **Nits:**
   - upload `content_type` from the model's mime (and test it);
   - remove `?authuser=3` from gallery URLs;
   - the M15 test (max re-renders 2 with a blocked re-render: `break`);
   - M14: keep `rendered.set()` with a comment explaining it's for future no-fallback callers, or remove it if nothing can reach it.
7. **Proof:** `tests/test_render_parity.py` unchanged and green; `test_render_concurrency.py` green; re-apply the wave-1 mutations (M13 claims_done, M22 429, CV-1 finally) in the moved code, and they still fail.

## R5: judge + finalize (~75 min)

1. **CE-18:** shared prompt fragments in `creative_eval/prompts.py`: the data-fence rule, and the common gate texts (`trend_risks_respected`, `timely_claim_supported`, `safer_angle_respected`, the moment block) composed into both judges.
   - Rendered prompts must be **byte-identical** to today's. Add a golden test of both rendered prompts for a fixture creative **before** refactoring, then refactor.
   - Split `evaluate_visual_concept` (about 186 lines) into `_build_visual_request`, `_call_judge` and `_score_visual`, and split `evaluate_all_creatives` similarly.
2. **CE-11..15:**
   - delete the dead strengths/improvements in `_score_from_verdicts`, and fix the `agent.py:281` comment;
   - a loop in `unreported_gates_warning`;
   - correct the schema names in the prompts;
   - one report builder, with `evaluate_creatives` delegating to the agent path's builder or vice versa; keep the public signatures, and test the learning fields and warnings on both entry points;
   - drop `advisory` on NOT_REPORTED gates (a test pins the UI label).
3. **CE-6 (behaviour, own commit):** `trend_motif_visible` joins `TREND_GATES`, so with no trend it passes in code with "no trend". Test it, and check whether this changes judge behaviour enough to need `JUDGE_VERSION` `2026-10-15.2`. Pass rules for no-trend runs change, so bump it, with the pins and CLAUDE.md.
4. **The follow-up:** `creative_agent/finalize.py` reads `score.judge_failed` (with the string fallback for old reports). Test: a judge writing "evaluation_failed" in improvements isn't treated as failed by finalize.
5. **CV-10:** after a successful `_run_step`, clear `f"{key}__issues"`. Test: a second, successful finalize clears the first run's issue.

## R6: trend_scout + agent_common (~75 min)

1. **N-4:** `trend_scout/digest_store.py` takes the pure helpers and BigQuery I/O (`coverage`, `flags_complete`, `should_cache`, lookup/store queries, `terms_hash`); `digest.py` keeps the agents and graph. Re-export what tests import, or update the tests.
2. **N-2:** public `trend_context` helpers (`norm_term`, `strip_fence`, `is_true`), keeping the private names as aliases only if external code uses them (grep); update `digest.py` and `tools.py`.
3. **N-1:** remove `logging.basicConfig` from library modules (`agent_common/observability.py`, `trend_scout/tools.py`, `trend_scout/callbacks.py`, `trend_scout/agent.py`); the entrypoints (`deployment/async_app.py`, deploy scripts) already configure logging, so check that and add it there if missing. Test: importing the agents doesn't add root handlers.
4. **AC-2:** move creative-only fields (image QA, render concurrency, aspect ratios, the ratings table, …) into `creative_agent.config.ResearchConfiguration`, and trend_scout-only fields (digest knobs) into `trend_scout.config`.
   - Attribute access stays `config.X`, so call sites don't change.
   - One `parse_on_flag` helper.
   - Fix the module docstring.
   - `tests/test_deploy_env_contract.py` and the shipped-defaults test must pass, and `ENV_VAR_DICT` is unchanged.
5. **TS-6 (behaviour, own commit):** `understand_trends_synthesizer` gets an `output_schema` (`InfoGtrends` items: term, context, cultural_angle, hook, as_of, event_hint) plus `scrub_surrogates_in_response`.
   - Its existing JSON consumers (`trend_context._info_item`, the picker prompt) must keep working; test with a fixture.
   - The temperature stays at the tuned value: the A/B decided to keep tuned temperatures.
6. **Comments:** the `retry_node.py` comment no longer calls `_images_generated` a boolean, and the `rate_limit.py` docstring documents `rpm_quota` as a safety cap (F-1).

## R7: revoke cascade folder scan (~45 min)

1. **The gap:** `person_refs.delete_session_renders` reads each recorded `session:<app>/<id>`. A cast render whose concept was renamed and re-rendered can drop out of `generated_images`, while the GCS object (with consent metadata) remains.
2. **The fix:** for each recorded session, also list `gs://<bucket>/{gcs_folder}/{agent_output_dir}/` (taken from the session state's `gcs_folder` and `agent_output_dir`, validated as single safe segments like `shares.output_prefix`; skip if absent).
   - The listing is bounded (max 500 blobs, with prefix + metadata fetch).
   - Delete every blob whose metadata `consent_id` or `source_consent_id` equals the revoked consent, skipping `shares/` and `person-refs/`.
   - Idempotent; failures go to `revoke_incomplete` as today.
3. **Tests:**
   - a renamed-concept orphan blob with matching metadata is deleted;
   - a blob with another consent's metadata, or none, is kept;
   - an unsafe folder segment means no listing;
   - the listing cap is respected;
   - a missing session is skipped.

   Mutation: drop the metadata check, and the "kept" test fails.

## Task 8: integrate, deploy, verify (~35 min, after all merge)

1. **Merge order:** R2 → R1 → R3 → R4 → R5 → R6 → R7 (any order works; this one minimises rebases on CLAUDE.md). After each merge, run the full suite on `main`.
2. **Mutation re-check:** re-run the wave-1 and review survivor mutations listed in each PR on combined `main`; all must still be killed.
3. **Engines:** Cloud Build for all three (`_SMOKE=true`), then `engine_handoff apply --update-worker`, then delete the old engines.
4. **api:** idle check, `--no-traffic`, pin. Web only if a PR touched `frontend/` (none planned; the CE-6 version bump doesn't change labels).
5. **Live checks:**
   - one creative run: the report's judge version, eval pairing, gallery links working without `authuser`;
   - one trend_scout run: digest hit/miss, `info_gtrends` parsed under the new schema.
6. **Wrap-up:** update memory and `agents/code_review.md` with a wave-2 status note.

## Verification

- **Refactor commits:** the suite is unchanged; sha pins, render parity and the new golden for judge prompts are byte-identical; specific wave-1 mutations, re-applied in moved code, still fail.
- **Behaviour commits** (CV-4, CV-8, CE-6, TS-6, CA-8, CV-10, the claims wording, the folder scan): each has a test that fails before the change, plus a mutation check.
- **Sizes after:** `agent.py` under 300 lines, `generate_image` about 40, `evaluate.py` under 900, `digest.py` under 350.
- **Live:** Task 8.

**Estimated wall clock:** about 2 h for the critical path (R1; the others take 45–90 min in parallel), about 30 min of reviews and sequential merges, about 35 min to deploy and verify. Roughly 3 h.
