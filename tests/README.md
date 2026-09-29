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
`subprocess` marker, and `filterwarnings` scoped to known third-party noise only.

See [CLAUDE.md](../CLAUDE.md) for the full testing notes (eval invocation gotchas,
per-agent rubric configs, integration tests).

## Structure

```bash
tests/
├── __init__.py
├── _fakes.py                        # shared test doubles: fake producers + stub/recording LLMs (retry-node + graph-Workflow tests), FakeToolContext/FakeState, FakeStorageClient, noop_async
├── conftest.py                      # shared fixtures: gcp_project_env (dummy GOOGLE_CLOUD_PROJECT), fresh_config (fresh package import, restored after)
├── eval/                            # ADK evals — rubric-based LLM-as-judge (real APIs)
│   ├── eval_config.json             # trend_scout rubric config
│   ├── creative_eval_config.json    # creative_agent rubric config
│   └── evalsets/
│       ├── trend_scout_evalset.json
│       └── creative_agent_evalset.json
├── test_agent_common_clients.py     # shared lazy GCS/BigQuery client getters
├── test_agent_common_idempotency.py # stable_row_id deterministic BigQuery row keys
├── test_agent_common_models.py      # shared model location + build_gemini() factory
├── test_agent_common_state.py       # shared memorize tool + seed_initial_state()
├── test_agents_dir.py               # agents/ serving-view symlinks used by the Cloud Run api_server
├── test_async_runs.py               # async-job run model: kick-off/poll/resume, terminal markers
├── test_authz.py                    # P3 per-user authz: modes, userId normalization, proxy ID-token check, middleware 401/403/404, ownership → 404
├── test_backend_entrypoint.py       # backend container entrypoint (uvicorn serves async_app.py)
├── test_callbacks.py                # citation replacement, state init, rate limiting
├── test_config.py                   # per-agent config resolution (incl. campaign-placement resolver)
├── test_create_session_engine.py    # create_session_engine.py (reuse-or-create sessions-only engine)
├── test_creative_agent_graph.py     # creative_agent graph pipelines end-to-end (stub models): routing, citations, no-stall
├── test_creative_eval.py            # creative_eval schemas, scoring logic, config
├── test_crf_config.py               # env-driven CRF config (required project, no hardcoding)
├── test_crf_entrypoint.py           # crf_entrypoint orchestrator (issue #46)
├── test_crf_logic.py                # Cloud Run Function logic (orchestrator + worker)
├── test_crf_sql_params.py           # CRF SQL safety: allow-listed identifiers, parameterized values
├── test_crf_worker_async.py         # async worker path of the CRF (issue #45)
├── test_deploy_utils.py             # deploy_agent.py utils (env file, extra_packages, runtimes.create)
├── test_export_concurrency.py       # creative_agent export tools: per-run scratch isolation (issue #104)
├── test_image_reference.py          # generate_image multimodal contents + valid ImageConfig
├── test_interactive_resume_graph.py # real interactive_creative App via start_run/start_resume: checkpoint pause → NodeTool resume, fail-once retry counts, one BQ key
├── test_no_legacy_agent_engines_api.py # guard: no legacy vertexai agent_engines API in repo call sites
├── test_observability.py            # shared agent_common observability callbacks
├── test_otel_flag.py                # ADK_OTEL_TO_CLOUD parsing + async_app wiring (opt-in Cloud Trace on the api)
├── test_pipeline_structure.py       # pipeline composition (graph nodes/edges by name, truthy terminals) + placement-env wiring
├── test_public_api.py               # creative_agent public facade (curated __all__ reuse surface) + guards: no legacy SequentialAgent/ParallelAgent/LoopAgent
├── test_retry_node.py               # RetryUntilKeyNode (retry-on-empty graph wrapper; is_populated; NodeTool no-stall)
├── test_retry_config.py             # scoped RetryConfig constants on infra agents
├── test_sanitize.py                 # lone-surrogate scrubber (agent_common.sanitize)
├── test_schemas.py                  # Pydantic schemas in the creative_agent pipeline
├── test_sdk_versions.py             # guard: aiplatform 2.x ships both agentplatform + vertexai surfaces
├── test_tools.py                    # backend tool functions (pure logic, no I/O)
├── test_tools_retry.py              # infra tools propagate (don't swallow) exceptions
├── test_trend_scout_graph.py        # trend_scout understand_trends graph run end-to-end (stub models)
├── test_trend_scout_concurrency.py  # trend_scout GCS-export tools: per-run scratch isolation
├── test_trend_scout_logging.py      # trend_scout wiring of the shared observability callbacks
├── test_visual_intent_prompts.py    # optional visual-intent {key?} tokens + IMAGE_PROMPT_GUIDE no-braces
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

- **Schemas & config** — `test_schemas.py`, `test_creative_eval.py`, `test_config.py`,
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
- **Prompts & facade** — `test_visual_intent_prompts.py`, `test_public_api.py`: optional
  visual-intent state tokens, and `creative_agent`'s curated public reuse surface.
- **Async-job run model** — `test_async_runs.py`: detached kick-off returns immediately,
  `_drive_run` appends a `done`/`error` terminal marker, poll derives status + slices
  events by cursor, and resume re-runs with a `functionResponse` (resetting status to
  `running` first so multi-checkpoint interactive runs don't stop early).
  `test_otel_flag.py`: the opt-in `ADK_OTEL_TO_CLOUD` flag parser and its (static)
  wiring into `async_app.py`'s `get_fast_api_app(otel_to_cloud=...)`.
- **Experiments harnesses** — `test_creative_latency_poll.py`, `test_experiment_*.py`,
  `test_quota_spread_*.py`: the pure/offline core of the `experiments/` measurement
  harnesses (event-log parsing, N-trial aggregation, 429/503 log-filter building, figure
  rendering, concurrent-batch record shaping, contention-slope analysis, quality harvest,
  and the Agent Platform Experiments uploader). No creds, no network — the live network
  drivers are integration-only. See [../experiments/README.md](../experiments/README.md).
- **Evals** (`eval/`) — end-to-end `adk eval` cases with rubric-based LLM-as-judge scoring
  (response quality + tool-use quality). One evalset + rubric config per agent. Runs
  against real APIs.
