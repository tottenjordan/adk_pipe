# Agent composition and interactive checkpoints

Moved from [CLAUDE.md](../../CLAUDE.md) on 2026-10-15 (verbatim) so the always-loaded file stays short. Keep this file current when the subsystem changes.

## Pipeline overview

**Phase 1 — `agents/trend_scout/`**: Gathers top 25 Google Search trends, digests them (`trend_digest`: a neutral one-line summary of why each is trending, from one grounded search turn, and the sensitivity flags — death, tragedy, disaster, violence/crime, a real person's serious illness; never auto-picked — both cached per trend list in BigQuery `trend_digest`, so only the day's first run searches), researches cultural context via web search, filters to 3 most campaign-relevant trends, saves to BigQuery.

**Phase 2 — `agents/creative_agent/`**: Takes a single trend + campaign metadata, runs parallel web research (campaign researcher + trend researcher as sub-agents), synthesizes a structured `CreativeBrief`, generates gated ad copy and visual concepts, renders images with a post-render QA check, evaluates all creatives (binary gates + advisory scores), and exports research PDF, HTML gallery, and evaluation report to GCS. The research PDF and the eval + persistence tail run as deterministic graph steps (inside `combined_research_pipeline` and `finalize_pipeline`), and every stage is chained inside one `creative_pipeline` Workflow, so the root makes a single workflow call after `memorize`.

## Interactive checkpoints and agent tree

**Phase 2 (interactive) — `agents/interactive_creative/`**: Same pipeline as `creative_agent`, but pauses at 3 checkpoints for human review via ADK's `LongRunningFunctionTool`: (1) after research report, (2) after ad copies, (3) after visual concepts. Uses `ResumabilityConfig(is_resumable=True)`. It is a thin wrapper that reuses `creative_agent`'s reusable pipelines + visual schema by importing them from `creative_agent`'s **public facade** (`agents/creative_agent/__init__.py`, a curated `__all__` re-export surface) rather than reaching into the volatile `creative_agent.agent`/`.schemas` internals. (The `config` singleton stays on its stable `creative_agent.config` submodule — re-exporting a name `config` from the package would shadow that submodule.)

**Interactive checkpoints 1–2 (brief review + copy revision):** checkpoint 1 shows the structured `creative_brief` as an editable form (full report collapsed underneath; sessions without a brief fall back to the report review). The resume sends `edits:[{field:"creative_brief", value:<full brief>}]` + `brief_edited:true`; `runserver.async_runs.merge_brief_edit` validates it against `CreativeBrief` (invalid → HTTP 400 `invalid_brief` with per-field `errors`, before anything is claimed or written), re-renders `creative_brief_md`, sets `creative_brief_edited` and clears `creative_brief__issues`; the root then re-saves the PDF (research never re-runs). At checkpoint 2 a `revision_requested` with feedback runs `prepare_copy_revision` (flags every copy with the feedback, capped at one user revision per session) → `ad_copy_user_reviser` (a small Workflow: a guard that skips unless the revision was prepared, a `FailSoftNode` around the shared single-turn `ad_copy_reviser`, which `AgentTool` cannot run, then a terminal that applies the timely copy cap and re-records `ad_copy_critique__issues` warning-only (`creative_agent.cap_and_residual_copies`) and sets `ad_copy_user_revised`) → `review_ad_copies` once more; the second review always proceeds.
### Agent Composition

```
trend_scout (root Agent `trend_scout`; App + ResumabilityConfig(is_resumable=True); sub-agents via AgentTool; root history trimmed by drop_other_agent_context)
├── gather_trends_agent (get_daily_gtrends tool)
├── trend_digest (Workflow, agents/trend_scout/digest.py — agents + graph; cache key, normalisation, coverage rules + BigQuery I/O in agents/trend_scout/digest_store.py; input_schema=PipelineRequest; bare node → NodeTool; right after gathering, in both modes)
│   START → digest_lookup (no LLM: key = gtrends_refresh_date + terms_hash = stable_row_id(date, sorted normalised terms, length=16); parameterised
│   SELECT in a worker thread, 10 s bound; any error/missing table/date → "miss" with a warning) ─ "hit" → digest_ready (restores trend_summaries
│   (source "cache") + trend_flags; no model call; a row without a verdict for every term is a miss)
│   ├ "miss" → trend_describe_failsoft (FailSoftNode(RetryUntilKeyNode(grounded trend_describe_searcher (worker model, google_search, all 25 terms in
│   │   one turn) → trend_describe_synthesizer (lite planner, TrendSummaries → trend_summaries_draft), 2 attempts))) → classify_trends_agent (gather
│   │   model, temperature 0: one verdict per raw term → trend_flags {flags:[{term, sensitive, reason}]}, reading the summaries via
│   │   {trend_summaries_draft?}, fenced as data: they can only ADD a flag, never clear one) → digest_store (trend_summaries {refresh_date,
│   │   source fresh|unavailable, items:[{term (raw, verbatim), summary (≤ 300 chars)}]}; insert-only MERGE of one row on stable_row_id(date,
│   │   terms_hash) only when the summaries cover ≥ 80% of the terms AND the flags cover EVERY term (a missing verdict reads as "not
│   │   sensitive", so a partial classify is never shared; the unclassified count is logged); a failed describe is never cached and its retry
│   │   marker is cleared; a failed write only logs) → digest_ready
│   └ "disabled" (TREND_DIGEST_ENABLED=false) → classify_trends_agent → digest_store (no-op) → digest_ready: today's pre-digest behaviour, no
│       describe, no cache I/O, no trend_summaries key
│   The auto pick (PICK_TRENDS_INSTR) and save_search_trends_to_session_state refuse flagged terms unless interactive_trend_pick (exactly True);
│   in auto mode the save tool also refuses a term not among the gathered raw_gtrends (normalised match, saved as gathered) and skips duplicates.
│   write_trends_to_bq stamps refresh_date from state gtrends_refresh_date (MAX(refresh_date) only as a fallback); gather queries are time-bounded.
├── understand_trends_agent_resilient (RetryUntilKeyNode over a searcher → synthesizer Workflow → info_gtrends; the synthesizer has output_schema=AnalyzedTrends ({analyzed_trends:[{term, category, context, cultural_angle, as_of, event_hint}]}, state holds the validated dict) at its tuned temperature 1.5, behind the surrogate scrub + `build_invalid_reply_dropper(AnalyzedTrends)`: a reply that still fails the schema is blanked, so it is a missed attempt like an empty reply (never a raised ValidationError) and all-invalid attempts end in `info_gtrends__retry_exhausted` with the run continuing; bare node → NodeTool)
├── pick_trends_agent (strategic filtering → selected_gtrends)
├── review_trends (LongRunningFunctionTool — opt-in interactive trend pick)
└── Persistence tools (BigQuery, GCS, record_research_gaps, memorize)

creative_agent (root Agent `root_agent`; non-resumable App (carries plugins); tools = creative_pipeline (bare node → NodeTool) + memorize; the root memorizes missing campaign fields, calls creative_pipeline exactly once, then writes the final text; only after an "Error running node" result does a continue message make it re-call creative_pipeline, once per message (`<RETRY_AFTER_ERROR>`, also on interactive_creative's pipeline steps))
├── creative_pipeline (Workflow, input_schema=PipelineRequest; the root's ONE workflow call — four separate root calls let the Pro root end Agent Engine runs with empty turns before finalize)
│   START → combined_research_pipeline → ad_creative_barrier → ad_creative_pipeline → visual_production_barrier
│   → visual_production_pipeline → finalize_barrier → finalize_pipeline (terminal: finalize_ready's summary); each
│   *_barrier is a no-output node keeping the previous stage's confirmation out of the next stage's PipelineRequest input.
│   The stages (nested Workflows; also reused separately by interactive_creative):
│   ├── combined_research_pipeline (Workflow, input_schema=PipelineRequest)
│   │   START → (gs_/ca_sequential_planner: each a Workflow planner → RetryUntilKeyNode-wrapped
│   │   searcher+synthesizer Workflow; load_brand_history: no-LLM BigQuery read → brand_history; trend_situation_pipeline: run/skip gate → FailSoftNode(RetryUntilKeyNode(grounded
│   │   searcher → TrendSituationDraft synthesizer)) → validate_trend_situation (no LLM) → trend_situation / trend_situation_md) → research_join (JoinNode) → research_barrier (no output)
│   │   → merge_planners → refinement_gate ("refine" only when base research is degraded:
│   │   evaluator → RetryUntilKeyNode-wrapped refined search; else "skip")
│   │   → combined_report_composer → brief_writer_failsoft (FailSoftNode → RetryUntilKeyNode →
│   │   brief_writer, CreativeBrief → creative_brief) → brief_gate (deterministic brief_check.py, incl.
│   │   the product named exactly in mandatories + no unsupported absolute claims (claims.py) in the proposition, reasons to believe or mandatories unless the user's selling points state them;
│   │   "revise" → brief_reviser_failsoft → back to brief_gate, at most BRIEF_REVISION_ROUNDS
│   │   passes; residuals → creative_brief__issues; every exit writes creative_brief_md, the
│   │   compact Markdown the creative prompts read; "ok") → save_research_pdf_node (research PDF, raw HTML escaped,
│   │   artifact + GCS → research_report_gcs_uri; skipped without a report; failure →
│   │   research_report_gcs_uri__issues) → research_report_ready (truthy terminal)
│   ├── ad_creative_pipeline (Workflow: drafter (10 copies spread across the brief's angles, self-rated
│   │   typicality) → critic (final 4 cover ≥3 angles; per-copy brief_checks checklist) → copy_gate
│   │   (deterministic copy_gate.py: product named, CTA ≤8 words, headline/caption length, brief avoid
│   │   terms, unsupported absolute claims (claims.py; allowed only when the user's selling points state
│   │   them, never from the brief's mandatories), a trend tie (trend_tie.py: the trend phrase, one of its distinctive
│   │   words or a brief motif; avoid terms never count; skipped for a sensitive moment or trend_scout's `trend_sensitive_hint`, unless `safer_angle_active`: then re-checked with the brief-risk-named event words excluded, any endorsement term (endorsement.py) flagged, at least ceil(n/2) copies required to name the subject or use a distinctive `trend_bridge.safer_angle` word (`safer_angle_tie_words`: generic words like "soul"/"studio", product/brand and risk/avoid words never count; below that every untied copy is flagged) and `safer_angle_named_copies` recorded), plus the critic's failed proposition/mandatories/risks (trend
│   │   risks)/trend_bridge (clear, specific trend tie) checks (other self-reports advisory);
│   │   "revise" → ad_copy_reviser_failsoft (rewrites ONLY flagged copies;
│   │   unflagged edits reverted by restore_unflagged) → back to copy_gate, at most COPY_REVISION_ROUNDS
│   │   passes; deterministic residuals only → ad_copy_critique__issues; "ok") → ad_copies_ready)
│   ├── visual_production_pipeline (Workflow)
│   │   visual_generation_pipeline (Workflow: art_director (resets the concept-fix state) → concept
│   │   drafter/critic/finalizer (each concept: brand_cue from the brief's distinctive assets / brand
│   │   colours, in-image text quoted from the paired copy's headline/CTA, brief avoid + fit_mode;
│   │   finalizer carries angle_id) → concept_gate (deterministic concept_guard.concept_issues:
│   │   cue-preceded quoted text ≠ paired headline/CTA/brand/product (meme/comic style exempt),
│   │   empty trend_motif, >2 headline/CTA text concepts, >1 centred hero; "revise" → visual_concept_fixer_failsoft (rewrites ONLY flagged concepts;
│   │   unflagged edits reverted by restore_unflagged_concepts, then the motif/product/brand_cue
│   │   guard) → back to concept_gate, at most CONCEPT_REVISION_ROUNDS passes; residuals →
│   │   final_visual_concepts__issues; "ok") → visual_concepts_ready) → render_barrier → visual_generator_resilient
│   │   (FunctionNode, no LLM turn, INFRA_RETRY → generate_image on the node Context: render → image QA vision check
│   │   (agents/creative_agent/image_qa.py) → at most IMAGE_QA_MAX_RERENDERS targeted re-renders (≤ IMAGE_QA_MAX_RERENDERS_PER_RUN per run), keep the
│   │   better attempt (critical failures weigh first), upload only it → generated_images / image_qa__issues)
│   │   → images_ready (truthy terminal)
│   └── finalize_pipeline (Workflow, agents/creative_agent/finalize.py: evaluate_creatives_node (creative_eval
│       judge off the event loop → creative_evaluation_report; none → __retry_exhausted, stale report
│       cleared) → persist_node (fixed order: eval report JSON → HTML gallery (lightbox links the original
│       renders, no upscale copy) → trend_creatives row → creative_evals row (upsert); transient 5xx/429/timeouts retried ×3, then fail-soft →
│       <key>__issues) → finalize_ready (sets finalize_done; truthy summary: scores, URIs, failed steps))
└── memorize

interactive_creative (root Agent `root_agent`; App + ResumabilityConfig(is_resumable=True); reviser via AgentTool)
├── combined_research_pipeline / ad_creative_pipeline / visual_generation_pipeline / finalize_pipeline (reused from creative_agent, incl. their gates; bare nodes → NodeTool)
├── review_research / review_ad_copies / review_visual_concepts (LongRunningFunctionTool checkpoints 1–3)
├── checkpoint 1 = editable structured brief (merge_brief_edit); checkpoint 2 feedback → prepare_copy_revision → ad_copy_user_reviser (bare Workflow → NodeTool, one user revision) → review_ad_copies once more (details above)
├── visual_concept_reviser (applies checkpoint-3 revision notes → final_visual_concepts under the shared VISUAL_CONCEPT_RULES + brief / paired copy / user avoid + brand colours; first `restore_revision_unflagged_concepts` (like the fixer's restore_unflagged_concepts) puts back every concept the notes don't name, renamed, dropped or invented from the resume's `visual_revision_snapshot` (the concepts after the direct edits; noted keys in `visual_revision_flagged_ids`, both written by `async_runs.visual_concept_edits_delta` next to `visual_revision_notes`) and keeps noted concepts' names, then guarded by ensure_trend_and_product_callback, then recheck_concept_issues_callback re-records final_visual_concepts__issues — no fix loop — and the three revision keys are cleared; a direct-edit resume clears the stale marker, and without notes the model is skipped but `skip_reviser_without_notes` runs the same guard + recheck on the direct edits (and clears the revision keys))
├── visual_generator_resilient (reused deterministic render node → NodeTool; render after the reviser) → finalize_pipeline
└── save_draft_report_artifact (only to re-save the PDF after a checkpoint-1 brief/report edit) + memorize
```

Key ADK patterns used: `Agent`, graph `Workflow`s (`google.adk.workflow`: fan-out + `JoinNode`, routed function nodes, truthy terminal nodes; exposed to roots as bare nodes → `NodeTool`), `RetryUntilKeyNode` (graph retry wrapper in `libs/agent_common/`), `AgentTool` (wraps agents as tools), `LongRunningFunctionTool` (pause/resume for human-in-the-loop), and `App` + `ResumabilityConfig` (resumable sessions). The ADK Workflow migration (proposal P2, complete 2026-09-29; `docs/plans/2026-09-29-p2-adk-workflow-migration.md`) retired the deprecated `SequentialAgent`/`ParallelAgent`/`LoopAgent` containers and the `RunIfAgent`/`RetryUntilKeyAgent` wrappers; `tests/test_public_api.py` guards against their return (no references in the agent packages, and importing every agent emits no legacy-container `DeprecationWarning`).
