# Tests

The Python test suite for Trend Trawler. Covers Pydantic schema validation, agent
pipeline structure, tool functions, callbacks, deployment utilities, Cloud Run Function
logic, ADK end-to-end evals, and the offline unit tests for the `experiments/`
measurement harnesses (see [../experiments/README.md](../experiments/README.md)).

```bash
# Python tests (pytest) — no GCP credentials needed, but GOOGLE_CLOUD_PROJECT must be set
# (any dummy value; the repo .env normally provides it) — genai.Client(vertexai=True)
# construction resolves the project eagerly
uv run pytest tests/ -v
uv run pytest tests/ -q -n 4   # parallel (pytest-xdist); CI uses -n 4. Avoid -n auto: per-worker agent imports make it slower
uv run pytest tests/ -q -n 6 -m "not slow"  # fast local loop: skips the multi-second tests (CI runs everything)
uv run pytest -m "not subprocess"  # skip the child-process tests (fresh-import guards, shell entrypoint)

# ADK evals — end-to-end LLM-as-judge (real API calls, ~5 min per case)
PYTHONPATH="$PWD" uv run adk eval trend_scout tests/eval/evalsets/trend_scout_evalset.json \
  --config_file_path=tests/eval/eval_config.json --print_detailed_results

# creative_agent eval — needs PYTHONPATH + its own rubric config
PYTHONPATH="$PWD" uv run adk eval creative_agent tests/eval/evalsets/creative_agent_evalset.json \
  --config_file_path=tests/eval/creative_eval_config.json --print_detailed_results
```

**Worker count:** locally, `-n 16` was fastest on a 32-core box (42 s, vs 50 s with
`-n auto` and 53 s with `-n 4`); CI keeps `-n 4` on its 4-vCPU runner (and prints the 15
slowest tests). To emulate CI's 4 cores locally:

```bash
GOOGLE_CLOUD_PROJECT=test-project /usr/bin/time -f "%e s wall %U s user" taskset -c 0-3 uv run pytest tests/ -q -p no:cacheprovider -n 4 --durations=15
```

Pytest config lives in `pyproject.toml` `[tool.pytest.ini_options]`: `testpaths`,
`pythonpath = ["."]` (so tests import the flat packages without `sys.path` hacks), the
`subprocess` and `slow` markers (`slow` = notebook-parity generation smoke, scenario-preview
goldens, `test_build_image.py`), and `filterwarnings` scoped to known third-party noise only.

See [CLAUDE.md](../CLAUDE.md) for the full testing notes (eval invocation gotchas,
per-agent rubric configs, integration tests).

## Structure

```bash
tests/
├── __init__.py
├── _bandit_sizes.py                 # standard bandit episode sizes (HORIZON_S / HORIZON_M / BATCH) so tests reuse compiled XLA programs
├── _creative_fixtures.py            # a real finished creative run (the screenshot-harness fixtures) as session state + eval report, shared by the shares tests
├── _fake_bq.py                      # FakeBigQueryClient: configurable, dependency-free fake bigquery.Client (records queries + streaming inserts; canned rows, insert/job errors)
├── _fakes.py                        # shared test doubles: fake producers + stub/recording LLMs (retry-node + graph-Workflow tests), FakeToolContext/FakeState, FakeStorageClient, noop_async
├── conftest.py                      # shared fixtures: gcp_project_env (dummy GOOGLE_CLOUD_PROJECT), fresh_config (fresh package import, restored after); persistent JAX compile cache at .pytest_cache/jax (override with JAX_COMPILATION_CACHE_DIR; delete the dir to reset)
├── eval/                            # ADK evals — rubric-based LLM-as-judge (real APIs)
│   ├── __init__.py                  # makes tests.eval importable (for the gate's unit tests)
│   ├── efficiency_gate.py           # CI regression gate over adk eval's informational metrics (not a pytest file)
│   ├── eval_config.json             # trend_scout rubric config
│   ├── creative_eval_config.json    # creative_agent rubric config
│   └── evalsets/
│       ├── trend_scout_evalset.json
│       └── creative_agent_evalset.json
├── test_agent_common_clients.py     # shared lazy GCS/BigQuery client getters (per-thread BigQuery reuse, nothing built at import)
├── test_agent_common_idempotency.py # stable_row_id deterministic BigQuery row keys
├── test_agent_common_models.py      # shared model location + build_gemini() factory
├── test_agent_common_state.py       # shared memorize tool + seed_initial_state()
├── test_ad_copy_prompts.py          # ad copy prompts: angle spread + typicality (drafter), angle coverage/surprise/brief checklist/CTA (critic), flagged-only reviser tokens, the trend tie rule (drafter) + trend_bridge check wording matching the judge's trend_connected (critic); brace-safety
├── test_agents_dir.py               # agents/ serving-view symlinks used by the Cloud Run api_server
├── test_async_app_wiring.py         # imports deployment.async_app for real (subprocess, in-memory stores): UserAuthzMiddleware installed, enforce boot guard refuses a missing proxy config
├── test_async_runs.py               # async-job run model: kick-off/poll/resume, terminal markers, checkpoint edits (report, structured brief: valid merge / 400 on invalid); only the terminal marker decides done/error, so a contained in-pipeline error event keeps the run running and is listed as a deduped, capped, truncated non-fatal `warnings` entry (#322)
├── test_authz.py                    # P3 per-user authz (incl. /shares/{u}, /person-refs/{u} and /variants/{u} paths): modes, userId normalization, proxy ID-token check, middleware 401/403/404, ownership → 404
├── test_backend_entrypoint.py       # backend container entrypoint (uvicorn serves async_app.py)
├── test_backfill_eval_labels.py    # deployment/backfill_eval_dimension_labels.py: pure SQL builder (no BigQuery)
├── test_bandit_endpoint_lib.py      # deployment/bandit/endpoint.py vs a fake aiplatform (single-worker env, 1 replica, labels, find_* by label oldest-first)
├── test_bandit_cloudbuild.py        # the one bandit image (endpoint + traffic job) build: cloudbuild.bandit.yaml + Dockerfile, dependency layer cached apart from the code
├── test_create_bq_tables.py         # create_bq_tables.sh with a stub bq: bandit_* + creative_ratings + creative_shares + person_references + trend_digest (partitioned, 30-day expiry) schemas (ratings from deployment/bq_schemas/creative_ratings.json, REPEATED fail_reasons, vs RATING_COLUMN_TYPES), partitioning/clustering, idempotency
├── test_bandit_*.py                 # JAX bandit core (bandit/): features, config, linear TS, baselines, environment, simulate+metrics+aggregate, notebook-parity figures (fixture data) + generation smoke, scripted shifts
│                                    #   (also test_bandit_continuous.py: §11 continuous segments; test_bandit_endpoint_client.py: traffic endpoint clients/retries; test_bandit_traffic_bq.py: traffic row builders vs contracts §3)
├── test_brand_history.py            # creative_agent/brand_history.py: parameterised creative_evals SELECT (no string-built brand), report reads limited to the configured bucket + size cap, aggregation (recent/strongest styles, recurring weak dimensions, runs with failed non-advisory gates), brace-free ≤120-word note, BQ/GCS/timeout fail-open, disabled knob → no query, shortlist re-draw unless a style preference is set, canonical style mapping (schema example phrasings), allowlisted tones/dimensions/gates only (injection strings never reach the note), GCS per-call timeouts + report-read deadline, failed-check denominator = readable reports
├── test_rating_signals.py          # creative_agent/rating_signals.py: fail-reason enum == runserver copy, check-backed strictness subset, allowlisted aggregation (canonical styles, tone Literal, enum; injection strings dropped), style/reason minimums + 30% dominance, brace-free ≤80-word note, SELECT reads only allowlisted columns (never note), normalised @brand/@days params, 8 s job timeout, fail-open (None = unavailable)
├── test_brief_check.py              # deterministic creative-brief check (proposition incl. abbreviations/capital-led sentence breaks/brand-product-trend names/and-compounds, X-but-Y insight + ;/dash/contrast markers, cited RTBs + normalised src-N/brief ids vs sources, fit_mode, angle names/tensions, motifs, assets, product named exactly in mandatories, unsupported absolute claims vs key selling points/mandatories)
├── test_brief_render.py             # creative brief → "## Creative Brief" markdown in the research PDF (real markdown_pdf TOC check) + compact (headless) prompt variant + gallery summary card (HTML-escaped)
├── test_build_context.py            # the web image's build context excludes per-checkout generated files (tsconfig.tsbuildinfo, next-env.d.ts, Playwright outputs) so the registry cache hits
├── test_callbacks.py                # citation replacement, state init (incl. style_shortlist seeding, reference_images/reference_roles), rate limiting, trend/product guard callback
├── test_citations.py                # shared citation renderer (creative_agent.citations)
├── test_cleanup_policies.py         # deployment/cleanup/ retention rules: scoped to trend-trawler's own packages/prefixes, untagged registry versions only, never live engine / bandit / run / share prefixes
├── test_cleanup_apply_sh.py         # deployment/cleanup/apply.sh preview with a stub gcloud: a failed lookup of what Cloud Run serves aborts (exit 5)
├── test_clock.py                    # agent_common/clock.py: per-run date state (timezone, fallback, naive = UTC), TREND_TIMEZONE config + deploy wiring
├── test_concept_guard.py            # final image prompts always name the trend_motif + product + brand_cue (pure guard; token-overlap matching incl. brand_cue, intangible-product cue); concept_issues (quoted text vs paired headline/CTA incl. curly quotes / punctuation / substring, meme+comic exemption, empty motif, set-level text cap + centred-hero selection, conservative heuristics; rating strictness: text cap 1, prominence line, verbatim motif) + restore_unflagged_concepts
├── test_config.py                   # per-agent config resolution (incl. campaign-placement resolver, brand-history + rating-learning knobs)
├── test_claims.py                  # creative_agent/claims.py: absolute-claim lexicon (whole-word, risk free/risk-free, 100% ≠ 100, lifetime idioms exempt, "never miss" not a claim), allowed text + term families, claims_allowed_text joins
├── test_lexicon.py                 # creative_agent/lexicon.py: the shared Lexicon engine (whole-word, fold, space/hyphen, boundary class, idioms blanked first, overrides, rewrites, families)
├── test_lexicon_table.py           # characterisation table pinning claims / temporal_terms / endorsement behaviour across the shared engine (incl. the CA-9 dropped idioms)
├── test_text_match.py               # shared conservative matching (Unicode folding, plurals, sizes/packaging head nouns, single-token full phrase, brand anchor, 60% token-overlap mentions)
├── test_copy_gate.py                # deterministic ad-copy gate (product named, CTA words, headline/caption length, brief avoid terms minus product/mandatory/trend overlaps; Unicode/&-brand/plural matching audit cases; warning-only structural issues; unsupported absolute claims (deterministic, allowed via allowed_claims_text); only proposition/mandatories/risks/trend_bridge self-reports gate (+ reason_to_believe under off_brief strictness; weak_cta → 6-word CTA), only deterministic residuals recorded; lenient product matching; duplicate-id keys; tolerant parsing) + restore_unflagged safety net
├── test_create_session_engine.py    # create_session_engine.py (reuse-or-create sessions-only engine)
├── test_creative_fanout_image.py    # CRF container image: one Dockerfile for both services, FUNCTION_TARGET / FUNCTION_SIGNATURE_TYPE (http for the Cloud Tasks worker); named so the CRF venv's test_crf_* glob skips it
├── test_creative_brief_prompts.py   # {creative_brief_md?} block before the report + shared contract rule (core + fallback; user feedback/art direction override) in the 5 creative prompts; brace-safety
├── test_creative_agent_graph.py     # creative_agent graph pipelines end-to-end (stub models): routing, citations, no-stall, guard-repaired render prompts, creative-brief gate (pass / revise once / revise twice with 2 rounds / residual issues / writer exhausted / raising writer or reviser fail-soft), ad-copy gate (all pass / flagged copy revised + unflagged edit reverted / raising reviser fail-soft / residual issues), concept gate (all pass / flagged concept fixed + unflagged edit reverted + brand cue re-guarded / raising fixer fail-soft / residual issues), research PDF node (artifact + GCS / skipped without report / failure recorded), finalize_pipeline (evaluate + persist happy path with fake judge/GCS/BQ, no creatives → notice, failing gallery doesn't block BQ, eval row written last), creative_pipeline (the root's single call: research → ads → visuals + render → finalize end to end, then the root answers)
├── test_creative_root_prompt.py     # creative_agent root contract: memorize missing fields → one creative_pipeline call → final summary; root state-token set
├── test_eval_efficiency_gate.py     # efficiency gate: metric extraction, tolerances, warn-only latency, CLI exit codes, --update-baseline, deterministic tool-use check (TOOL_SPECS pinned to the real root agents), advisory tool-use rubric
├── test_creative_eval.py            # creative_eval schemas, scoring logic, config, report learning_used/learning_flags, JUDGE_VERSION export (date + optional ".N") + report judge_version (both report builders; old reports parse as "")
├── test_eval_gates.py               # creative_eval binary gates: schema defaults (old reports parse), passed = score AND gates, gates_pass_rate over judged creatives only, brief block (brace-safe), judge-only response schemas (gates required, no code-set fields), gate normalisation (zero gates reported → fails, partial omission passes as "not checked" + report warning, lenient names, no brief → "no brief"), presence/violation gate wording, advisory brand cue, rendered image as a gs:// Part + image-QA hint, unreadable-image fail-soft to prompt-only limited to image 4xx, finalize "failed checks" / "evaluation failed" / gates pass-rate line, the 2026-10-08 rater-audit gates (no_visual_defects, trend_risks_respected + brief "Trend risks" line, stricter trend motif, prompt gate counts), the 2026-10-12.2 trend_connected ad-copy gate (blocking, judged without a brief, "no trend" pass on an empty trend)
├── test_eval_dimensions.py          # creative_eval.dimensions labels mirror frontend/src/lib/eval-dimensions.ts (drift test)
├── test_crf_completion.py           # CRF worker completion check: PROCESSED only on finalize_done, cut-stream poll, one retry only on a positive end-of-invocation signal (quiet time alone never retries; idle → stalled), budget
├── test_crf_config.py               # env-driven CRF config: one frozen CrfConfig (required project, clamps, lazy reads, cross-checks that log + fall back), CREATIVE_AGENT_ENGINE_ID normalisation, CRF_WORKER_TRANSPORT (tasks needs an https URL + invoker SA)
├── test_crf_entrypoint.py           # crf_entrypoint orchestrator (issue #46)
├── test_crf_hardening.py            # CRF JSON logs, malformed messages ACKed, client singletons, bounded BQ waits, per-invocation Agent Runtime client (two asyncio.run loops)
├── test_crf_tasks_dispatch.py       # CRF Cloud Tasks transport: one OIDC HTTP task per row, deterministic name per row attempt, QUEUED before the first task, AlreadyExists = dispatched, other errors leave the row QUEUED (fake: crf_fakes.FakeTasksClient)
├── test_crf_sql_params.py           # CRF SQL safety: allow-listed identifiers, parameterized values, sensitive trend_context rows excluded from the orchestrator SELECT
├── test_crf_row_state.py            # CRF row state: transition guards, compare-and-set lock + attempt fence (lost_fence), row key (uuid, target_trend) + legacy fallback, > 1 row = error, QUEUED before publish, re-read row drives the run (missing/sensitive → FAILED), PROCESSED never FAILED, pre-session requeue, publish timeout (fakes: crf_fakes.py)
├── test_crf_worker_async.py         # async worker path of the CRF (issue #45)
├── test_crf_worker_http.py          # CRF worker HTTP entry agent_worker_task: 200 for done/permanent, 503 for retry, deadline at request start, task headers logged, image load path
├── test_crf_time_budget.py          # CRF worker budget: deadline from request start incl. the safety margin, best-effort bounded session delete, compact event logs, row-named session user ids
├── test_date_prompts.py            # the shared DATE_CONTEXT_LINE in trend research / brief / copy / art-direction prompts (optional tokens, no literal dates), scout as_of/event_hint, Current Moment section, TREND_SCOUT_HINT_BLOCK
├── test_cloudbuild_agents.py        # Cloud Build engine deploys: cloudbuild.agents.yaml structure, deploy.env profile drift, clean-env wrapper, engine_handoff.py
├── test_engine_handoff_worker.py    # engine_handoff.py apply --update-worker: PROCESSING-rows guard + shell-free gcloud update of the CRF worker's CREATIVE_AGENT_ENGINE_ID + pinned-traffic move to the new revision and its verification
├── test_deploy_utils.py             # deploy_agent.py utils (env file, extra_packages, runtimes.create) + integration_test.py (skips, creative_agent smoke output assertion)
├── test_deploy_import_graph.py      # each agent's import-time repo-root packages ⊆ its AGENT_EXTRA_PACKAGES bundle (subprocess, slow)
├── test_deploy_packaging.py         # deploy_agent.py stages a filtered copy of each package (no .adk/, __pycache__, caches; refuses if one got staged) and runs the SDK from the staging root
├── test_deploy_env_contract.py      # deployment/deploy.env ↔ ENV_VAR_DICT: every runtime knob read in the engine reaches it
├── test_dependency_groups.py        # slim runtime dependency set: eval / experiments / CRF-only packages live in dependency groups, runtime code imports none of them
├── test_empty_turn_retry.py         # root orchestrators re-ask a clean empty model turn (empty_turn_retries) instead of ending the run
├── test_experiments_api.py          # /experiments routes: create→ready, 400/404/409, traffic, stop, TTL reaper, reconcile, authz, snapshot_arms (person_variants never change the arms; cast creatives → cast_creative unless their consent is in active_cast_consents, then consentId on the arm; person-refs/variants images → person_image; expired timely → timelyExpired + create warning), §9 scenarioOverrides validation + bandit parity, deploy lease (one deployer, expiry, heartbeat, release)
├── test_experiments_consent.py      # cast arms at create: active consent → consentId, 400 consent_not_active (revoked / other owner / missing / unknown), 503 consent_unavailable with nothing written, person_image still refused; revoke hook stop_experiments_for_consent stops live/deploying experiments with the consent only (idempotent), via DELETE /person-refs; hook or teardown failure → revoke 502, retry finishes
├── test_experiments_backends.py     # VertexDeployer (stepwise/resume, labelled-resource reuse, teardown of extras) + CloudRunJobsRunner env overrides, fakes
├── test_experiments_continuous.py   # §11 continuous learning in the api: traffic-body validation, job env, stitched /metrics + batch-means summary
├── test_experiments_metrics.py      # pure ExperimentMetrics aggregation (CI bands, totals, arm share, segments)
├── test_experiments_series.py       # pure §8 /creatives aggregation (windows, share, segments, missedClicks, engagedSecondsPer1k)
├── test_experiments_shifts.py       # §10 in the api: shift validation + bandit parity, numbered traffic runs (runs/{n}.json, env overrides, trafficRuns), ?run= reads incl. legacy NULL rows, shift_response + regime aggregation, regime SQL, unmigrated-table fallbacks
├── test_experiments_store.py        # bandit_experiments MERGE/SELECT builders, typed params, §8 series SQL builders, both stores, deploy-lease UPDATEs, unknown-column tolerance
├── test_ratings_api.py              # /ratings routes: PUT validation (400 reasons, unknown creative_key), upsert idempotency (created_at kept), GET listing, foreign/unknown session 404 (incl. ownership ValueError), judge fields from state or the GCS report (cached, fail soft; conservative headline/id matching), store 502s, calibration endpoint (current judge version only, excluded_other_versions, by_learning split), judge_version + learning_used snapshot at PUT, enforce-mode 401/403, allowlisted fail_reasons (400 invalid_fail_reasons, body-shape 422s, deduped, other-kind reasons dropped, emptied on pass), older rows without the learning columns, learning context stamped at PUT (normalize_brand brand, canonical visual_style, tone Literal, angle id; anything else "")
├── test_ratings_store.py            # creative_ratings MERGE/SELECT builders (fully parameterised; fail_reasons as an ARRAY<STRING> param), learning-context columns, judge_version/learning_used columns (updatable, in the calibration SELECT), BigQuery store over the fake client, in-memory store, RATINGS_STORE selection + fallback
├── test_share_snapshot.py           # runserver/share_snapshot.py: allowlisted v1 snapshot (exact creative keys; no prompts/rationales/judge notes/ids/gs:// URIs), eval-matching.ts pairing parity (id → headline → index), single creative + eval block (gate labels, advisory, copy/visual verdicts, mean score), concepts without images skipped / none → no_images, unknown concept → unknown_concept, aspect-ratio override + fallbacks, JSON-string state; cast concepts: included only when consent_lookup returns allow_public_share True (no lookup / private / unknown / no consent_id → skipped with person_not_shareable, pipeline order), named non-shareable → person_not_shareable, person_consent_ids recorded but never in the snapshot, cast_consent_ids
├── test_shares_store.py             # creative_shares MERGE/SELECT/UPDATE builders (fully parameterised; concept_names as an ARRAY<STRING> param), schema JSON == SHARE_COLUMN_TYPES, idempotent owner-checked revoke, list_with_consent (owner-only, revoked too, `@consent_id IN UNNEST(person_consent_ids)`), BigQuery store over the fake client, in-memory store, SHARES_STORE selection + Cloud Run fail-loud
├── test_share_images.py            # runserver/share_images.py: to_share_jpeg re-encode (JPEG RGB progressive, long edge ≤1600 px, aspect kept, no upscale, RGBA/LA/P/L inputs, transparency → white, EXIF stripped, 2048² render ~8.75 MB → ~0.73 MB)
├── test_shares_api.py               # /shares routes with a fake GCS: person-refs/variants images refused (person_image), re-encoded <i>.jpg images (≤1600 px, image/jpeg) + snapshot.json only under shares/<token>/ (cache-control, content type), $SHARE_BASE_URL URLs, eval report from GCS/state, 400 reasons (body, unknown concept, no images, image_outside_bucket incl. traversal, non-creative app), foreign/unknown session 404, rollback + 502 on download/upload/store failure, 200-share cap (429), 503 without a bucket, list newest first, revoke deletes objects (cross-owner 404), enforce-mode 401/403; person share scope: named non-shareable cast creative 400 (private / revoked / other owner / unknown consent), slate skips it (`skipped`, row person_consent_ids []), public-share consent includes it (row person_consent_ids, never in snapshot.json), consent store error 503 consent_unavailable before any write, revoke_shares_for_consent revokes + deletes only that consent's shares (idempotent; GCS failure raises, retry finishes); hardening: consent revoked between check and put → share revoked + 409 person_consent_changed, sources only directly in the run's output folder (output_prefix segment rules), a source whose metadata consent_id isn't shared → 400 person_not_shareable, share images carry no metadata
├── test_person_refs_store.py        # person_references MERGE/SELECT/UPDATE builders (fully parameterised; person_renders as an ARRAY<STRING> param), schema JSON == PERSON_REF_COLUMN_TYPES, active_for (owner + not revoked), idempotent owner-checked revoke, add_renders (owner-guarded deduped UPDATE, revoked consents too), set_photo_min_side (guarded UPDATE that only fills a NULL photo_min_side), photo_min_side INT64 column, BigQuery store over the fake client, in-memory store, PERSON_REFS_STORE selection + Cloud Run fail-loud
├── test_person_refs_api.py          # /person-refs routes with a fake GCS: slug_for vs the shared golden tests/fixtures/person_slugs.json (also read by frontend gcs-route-person.test.ts; a.b@x.com vs a_b@x.com apart), photo URI only under the caller's own person-refs/<slug>/ (other prefix/bucket/type/nesting/traversal → 400 before any GCS read), photo HEAD (missing/too big/not image → photo_unreadable), photo size via Pillow (undecodable/download error → photo_unreadable; shorter side < PERSON_PHOTO_MIN_SIDE → photo_too_small; stored photo_min_side + low_resolution < 1024 in POST and GET; env clamp 256–4096), lazy size backfill on GET (cached on the row; download/decode/store failures non-fatal; photos outside the owner prefix skipped), adult attestation, stale consent text, duplicate photo 409, 50-person cap 429, 503 without a bucket, list + upload prefix, revoke runs hooks + deletes the photo (idempotent; cross-owner 404; cleanup failure 502 then retry), active_consent helper, enforce-mode 401/403; revoke cascade: render_path (bucket only, never shares/ / person-refs/ / traversal), record_renders (deduped, owner-only; after a revoke deletes at once), revoke order hooks (re-read record) → renders (only objects with metadata consent_id = this consent) → photo, render delete failure 502 then retry, async_app registers the share hook + session service; record_session markers (deduped, owner-only, malformed refused), revoke deletes cast renders + variants listed in recorded sessions' state (deleted session skipped), revoke deletes person swaps whose blob metadata source_consent_id names the consent (recorded URI + session state), session read failure 502 then retry
├── test_person_consent_drift.py     # runserver.person_refs CONSENT_TEXT + CONSENT_TEXT_VERSION mirror frontend/src/lib/person-consent.ts (drift test)
├── test_async_runs_person.py        # person_reference consent check: kick-off 400 person_reference_invalid before any claim (revoked / foreign / other photo / bad shape), 503 on a store error, empty or non-creative unaffected; resume 400 person_reference_revoked; router mapping; cast renders recorded on the consent's person_renders when a segment ends (done and error, deduped across segments, store failure never blocks the done marker, non-creative apps skipped), cast_renders; kick-off records the session marker (store error → 503 before anything starts)
├── test_person_config.py            # PERSON_SAFE_STYLES (canonical names, default) / MAX_CAST_CONCEPTS (clamped 0..4) + ENV_VAR_DICT / .env.example
├── test_person_casting_guard.py     # concept_guard.enforce_person_casting: one test per clearing rule + cap order, reference wording appended / neutralised, wiring into ensure_trend_and_product_callback
├── test_person_render.py            # person_render helpers (URI shape, block reasons, ALLOW_ADULT + BLOCK_PROMINENT_PEOPLE config; a prominent-people block falls back) + generate_image: person part + ALLOW_ADULT for cast concepts only, safety/4xx fallback without the person (person_reference_rejected + __issues), URI outside person-refs/ ignored and never logged, render-time guard, no cast key without a reference, cast uploads carry blob metadata consent_id (uncast none) and are not saved as ADK artifacts, _save_to_gcs metadata only when given
├── test_image_qa_person.py          # image QA likeness: person fields default on old payloads, rules only for cast concepts (not critical), correction restates likeness, photo attached + comparison text only with person_image; cast re-render keeps the person part
├── test_image_qa_composition.py     # variant-only composition check: old payloads parse, rule `composition changed` (not critical) only with original_image (cleared otherwise), correction restates keep-everything + capped brace/quote-free differences, original attached last with its label, stored QA record carries the composition fields only for edits
├── test_person_edit.py              # creative_agent.person_edit: edit instruction (keep everything else, brace-free), contents order (instruction, labelled original, labelled person), unwanted_logo line, correction instruction, nearest aspect ratio
├── test_eval_person_gate.py         # person_depicted_respectfully: in VISUAL_GATES / labels / prompt, passed in code with "no person cast" unless generated_images[c].cast, unversioned-report inference unchanged
├── test_eval_person_photo.py        # cast concepts' judge request: person reference photo Part after the render + ground-truth appearance text (uncast / missing / invalid / other-bucket photo → none), person_photo_uri parity with the engine shape check, unreadable photo → render-only retry, URI never logged or on the report, state person_reference threaded through
├── test_eval_label_text.py          # judge 2026-10-13.1: no_visual_defects exempts small product-label text (large ad gibberish stays a defect), text_correct still exact for requested text + label fine print not counted, JUDGE_VERSION 2026-10-13.1
├── test_eval_safer_angle_gate.py   # safer_angle_respected (ad copy + visual): registered/labelled "Respectful reference", JUDGE_VERSION 2026-10-15, prompt counts 9/10 + gate/trend_connected wording, normalize matrix (inactive → "no safer angle" code pass whatever the judge says; active keeps the verdict; active + omitted → failed "not checked" + warning), brief block Safer angle / Sensitive event lines only when active, flag reaches both judge kinds, fingerprint includes the safer-angle fields
├── test_eval_timely_gate.py         # timely_claim_supported (ad copy + visual): registered/labelled, JUDGE_VERSION 2026-10-15 (pinned again by the safer-angle gate), prompt counts 9/10, "not timely" code pass unless the creative (or a visual's paired copy) is flagged under a timely trend_situation, omitted → failed "not checked" + warning, Timely: yes + CURRENT_MOMENT block only for timely creatives, fingerprint includes trend_situation, trend_valid_until / timely report fields (old reports parse)
├── test_rating_reasons_drift.py     # runserver.rating_reasons fail-reason enum + labels + per-kind lists (FAIL_REASONS_BY_KIND vs failReasonsFor) mirror frontend/src/lib/rating-reasons.ts (drift test)
├── test_calibration.py              # judge-human calibration maths: Cohen's kappa (textbook value, degenerate single-class reasons), tie-averaged Spearman, per-kind slices, CSV string coercion, current-JUDGE_VERSION filter + excluded_other_versions, learned/not-learned split, scripts/eval_calibration.py over a CSV (incl. --judge-version)
├── test_export_concurrency.py       # creative_agent export tools: per-run scratch isolation (issue #104), no gallery upscale
├── test_image_prompt_guide.py       # IMAGE_PROMPT_GUIDE rules: text cap, descriptors not templates, Educational mapping, trend motif, trend_motif schema field, REFERENCE_IMAGES section (role-only references, ignore-text left to the tool, style ref vs family diversity), Subject+Action+Location+Composition+Style blocks, typography, no unrequested logos
├── test_creative_hardening.py      # research PDF escapes raw HTML + empty Story archive (no cwd files), _is_public_ip table (is_global: 100.64/10 + NAT64 metadata refused), _download_blob size cap before download + missing → NotFound, image retry predicate (ConnectError, 408)
├── test_image_reference.py          # generate_image multimodal contents + valid ImageConfig; multiple reference images (resolve_references legacy fold-in/dedupe/cap/invalid roles, ordered parts, one failed fetch skips only that ref + unavailable-role line, numbered role block + ignore-text line, SSRF/size/content-type fetch hardening) (image QA off via the `image_qa_off` conftest fixture)
├── test_image_qa_pipelining.py      # generate_image pipelining: QA of image N overlaps render N+1 (renders never overlap), re-render queued right after the render in flight, budget claimed in concept order whatever the QA finish order, mixed pass/fail/re-render/QA-error outcomes unchanged, render failure raises + cancels pending QA, inert with QA disabled
├── test_render_concurrency.py       # generate_image keeps ≤ IMAGE_RENDER_CONCURRENCY image-model calls in flight: first renders in concept order, concept i waits for concept i−N's first render, re-renders share the cap; the next concept's re-render starts before this one's returns (early claims_done release)
├── test_image_qa.py                 # post-render image QA: expected_text, the qa_failures matrix (brand cue advisory, unrequested logo fails, expected_text authoritative for text checks, empty motif/product never fail), the conservative instruction wording (image prompt capped, partial product view, gibberish/logo allowlist, meme slang), correction_text (quote-stripped, capped, "no new text"), inspect_image request (image Part, schema, temperature 0), IMAGE_QA_* config + ENV_VAR_DICT; generate_image flow (fail → re-render → pass keeps the second, both fail → fewer-failures attempt + image_qa__issues, ties → latest, critical weighting + never keep a new unsafe, per-image + per-run budget, fail-open → image_qa__unavailable (not surfaced), render off the event loop, disabled = one render) and the gallery "Image check" line, malformed (mirrored) product rule, off-model product + malformed hands rules (not critical; finger-count instruction, stylised-hands exemption)
├── test_interactive_resume_graph.py # real interactive_creative App via start_run/start_resume: checkpoint pause → NodeTool resume, fail-once retry counts, one BQ key (write_trends_to_bq added to the root's tools for the test), the resumed root's history keeps the checkpoint response but no sub-agent turns; checkpoint-1 brief edit → creative_brief_md in the ad drafter's prompt + PDF re-save; checkpoint-2 revision runs the reviser once and re-presents (fail-soft on a raising reviser)
├── test_interactive_copy_revision.py # checkpoint-2 user revision: prepare_copy_revision flags every copy (one revision cap), input clearing after the reviser, fail-soft on_error
├── test_interactive_prompts.py      # interactive checkpoint contracts: brief/report edit → PDF re-save (no research re-run), checkpoint-2 feedback → one revision; brace-safety
├── test_no_legacy_agent_engines_api.py # guard: no legacy vertexai agent_engines API in repo call sites
├── test_model_request_timeout.py    # per-request model timeout (MODEL_REQUEST_TIMEOUT_SECONDS / flash FLASH_MODEL_REQUEST_TIMEOUT_SECONDS → HttpOptions.timeout) + TimeoutError retry
├── test_observability.py            # shared agent_common observability callbacks
├── test_otel_flag.py                # ADK_OTEL_TO_CLOUD parsing + async_app wiring (opt-in Cloud Trace on the api)
├── test_pipeline_structure.py       # pipeline composition (graph nodes/edges by name, truthy terminals) + placement-env wiring
├── test_public_api.py               # creative_agent public facade (curated __all__ reuse surface) + guards: no legacy SequentialAgent/ParallelAgent/LoopAgent
├── test_retry_node.py               # RetryUntilKeyNode (retry-on-empty graph wrapper; transient errors as missed attempts; is_populated; NodeTool no-stall)
├── test_root_history.py            # agent_common.history: ADK replays earlier pipelines' sub-agent turns + node inputs into a root's request (measured on the real creative root), the trim keeps only user/root turns + call/response pairs, precise matching, both roots wired
├── test_early_ad_copy_eval.py       # early ad-copy judging (evaluate_ad_copies_only / evaluate_ad_copies_node): partial stored, reused by finalize only when the fingerprint matches, failed copies re-judged, fail-soft
├── test_finalize.py                 # creative_agent/finalize.py nodes: summary formatting, judge off-loop on a snapshot, per-step isolation + ordering in persist_node, sync steps off the loop, skipped eval writes
├── test_fail_soft_node.py           # FailSoftNode (optional-step exception → on_error state delta; successors still run; DynamicNodeFailError unwrap)
├── test_retry_config.py             # scoped RetryConfig constants on infra agents
├── test_research_alignment.py       # research prompts, inputs and brief helpers agree with each agent's input and the deterministic gates (planner fallbacks, named tokens, source de-duplication, schema/doc wording)
├── test_safety_plugins.py           # opt-in Model Armor (agent_common.safety): env parsing, root-only scoping, every agent's App + canned-loader wiring
├── test_scenario_presets_sync.py    # frontend scenario-presets.generated.json matches bandit/ (no JAX)
├── test_sanitize.py                 # lone-surrogate scrubber (agent_common.sanitize)
├── test_schemas.py                  # Pydantic schemas in the creative_agent pipeline
├── test_schema_enums.py             # no model-facing creative_agent / creative_eval schema offers "" as an enum value (Vertex rejects empty enum strings with 400 INVALID_ARGUMENT; #321)
├── test_sdk_versions.py             # guard: aiplatform 2.x ships both agentplatform + vertexai surfaces
├── test_style_shortlist.py          # per-session stratified style shortlist (families match the guide palette; exclusion of recently used styles and preference of well-rated ones keep the 2/3/1 strata)
├── test_tools.py                    # backend tool functions (pure logic, no I/O)
├── test_tools_retry.py              # infra tools propagate (don't swallow) exceptions
├── test_trend_context.py           # trend_scout/trend_context.py: per-trend context JSON from info_gtrends + selected_gtrends (fenced JSON, markdown fallback, malformed → ""), plus sensitive / sensitive_reason only for a flagged term (unflagged output unchanged)
├── test_trend_situation.py         # creative_agent/trend_situation.py: the validator table (upcoming/just-happened ok; no source / unknown src id / outside the window / wrong side of today / no outcome / URL date before the event → unknown; live/sensitive/ongoing/evergreen/low confidence never timely), valid_until, run-timezone dates, tone playbook, one-line moment, prompt tokens
├── test_trend_situation_graph.py   # the trend situation branch inside combined_research_pipeline (stub models): grounded draft → validated moment in the brief writer's prompt + compact brief; synthesizer raises → unknown; empty turns exhaust → unknown; disabled → no model call, stale draft cleared
├── test_timely_rules.py            # MAX_TIMELY_CONCEPTS, build_timely_rules per phase (incl. the loss wording), timely_rules seeded only when timely, {timely_rules?} placement + sha256-pinned byte-identical instructions without it
├── test_safer_angle.py            # creative_agent/safer_angle.py (opt-in safer angle for sensitive trends): parse_hint table, derive_state truth table (opt-in AND scout flag; "true" ≠ True), brace-free rule texts carrying the safety floor, state init (both creative apps), {safer_angle_*_rules?} placement + sha256-pinned byte-identical brief/research instructions without it, check_brief(safer_angle_active=) rules, brief gate (stray cleared, active checked), "Safer angle" rendering, checkpoint-1 brief edit
├── test_temperature_profile.py     # agent_common/sampling.py GEMINI_TEMPERATURE_PROFILE: tuned|default|unknown parsing; fresh-interpreter golden of every agent's temperature (tuned = pre-seam values, default = all 1.0 or unset)
├── test_temporal_terms.py          # creative_agent/temporal_terms.py: outcome/date/weekday terms flagged, idioms (win win, lose weight, …) pass
├── test_timely_guards.py           # enforce_timely_cap / enforce_timely_copy_cap, evergreen temporal-term + pairing issues (only with a timely moment), copy gate write-back, concept guard callback
├── test_timely_records.py          # generate_image adds timely: True + valid_until only to the timely concept's render record; every other record keeps its exact keys
├── test_timely_expiry.py           # runserver/timely.py timely_expired vs the shared golden tests/fixtures/timely_expiry.json (also read by the frontend's lib/timely.ts tests)
├── test_trend_situation_rederive.py # batch (CRF) runs re-derive the current moment: the branch never reads trend_scout_context / trend_detected_on, the worker seeds no trend_situation
├── test_trend_scout_graph.py        # trend_scout understand_trends graph run end-to-end (stub models; incl. an invalid briefing on every attempt → no exception, info_gtrends unset, retry_exhausted recorded, root continues)
├── test_understand_trends_schema.py # TS-6: the synthesizer's AnalyzedTrends output_schema keeps the info_gtrends shape trend_context / picker / trend cards parse; a reply failing the schema is blanked by build_invalid_reply_dropper (degrades like an empty reply); schema + scrub + dropper + temperature 1.5 wiring
├── test_logging_config.py           # N-1: importing agent_common + trend_scout adds no root log handlers; ensure_default_logging only fills an unconfigured root
├── test_trend_scout_history.py      # trend_scout root history trim (measured on the real App, stub models): gather/pick AgentTool runs are isolated, the understand_trends (and, on a cache miss, trend_digest) NodeTool leaks 2 sub-agent turns + 2 node inputs into the root's final request without drop_other_agent_context, 0 with it; resumed review_trends checkpoint response kept; trim wired before the rate limiter
├── test_trend_scout_concurrency.py  # trend_scout GCS-export tools: per-run scratch isolation
├── test_trend_scout_tools_off_loop.py # trend_scout BigQuery/GCS tools: async, blocking I/O off the event loop, model-facing declarations unchanged (golden)
├── test_trend_scout_logging.py      # trend_scout wiring of the shared observability callbacks
├── test_trend_digest.py             # trend_scout trend digest (digest.py graph + digest_store.py helpers/I/O): terms_hash / normalize_draft / SQL builders (param-only) + schema file; real-App graph runs for cache hit (state restored, no model call), miss (describe → classify reads summaries → one MERGE), failed describe (unavailable, no write), < 80% summary coverage, flags must cover every term (partial flags → no write; a row without full flags → miss), a stale draft cleared on the miss route, other terms_hash, BigQuery error/timeout, disabled (classifier only)
├── test_trend_scout_sensitive.py    # trend_scout sensitive trends: TrendFlags schema, classify_trends_agent wiring + instruction (inside trend_digest; the root digests right after gather, both branches), pick prompt never selects flagged terms, save_search_trends_to_session_state refuses flagged auto picks (dict/JSON/fenced flags; interactive picks saved; a truthy non-bool interactive flag is not a human pick; malformed flags never block), auto picks limited to the gathered raw_gtrends (normalised, saved as gathered, duplicates skipped), classifier summaries can only add a flag (fenced as data), sensitive_flags parser
├── test_trend_tie.py                # creative_agent/trend_tie.py: distinctive trend token / full phrase / brief motif (adjacent-word motif phrases) tie, generic query tokens + years + short tokens don't count, avoid terms never count, empty trend passes, tie_terms examples
├── test_endorsement.py             # creative_agent/endorsement.py lexicon: whole-word/fold/hyphen matching, pinned idioms (partner up, real estate, lower-case rip)
├── test_safer_angle_guards.py      # safer angle on a sensitive trend: copy gate endorsement flag + re-enabled trend tie (risk-named event words excluded) + safer_angle_named_copies, trend_sensitive_hint tie skip, image-QA public_figure_likeness (critical, only when active)
├── test_safer_angle_copy_tie.py    # safer angle: at least ceil(n/2) copies must name the subject or use a distinctive safer-angle word (generic/product/brand/risk/avoid words never count), else every untied copy is flagged; gate routing + interactive residual path
├── test_visual_concept_prompts.py   # visual concept prompts: shared brand_cue / copy-quoted in-image text / brief avoid + fit_mode rules (drafter, critic, finalizer), finalizer angle_id, interactive reviser keeps brand_cue; brace-safety
├── test_visual_intent_prompts.py    # optional visual-intent {key?} tokens + IMAGE_PROMPT_GUIDE no-braces + {style_shortlist?}/composition/text-cap rules
├── test_workflow_api_contract.py    # offline pins on the upstream ADK graph-Workflow behaviours the P2 migration relies on
├── test_worker_retry_message_drift.py # CRF worker retry message == runserver AUTO_RETRY_MESSAGE (not test_crf_*: the CRF venv has no runserver)
│                                    #
│                                    # experiments/ harness unit tests (pure/offline — no creds, no network)
├── test_creative_latency_poll.py    # poll_to_terminal retries a transient slow/failed poll
├── test_experiment_parse.py         # creative_latency event-log parser
├── test_experiment_aggregate.py     # creative_latency N-trial aggregation math
├── test_experiment_logs.py          # Cloud Logging 429/503 filter builder
├── test_experiment_plot.py          # Plotly report builder smoke (no Chrome)
├── test_experiment_render_static.py # matplotlib static-figure renderer
├── test_render_concept.py          # creative_agent.render_concept without a ToolContext: contents order (refs then person), QA re-render, cast render + likeness photo, blocked person without fallback → rejected (no person-less render), missing photo, fallback neutralises the prompt, QA off / QA error; render_edit (original then person, ALLOW_ADULT, QA gets person + original, blocked edit → rejected with no fallback, re-render repeats the edit with the correction, QA off); generate_image batch: a render error aborts before any upload (deliberate change), an empty render skips only that concept, a QA error → image_qa__unavailable
├── test_render_parity.py           # generate_image after the render_concept refactor vs a golden recording of the pre-refactor code (tests/fixtures/render_parity.json): every image call (contents, config), QA call, upload and state key per scenario (refs + strictness + override + budget, person cast/block/likeness, QA off + photo missing); UPDATE_RENDER_PARITY=1 regenerates
├── test_render_robustness.py       # generate_image: de-duplicated artifact keys ("Game Day!" vs "Game Day"), the render fingerprint (changed concepts / person re-render, legacy True skips), the timely cap re-applied at render time
├── test_variants_api.py            # /variants routes: queued → rendering → done with upload under variants/<slug>/ (metadata consent_id, private no-store), deltas carry only person_variants, cache hit, joining a live render, semaphore queueing, daily cap (process + state), ownership/authz, consent checks, concept_not_found / not_castable, person swap (cast concept → castable mode swap + cast_consent_id; same_person_as_cast refused before the cap; another person allowed), appearance-override line + versioned cache key, human-subject check ignores the variant wording, unsafe output folder, run in progress, rejected/failed renders, orphaned pending → interrupted, consent revoked mid-render → consent_revoked (no upload), variant URI recorded on person_renders, revoke during upload → object deleted + consent_revoked, record failure → render_error (no upload), concurrent duplicate POSTs render/count once, concept_not_ready; edit mode: the renderer gets the original render's bytes, a swap is recorded on both consents with blob metadata consent_id + source_consent_id, revoking the cast person deletes the swap (recorded URI and session marker), the cast consent must be active, a missing original → 409 "Original image is no longer available" (before the cap), an original outside the run folder → image_outside_bucket, cast person revoked mid-render → consent_revoked, v3 cache key; render_variant with a fake image client (edits the original: contents order, nearest aspect ratio from the original's size (concept ratio if unreadable), strictness line, ALLOW_ADULT, QA gets the original, no fallback)
├── test_person_calibration.py       # person-reference calibration spike: grid, block reasons, summary
├── test_swap_compare.py             # person-swap edit-vs-regenerate spike (experiments/person_reference/swap_compare.py): comparison parsing, edit contents, nearest aspect ratio, summary + decision (no API calls)
├── test_quota_spread_batch.py       # quota-spread concurrent batch harness (pure core)
├── test_quota_spread_analyze.py     # quota-spread slope + tidy CSV + plots + quality harvest
└── test_quota_spread_upload.py      # Agent Platform Experiments uploader record shaping
```

## Categories

- **Schemas & config** — `test_schemas.py`, `test_schema_enums.py`, `test_creative_eval.py`, `test_eval_gates.py`, `test_config.py`,
  `test_agent_common_models.py`: Pydantic validation, model-location pinning, per-agent
  config resolution.
- **Pipeline & callbacks** — `test_pipeline_structure.py`, `test_callbacks.py`,
  `test_agent_common_state.py`, `test_agent_common_clients.py`,
  `test_agent_common_idempotency.py`, `test_retry_config.py`: agent composition, state
  init, rate limiting, citation regex, idempotent BigQuery row keys, scoped `RetryConfig`.
- **Graph Workflows** — `test_workflow_api_contract.py`, `test_retry_node.py`,
  `test_creative_agent_graph.py`, `test_trend_scout_graph.py`,
  `test_interactive_resume_graph.py`: the upstream ADK Workflow contract, the
  `RetryUntilKeyNode` wrapper, and each agent's graphs run end-to-end over stub models
  (doubles in `_fakes.py`).
- **Bandit core** — `test_bandit_features.py`, `test_bandit_config.py`,
  `test_bandit_linear_ts.py`, `test_bandit_baselines.py`, `test_bandit_environment.py`,
  `test_bandit_simulate_metrics.py`, `test_bandit_notebook_parity.py`: the offline JAX
  contextual bandit (PR 1 of the bandit plan); JAX comes from the uv dev group.
  `test_bandit_notebook_parity.py` renders all 9 figures of
  `experiments/bandit/notebook_parity.py` from committed fixture data
  (`tests/fixtures/bandit_parity/`, ~160 KB: one episode at `--horizon-scale 0.001`,
  trimmed to the keys `plot_all` reads, floats at 4 significant digits; not `slow`),
  plus a `slow` generation smoke that runs only `clear_winner` with LinTS + oracle
  through `generate()`. **Regenerate the fixture** with
  `uv run python tests/fixtures/bandit_parity/regenerate.py` when a plot function reads
  a new field, or a figure/run is added (then extend the key lists in `regenerate.py`);
  the output is deterministic, so an unchanged generator gives no diff.
  Contracts §9 scenario overrides are covered across `test_bandit_config.py` (strict
  parsing, bounds, round-trip omission), `test_bandit_environment.py` (each knob's effect
  on the ground truth), `test_bandit_simulate_metrics.py` (CLI flags),
  `test_bandit_traffic.py` (the traffic job simulates the tuned scenario) and
  `test_bandit_predictor.py` (configs with overrides load).
  Contracts §10 scripted shifts live in `test_bandit_shifts.py`: strict parsing and
  ctr-mode-scaled bounds, common random numbers between shifted and unshifted
  environments, each kind's effect at its round, time-order `"leader"` resolution,
  drift composition, `shift_response` / `merge_checkpoints` / `regime_stats`, and the
  CLI. `test_scenario_preview_golden.py` also checks the after-shift preview fixture.
  The golden fixtures are only written with `UPDATE_PREVIEW_GOLDEN=1`; a missing one
  fails the test instead of being regenerated silently.
  `test_bandit_endpoint_parity.py` drives the real `BanditPredictor` through the traffic
  loop and checks that its LinTS picks the simulator's arms round for round (contracts §2
  policy stream: click/engaged, discount, request splitting, and the §10 ghost up to the
  first shift).
  **Sizes:** tests that run episodes use the shared sizes in `_bandit_sizes.py`
  (`HORIZON_S` = 1000 for plumbing and exact parity, `HORIZON_M` = 4000, `BATCH` = 100).
  `simulate.run_episodes` compiles one XLA program per policy x batch x number of
  batches x arms x episode-chunk shape, so a shared size is a cache hit (in process, or
  in the persistent compile cache across files and workers), while a one-off horizon
  costs ~1-3 s per policy. Only keep a different size when the assertion needs it, and
  say why in a comment (e.g. the statistical checks in `test_bandit_simulate_metrics.py`).
  **On a JAX (or jaxlib) bump, seed-sweep the exact-parity tests**
  (`test_bandit_endpoint_parity.py`, the ghost checks in `test_bandit_traffic.py`, the
  golden fixtures): they compare float32 programs compiled on different paths (the
  predictor's padded kernels vs the simulator's scan) bit for bit, so an XLA change in
  fusion or reduction order can flip a near-tied arm choice. Re-run them over a few
  `seed=` values in `_cfg` (or `build_sim_config(..., seed=s)`) before trusting a green
  run on the default seed.
- **Tools** — `test_tools.py`, `test_tools_retry.py`: pure tool logic, plus the contract
  that infra tools raise (rather than swallow errors into status dicts) so ADK retry works.
- **Deployment & fan-out** — `test_deploy_utils.py`, `test_deploy_import_graph.py`, `test_deploy_packaging.py`,
  `test_deploy_env_contract.py`, `test_dependency_groups.py`, `test_build_context.py`,
  `test_cleanup_policies.py`, `test_cleanup_apply_sh.py`, `test_async_app_wiring.py`,
  `test_bandit_cloudbuild.py`, `test_creative_fanout_image.py`, `test_cloudbuild_agents.py`, `test_engine_handoff_worker.py`, `test_create_session_engine.py`,
  `test_crf_entrypoint.py`, `test_crf_worker_async.py`,
  `test_crf_config.py`, `test_crf_sql_params.py`, `test_crf_hardening.py`,
  `test_crf_completion.py`, `test_crf_row_state.py`, `test_crf_tasks_dispatch.py`,
  `test_crf_worker_http.py`, `test_crf_time_budget.py` (+ `test_worker_retry_message_drift.py`, outside the CRF venv): deploy
  mappings/env wiring, the `runtimes.create` path, and the orchestrator + worker Cloud
  Run Function paths (env-driven config, SQL identifier/parameter safety, malformed
  messages, client lifetimes, bounded BigQuery waits, JSON logs, the finalize_done
  completion check + one retry, the row-state transitions and fencing, the Cloud
  Tasks transport and the worker's HTTP entry point).
- **SDK guards** — `test_sdk_versions.py`, `test_no_legacy_agent_engines_api.py`: the
  aiplatform 2.x `agentplatform` + `vertexai` surfaces are present, and no repo call site
  uses the deprecated `agent_engines` API.
- **Concurrency** — `test_export_concurrency.py`, `test_trend_scout_concurrency.py`:
  in-process concurrent runs get isolated scratch dirs (issue #104); the gallery links the
  original renders without an upscale re-upload.
- **Prompts & facade** — `test_visual_intent_prompts.py`, `test_visual_concept_prompts.py`,
  `test_public_api.py`: optional visual-intent state tokens, the visual concept brand-cue /
  copy-quoted-text / brief rules, and `creative_agent`'s curated public reuse surface.
- **Image diversity** — `test_image_prompt_guide.py`, `test_style_shortlist.py`,
  `test_concept_guard.py`: the guide's text cap / descriptor palette / Educational mapping,
  the per-session style shortlist, and the trend-motif + product prompt guard.
- **Creative quality (brief → gates → QA → eval → calibration)** — `test_brief_check.py`,
  `test_brief_render.py`, `test_creative_brief_prompts.py` (structured brief + `brief_gate`),
  `test_copy_gate.py`, `test_claims.py`, `test_ad_copy_prompts.py` (`copy_gate` + flagged-only reviser),
  `test_concept_guard.py`, `test_text_match.py`, `test_visual_concept_prompts.py`
  (`concept_gate`), `test_image_reference.py`, `test_image_qa.py` (multi-reference renders,
  post-render QA + re-render), `test_render_concurrency.py` / `test_image_qa_pipelining.py`
  (pipelined renders under `IMAGE_RENDER_CONCURRENCY`), `test_research_alignment.py`, `test_finalize.py` (`finalize_pipeline`), `test_eval_gates.py`,
  `test_eval_dimensions.py` (binary gates vs advisory scores), `test_ratings_api.py`,
  `test_ratings_store.py`, `test_rating_reasons_drift.py`, `test_calibration.py` (human
  ratings + fail-reason chips + Cohen's kappa),
  `test_interactive_prompts.py`, `test_interactive_copy_revision.py` (checkpoint-1 brief
  edit, checkpoint-2 revision), `test_creative_root_prompt.py` (single `creative_pipeline`
  call); the revise / no-revise / exhausted graph paths live in `test_creative_agent_graph.py`.
- **Rating learning** — `test_rating_signals.py` (query + aggregation + note),
  `test_brand_history.py::TestRatingLearning` (opt-in/kill switch/effects, not-enough/unavailable records,
  concurrent fetch, one merged shortlist draw), `test_style_shortlist.py` (`prefer` keeps 2/3/1),
  `test_creative_agent_graph.py` (toggle on → `rating_signals` reaches the brief writer prompt; off → no query),
  `test_creative_brief_prompts.py` (`{rating_signals?}` block + rule in exactly three prompts), `test_config.py`.
  Strictness (each flag positive + default unchanged): `test_copy_gate.py` (`weak_cta`, `off_brief`),
  `test_concept_guard.py` (`text_problem`, `product_not_visible`, `trend_unclear`; idempotent re-guard),
  `test_pipeline_structure.py` / `test_callbacks.py` (gates + guard/recheck callbacks read `rating_strictness`),
  `test_image_qa.py` (prominent product / motif instruction, flags passed from state), `test_image_reference.py`
  (no-other-logos line on every render prompt, never doubled by `unwanted_logo`), `test_rating_signals.py` (`strictness_flags`), `test_creative_agent_graph.py`
  (learning step with fake ratings → copy gate caps CTAs at 6 words, concept gate allows one text concept, guard adds
  the prominence line), `test_creative_eval.py::TestReportLearningFields` (`learning_used` / `learning_flags`),
  `test_rating_reasons_drift.py` (frontend `STRICTNESS_LABELS` keys); frontend `learning-summary.test.tsx`.
- **Person casting** — `test_async_runs_person.py` (consent check), `test_callbacks.py` (`person_reference` /
  `person_reference_available` seeding), `test_schemas.py` (casting fields on every visual stage),
  `test_visual_concept_prompts.py` (`PERSON_CASTING_RULES`, `{person_casting_rules?}` in the six visual
  prompts, no casting text without a person), `test_person_config.py`, `test_person_casting_guard.py`,
  `test_person_render.py`, `test_image_qa_person.py`, `test_eval_person_gate.py`; frontend `person-select.test.tsx`,
  `person-casting.test.tsx`, `initial-state.test.ts` / `run-history.test.ts` (seed + Duplicate brief),
  `eval-checks.test.tsx` (gate label).
- **Personalised variants** — `test_render_concept.py` (incl. `render_edit`), `test_person_edit.py`,
  `test_image_qa_composition.py`, `test_render_parity.py` (golden pre-refactor
  recording of `generate_image`), `test_variants_api.py`, `test_experiments_api.py` / `test_experiments_consent.py` (cast arms need an active consent),
  `test_authz.py` (`/variants/{u}`); frontend `personalise-panel.test.tsx` (panel polling, swap mode + `lib/variants.ts`),
  `user-scoping.test.ts` (variants routes), `deploy-panel-arms.test.tsx` (cast proofs selectable), `arm-image.test.tsx` (consent-revoked arm placeholder).
- **Person share scope + revoke cascade** — `test_share_snapshot.py`, `test_shares_api.py`, `test_shares_store.py`
  (public-share consent filter, `skipped`, `person_consent_ids`, `revoke_shares_for_consent`),
  `test_person_refs_api.py` / `test_person_refs_store.py` (`record_renders`, `add_renders`, cascade order, metadata
  check, retry), `test_variants_api.py` (recorded before upload), `test_async_runs_person.py` (cast renders recorded
  at segment end), `test_person_render.py` (cast upload metadata); frontend `share-dialog.test.tsx` /
  `shares-api.test.ts` (left-out notice, messages), `proof-image.test.tsx` ("Image removed (consent revoked)").
- **Brand history** — `test_brand_history.py` (helper + node delta),
  `test_creative_agent_graph.py` (the note reaches the brief writer; disabled → no query;
  a raising step doesn't stop research), `test_pipeline_structure.py` (node in the START
  fan-out). `conftest.py` points the module's BigQuery and GCS getters at a raiser so no
  test reads the live table/bucket the repo `.env` names.
- **Trend situation** — `test_trend_situation.py` (pure validator + tone + rendering),
  `test_trend_situation_graph.py` (branch runs), `test_pipeline_structure.py` (branch graph in
  the START fan-out), `test_config.py::TestTrendStateKnobs`. `conftest.py` keeps the branch
  off (`_trend_state_off`) so other graph tests need not script its two stub models.
- **Timely slot** — `test_timely_rules.py`, `test_temporal_terms.py`, `test_timely_guards.py`,
  `test_timely_records.py` (render record marks), `test_trend_situation_rederive.py` (batch runs
  re-derive the moment), `test_brief_check.py` (timely angle), `test_brief_render.py` (timely angle +
  gallery label), `test_eval_timely_gate.py` (judge gate), `test_timely_expiry.py` (expiry golden,
  share guard + experiment warning live in `test_shares_api.py` / `test_experiments_api.py`);
  frontend `timely-proof.test.tsx`.
- **Safer angle (opt-in, sensitive trends)** — `test_safer_angle.py`, `test_brief_check.py`
  (activation default); frontend `creative-brief-drift.test.ts` (the field round-trips).
- **Sensitive trends + copy trend tie** — `test_trend_scout_sensitive.py` (classifier + auto-pick
  guard), `test_trend_context.py` (flag in `trend_context`), `test_crf_sql_params.py` (CRF
  skips flagged rows), `test_trend_tie.py` (matcher), `test_copy_gate.py` (trend-tie issue,
  skipped for a sensitive moment), `test_endorsement.py` (endorsement lexicon + pinned idioms),
  `test_safer_angle_guards.py` (safer angle: endorsement flag, re-enabled trend tie, named copies,
  hint-based tie skip, image-QA public-figure rule + plumbing), `test_safer_angle_copy_tie.py` (at-least-half safer-angle tie); frontend `trend-flags.test.ts`, `review-trends.test.tsx`,
  `trend-cards.test.tsx`, `campaign-form-sensitive.test.tsx`, `trend-situation.test.tsx`.
- **Async-job run model** — `test_async_runs.py`: detached kick-off returns immediately,
  `_drive_run` appends a `done`/`error` terminal marker, poll derives status + slices
  events by cursor, and resume re-runs with a `functionResponse` (resetting status to
  `running` first so multi-checkpoint interactive runs don't stop early). Only the
  terminal marker decides `done`/`error`: an in-pipeline error event that a fail-soft step
  contained keeps the run `running` and shows up in the poll payload's `warnings` (#322;
  frontend `step-errors.test.ts` / `run-page-step-errors.test.tsx`).
  `test_otel_flag.py`: the opt-in `ADK_OTEL_TO_CLOUD` flag parser and its (static)
  wiring into `async_app.py`'s `get_fast_api_app(otel_to_cloud=...)`.
- **Safety** — `test_safety_plugins.py`: `build_safety_plugins` returns `[]` unless
  `MODEL_ARMOR_TEMPLATE` is set (response-template + fail-closed overrides), the plugin
  screens only root-agent turns (sub-agent callbacks short-circuit), and all three
  agents expose an `App` whose `plugins` list the runner + ADK's canned loader use.
- **Experiments harnesses** — `test_creative_latency_poll.py`, `test_experiment_*.py`,
  `test_quota_spread_*.py`, `test_person_calibration.py`, `test_swap_compare.py`: the pure/offline core of the `experiments/` measurement
  harnesses (event-log parsing, N-trial aggregation, 429/503 log-filter building, figure
  rendering, concurrent-batch record shaping, contention-slope analysis, quality harvest,
  the Agent Platform Experiments uploader, and the person-reference calibration grid,
  block-reason reading and go/no-go summary, and the person-swap edit-vs-regenerate
  comparison). No creds, no network — the live network
  drivers are integration-only. See [../experiments/README.md](../experiments/README.md).
- **Evals** (`eval/`) — end-to-end `adk eval` cases with rubric-based LLM-as-judge scoring
  (response quality + tool-use quality). One evalset + rubric config per agent. Runs
  against real APIs. `eval/efficiency_gate.py` gates the nightly `adk-eval.yml` CI run on
  case status (a deterministic root-agent tool-use check over the trace + every non-tool-use
  LLM metric; the LLM tool-use rubric is advisory) + token/call-count regressions vs
  `docs/baselines/eval_efficiency.json`
  (unit-tested offline by `test_eval_efficiency_gate.py`; see
  [deployment/README.md → Eval CI (WIF)](../deployment/README.md#eval-ci-wif)).
