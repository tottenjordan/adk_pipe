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

Pytest config lives in `pyproject.toml` `[tool.pytest.ini_options]`: `testpaths`,
`pythonpath = ["."]` (so tests import the flat packages without `sys.path` hacks), the
`subprocess` and `slow` markers (`slow` = notebook-parity smoke, scenario-preview
goldens, `test_build_image.py`), and `filterwarnings` scoped to known third-party noise only.

See [CLAUDE.md](../CLAUDE.md) for the full testing notes (eval invocation gotchas,
per-agent rubric configs, integration tests).

## Structure

```bash
tests/
├── __init__.py
├── _bandit_sizes.py                 # standard bandit episode sizes (HORIZON_S / HORIZON_M / BATCH) so tests reuse compiled XLA programs
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
├── test_agent_common_clients.py     # shared lazy GCS/BigQuery client getters
├── test_agent_common_idempotency.py # stable_row_id deterministic BigQuery row keys
├── test_agent_common_models.py      # shared model location + build_gemini() factory
├── test_agent_common_state.py       # shared memorize tool + seed_initial_state()
├── test_ad_copy_prompts.py          # ad copy prompts: angle spread + typicality (drafter), angle coverage/surprise/brief checklist/CTA (critic), flagged-only reviser tokens; brace-safety
├── test_agents_dir.py               # agents/ serving-view symlinks used by the Cloud Run api_server
├── test_async_runs.py               # async-job run model: kick-off/poll/resume, terminal markers
├── test_authz.py                    # P3 per-user authz: modes, userId normalization, proxy ID-token check, middleware 401/403/404, ownership → 404
├── test_backend_entrypoint.py       # backend container entrypoint (uvicorn serves async_app.py)
├── test_bandit_endpoint_lib.py      # deployment/bandit/endpoint.py vs a fake aiplatform (single-worker env, 1 replica, labels, find_* by label oldest-first)
├── test_create_bq_tables.py         # create_bq_tables.sh with a stub bq: bandit_* + creative_ratings schemas (ratings vs RATING_COLUMN_TYPES), partitioning/clustering, idempotency
├── test_bandit_*.py                 # JAX bandit core (bandit/): features, config, linear TS, baselines, environment, simulate+metrics+aggregate, notebook-parity smoke, scripted shifts
├── test_brand_history.py            # creative_agent/brand_history.py: parameterised creative_evals SELECT (no string-built brand), report reads limited to the configured bucket + size cap, aggregation (recent/strongest styles, recurring weak dimensions, runs with failed non-advisory gates), brace-free ≤120-word note, BQ/GCS/timeout fail-open, disabled knob → no query, shortlist re-draw unless a style preference is set, canonical style mapping (schema example phrasings), allowlisted tones/dimensions/gates only (injection strings never reach the note), GCS per-call timeouts + report-read deadline, failed-check denominator = readable reports
├── test_brief_check.py              # deterministic creative-brief check (proposition incl. abbreviations/capital-led sentence breaks/brand-product-trend names/and-compounds, X-but-Y insight + ;/dash/contrast markers, cited RTBs + normalised src-N/brief ids vs sources, fit_mode, angle names/tensions, motifs, assets)
├── test_brief_render.py             # creative brief → "## Creative Brief" markdown in the research PDF (real markdown_pdf TOC check) + compact (headless) prompt variant + gallery summary card (HTML-escaped)
├── test_callbacks.py                # citation replacement, state init (incl. style_shortlist seeding, reference_images/reference_roles), rate limiting, trend/product guard callback
├── test_concept_guard.py            # final image prompts always name the trend_motif + product + brand_cue (pure guard; token-overlap matching incl. brand_cue, intangible-product cue); concept_issues (quoted text vs paired headline/CTA incl. curly quotes / punctuation / substring, meme+comic exemption, empty motif, set-level text cap + centred-hero selection, conservative heuristics) + restore_unflagged_concepts
├── test_config.py                   # per-agent config resolution (incl. campaign-placement resolver)
├── test_text_match.py               # shared conservative matching (Unicode folding, plurals, sizes/packaging head nouns, single-token full phrase, brand anchor, 60% token-overlap mentions)
├── test_copy_gate.py                # deterministic ad-copy gate (product named, CTA words, headline/caption length, brief avoid terms minus product/mandatory/trend overlaps; Unicode/&-brand/plural matching audit cases; warning-only structural issues; only proposition/mandatories self-reports gate, only deterministic residuals recorded; lenient product matching; duplicate-id keys; tolerant parsing) + restore_unflagged safety net
├── test_create_session_engine.py    # create_session_engine.py (reuse-or-create sessions-only engine)
├── test_creative_brief_prompts.py   # {creative_brief_md?} block before the report + shared contract rule (core + fallback; user feedback/art direction override) in the 5 creative prompts; brace-safety
├── test_creative_agent_graph.py     # creative_agent graph pipelines end-to-end (stub models): routing, citations, no-stall, guard-repaired render prompts, creative-brief gate (pass / revise once / revise twice with 2 rounds / residual issues / writer exhausted / raising writer or reviser fail-soft), ad-copy gate (all pass / flagged copy revised + unflagged edit reverted / raising reviser fail-soft / residual issues), concept gate (all pass / flagged concept fixed + unflagged edit reverted + brand cue re-guarded / raising fixer fail-soft / residual issues), research PDF node (artifact + GCS / skipped without report / failure recorded), finalize_pipeline (evaluate + persist happy path with fake judge/GCS/BQ, no creatives → notice, failing gallery doesn't block BQ, eval row written last), creative_pipeline (the root's single call: research → ads → visuals + render → finalize end to end, then the root answers)
├── test_eval_efficiency_gate.py     # efficiency gate: metric extraction, tolerances, warn-only latency, CLI exit codes, --update-baseline
├── test_creative_eval.py            # creative_eval schemas, scoring logic, config
├── test_eval_gates.py               # creative_eval binary gates: schema defaults (old reports parse), passed = score AND gates, gates_pass_rate over judged creatives only, brief block (brace-safe), judge-only response schemas (gates required, no code-set fields), gate normalisation (zero gates reported → fails, partial omission passes as "not checked" + report warning, lenient names, no brief → "no brief"), presence/violation gate wording, advisory brand cue, rendered image as a gs:// Part + image-QA hint, unreadable-image fail-soft to prompt-only limited to image 4xx, finalize "failed checks" / "evaluation failed" / gates pass-rate line
├── test_crf_config.py               # env-driven CRF config (required project, no hardcoding)
├── test_crf_entrypoint.py           # crf_entrypoint orchestrator (issue #46)
├── test_crf_logic.py                # Cloud Run Function logic (orchestrator + worker)
├── test_crf_sql_params.py           # CRF SQL safety: allow-listed identifiers, parameterized values
├── test_crf_worker_async.py         # async worker path of the CRF (issue #45)
├── test_deploy_utils.py             # deploy_agent.py utils (env file, extra_packages, runtimes.create) + integration_test.py (skips, creative_agent smoke output assertion)
├── test_experiments_api.py          # /experiments routes: create→ready, 400/404/409, traffic, stop, TTL reaper, reconcile, authz, snapshot_arms, §9 scenarioOverrides validation + bandit parity, deploy lease (one deployer, expiry, heartbeat, release)
├── test_experiments_backends.py     # VertexDeployer (stepwise/resume, labelled-resource reuse, teardown of extras) + CloudRunJobsRunner env overrides, fakes
├── test_experiments_metrics.py      # pure ExperimentMetrics aggregation (CI bands, totals, arm share, segments)
├── test_experiments_series.py       # pure §8 /creatives aggregation (windows, share, segments, missedClicks, engagedSecondsPer1k)
├── test_experiments_shifts.py       # §10 in the api: shift validation + bandit parity, numbered traffic runs (runs/{n}.json, env overrides, trafficRuns), ?run= reads incl. legacy NULL rows, shift_response + regime aggregation, regime SQL, unmigrated-table fallbacks
├── test_experiments_store.py        # bandit_experiments MERGE/SELECT builders, typed params, §8 series SQL builders, both stores, deploy-lease UPDATEs, unknown-column tolerance
├── test_ratings_api.py              # /ratings routes: PUT validation (400 reasons, unknown creative_key), upsert idempotency (created_at kept), GET listing, foreign/unknown session 404 (incl. ownership ValueError), judge fields from state or the GCS report (cached, fail soft; conservative headline/id matching), store 502s, calibration endpoint, enforce-mode 401/403
├── test_ratings_store.py            # creative_ratings MERGE/SELECT builders (fully parameterised), BigQuery store over the fake client, in-memory store, RATINGS_STORE selection + fallback
├── test_calibration.py              # judge-human calibration maths: Cohen's kappa (textbook value, degenerate single-class reasons), tie-averaged Spearman, per-kind slices, CSV string coercion, scripts/eval_calibration.py over a CSV
├── test_export_concurrency.py       # creative_agent export tools: per-run scratch isolation (issue #104)
├── test_image_prompt_guide.py       # IMAGE_PROMPT_GUIDE rules: text cap, descriptors not templates, Educational mapping, trend motif, trend_motif schema field, REFERENCE_IMAGES section (role-only references, ignore-text left to the tool, style ref vs family diversity), Subject+Action+Location+Composition+Style blocks, typography, no unrequested logos
├── test_image_reference.py          # generate_image multimodal contents + valid ImageConfig; multiple reference images (resolve_references legacy fold-in/dedupe/cap/invalid roles, ordered parts, one failed fetch skips only that ref + unavailable-role line, numbered role block + ignore-text line, SSRF/size/content-type fetch hardening) (image QA off via the `image_qa_off` conftest fixture)
├── test_image_qa.py                 # post-render image QA: expected_text, the qa_failures matrix (brand cue advisory, unrequested logo fails, expected_text authoritative for text checks, empty motif/product never fail), the conservative instruction wording (image prompt capped, partial product view, gibberish/logo allowlist, meme slang), correction_text (quote-stripped, capped, "no new text"), inspect_image request (image Part, schema, temperature 0), IMAGE_QA_* config + ENV_VAR_DICT; generate_image flow (fail → re-render → pass keeps the second, both fail → fewer-failures attempt + image_qa__issues, ties → latest, critical weighting + never keep a new unsafe, per-image + per-run budget, fail-open → image_qa__unavailable (not surfaced), render off the event loop, disabled = one render) and the gallery "Image check" line
├── test_interactive_resume_graph.py # real interactive_creative App via start_run/start_resume: checkpoint pause → NodeTool resume, fail-once retry counts, one BQ key (write_trends_to_bq added to the root's tools for the test), the resumed root's history keeps the checkpoint response but no sub-agent turns
├── test_no_legacy_agent_engines_api.py # guard: no legacy vertexai agent_engines API in repo call sites
├── test_observability.py            # shared agent_common observability callbacks
├── test_otel_flag.py                # ADK_OTEL_TO_CLOUD parsing + async_app wiring (opt-in Cloud Trace on the api)
├── test_pipeline_structure.py       # pipeline composition (graph nodes/edges by name, truthy terminals) + placement-env wiring
├── test_public_api.py               # creative_agent public facade (curated __all__ reuse surface) + guards: no legacy SequentialAgent/ParallelAgent/LoopAgent
├── test_retry_node.py               # RetryUntilKeyNode (retry-on-empty graph wrapper; is_populated; NodeTool no-stall)
├── test_root_history.py            # agent_common.history: ADK replays earlier pipelines' sub-agent turns + node inputs into a root's request (measured on the real creative root), the trim keeps only user/root turns + call/response pairs, precise matching, both roots wired
├── test_finalize.py                 # creative_agent/finalize.py nodes: summary formatting, judge off-loop on a snapshot, per-step isolation + ordering in persist_node, skipped eval writes
├── test_fail_soft_node.py           # FailSoftNode (optional-step exception → on_error state delta; successors still run; DynamicNodeFailError unwrap)
├── test_retry_config.py             # scoped RetryConfig constants on infra agents
├── test_safety_plugins.py           # opt-in Model Armor (agent_common.safety): env parsing, root-only scoping, every agent's App + canned-loader wiring
├── test_sanitize.py                 # lone-surrogate scrubber (agent_common.sanitize)
├── test_schemas.py                  # Pydantic schemas in the creative_agent pipeline
├── test_sdk_versions.py             # guard: aiplatform 2.x ships both agentplatform + vertexai surfaces
├── test_style_shortlist.py          # per-session stratified style shortlist (families match the guide palette; exclusion of recently used styles keeps the 2/3/1 strata)
├── test_tools.py                    # backend tool functions (pure logic, no I/O)
├── test_tools_retry.py              # infra tools propagate (don't swallow) exceptions
├── test_trend_scout_graph.py        # trend_scout understand_trends graph run end-to-end (stub models)
├── test_trend_scout_concurrency.py  # trend_scout GCS-export tools: per-run scratch isolation
├── test_trend_scout_logging.py      # trend_scout wiring of the shared observability callbacks
├── test_visual_concept_prompts.py   # visual concept prompts: shared brand_cue / copy-quoted in-image text / brief avoid + fit_mode rules (drafter, critic, finalizer), finalizer angle_id, interactive reviser keeps brand_cue; brace-safety
├── test_visual_intent_prompts.py    # optional visual-intent {key?} tokens + IMAGE_PROMPT_GUIDE no-braces + {style_shortlist?}/composition/text-cap rules
├── test_workflow_api_contract.py    # offline pins on the upstream ADK graph-Workflow behaviours the P2 migration relies on
│                                    #
│                                    # experiments/ harness unit tests (pure/offline — no creds, no network)
├── test_creative_latency_poll.py    # poll_to_terminal retries a transient slow/failed poll
├── test_experiment_parse.py         # creative_latency event-log parser
├── test_experiment_aggregate.py     # creative_latency N-trial aggregation math
├── test_experiment_logs.py          # Cloud Logging 429/503 filter builder
├── test_experiment_plot.py          # Plotly report builder smoke (no Chrome)
├── test_experiment_render_static.py # matplotlib static-figure renderer
├── test_quota_spread_batch.py       # quota-spread concurrent batch harness (pure core)
├── test_quota_spread_analyze.py     # quota-spread slope + tidy CSV + plots + quality harvest
└── test_quota_spread_upload.py      # Agent Platform Experiments uploader record shaping
```

## Categories

- **Schemas & config** — `test_schemas.py`, `test_creative_eval.py`, `test_eval_gates.py`, `test_config.py`,
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
- **Deployment & fan-out** — `test_deploy_utils.py`, `test_create_session_engine.py`,
  `test_crf_entrypoint.py`, `test_crf_logic.py`, `test_crf_worker_async.py`,
  `test_crf_config.py`, `test_crf_sql_params.py`: deploy mappings/env wiring, the
  `runtimes.create` path, and the orchestrator + worker Cloud Run Function paths
  (env-driven config, SQL identifier/parameter safety).
- **SDK guards** — `test_sdk_versions.py`, `test_no_legacy_agent_engines_api.py`: the
  aiplatform 2.x `agentplatform` + `vertexai` surfaces are present, and no repo call site
  uses the deprecated `agent_engines` API.
- **Concurrency** — `test_export_concurrency.py`, `test_trend_scout_concurrency.py`:
  in-process concurrent runs get isolated scratch dirs (issue #104).
- **Prompts & facade** — `test_visual_intent_prompts.py`, `test_visual_concept_prompts.py`,
  `test_public_api.py`: optional visual-intent state tokens, the visual concept brand-cue /
  copy-quoted-text / brief rules, and `creative_agent`'s curated public reuse surface.
- **Image diversity** — `test_image_prompt_guide.py`, `test_style_shortlist.py`,
  `test_concept_guard.py`: the guide's text cap / descriptor palette / Educational mapping,
  the per-session style shortlist, and the trend-motif + product prompt guard.
- **Brand history** — `test_brand_history.py` (helper + node delta),
  `test_creative_agent_graph.py` (the note reaches the brief writer; disabled → no query;
  a raising step doesn't stop research), `test_pipeline_structure.py` (node in the START
  fan-out). `conftest.py` points the module's BigQuery and GCS getters at a raiser so no
  test reads the live table/bucket the repo `.env` names.
- **Async-job run model** — `test_async_runs.py`: detached kick-off returns immediately,
  `_drive_run` appends a `done`/`error` terminal marker, poll derives status + slices
  events by cursor, and resume re-runs with a `functionResponse` (resetting status to
  `running` first so multi-checkpoint interactive runs don't stop early).
  `test_otel_flag.py`: the opt-in `ADK_OTEL_TO_CLOUD` flag parser and its (static)
  wiring into `async_app.py`'s `get_fast_api_app(otel_to_cloud=...)`.
- **Safety** — `test_safety_plugins.py`: `build_safety_plugins` returns `[]` unless
  `MODEL_ARMOR_TEMPLATE` is set (response-template + fail-closed overrides), the plugin
  screens only root-agent turns (sub-agent callbacks short-circuit), and all three
  agents expose an `App` whose `plugins` list the runner + ADK's canned loader use.
- **Experiments harnesses** — `test_creative_latency_poll.py`, `test_experiment_*.py`,
  `test_quota_spread_*.py`: the pure/offline core of the `experiments/` measurement
  harnesses (event-log parsing, N-trial aggregation, 429/503 log-filter building, figure
  rendering, concurrent-batch record shaping, contention-slope analysis, quality harvest,
  and the Agent Platform Experiments uploader). No creds, no network — the live network
  drivers are integration-only. See [../experiments/README.md](../experiments/README.md).
- **Evals** (`eval/`) — end-to-end `adk eval` cases with rubric-based LLM-as-judge scoring
  (response quality + tool-use quality). One evalset + rubric config per agent. Runs
  against real APIs. `eval/efficiency_gate.py` gates the nightly `adk-eval.yml` CI run on
  final status + token/call-count regressions vs `docs/baselines/eval_efficiency.json`
  (unit-tested offline by `test_eval_efficiency_gate.py`; see
  [deployment/README.md → Eval CI (WIF)](../deployment/README.md#eval-ci-wif)).
