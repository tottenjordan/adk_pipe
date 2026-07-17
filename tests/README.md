# Tests

The Python test suite for Trend Trawler. Covers Pydantic schema validation, agent
pipeline structure, tool functions, callbacks, deployment utilities, Cloud Run Function
logic, ADK end-to-end evals, and the offline unit tests for the `experiments/`
measurement harnesses (see [../experiments/README.md](../experiments/README.md)).

```bash
# Python tests (pytest) — requires GCP credentials (module-level genai.Client)
uv run pytest tests/ -v

# ADK evals — end-to-end LLM-as-judge (real API calls, ~5 min per case)
uv run adk eval trend_scout tests/eval/evalsets/trend_scout_evalset.json \
  --config_file_path=tests/eval/eval_config.json --print_detailed_results

# creative_agent eval — needs PYTHONPATH + its own rubric config
PYTHONPATH="$PWD" uv run adk eval creative_agent tests/eval/evalsets/creative_agent_evalset.json \
  --config_file_path=tests/eval/creative_eval_config.json --print_detailed_results
```

See [CLAUDE.md](../CLAUDE.md) for the full testing notes (eval invocation gotchas,
per-agent rubric configs, integration tests).

## Structure

```bash
tests/
├── __init__.py
├── eval/                            # ADK evals — rubric-based LLM-as-judge (real APIs)
│   ├── eval_config.json             # trend_scout rubric config
│   ├── creative_eval_config.json    # creative_agent rubric config
│   └── evalsets/
│       ├── trend_scout_evalset.json
│       └── creative_agent_evalset.json
├── test_agent_common_models.py      # shared model location + build_gemini() factory
├── test_agents_dir.py               # agents/ serving-view symlinks used by the Cloud Run api_server
├── test_async_runs.py               # async-job run model: kick-off/poll/resume, terminal markers
├── test_backend_entrypoint.py       # backend container entrypoint (uvicorn serves async_app.py)
├── test_callbacks.py                # citation replacement, state init, rate limiting
├── test_conditional_agent.py        # RunIfAgent — conditional-block control-flow wrapper
├── test_config.py                   # per-agent config resolution (incl. campaign-placement resolver)
├── test_creative_eval.py            # creative_eval schemas, scoring logic, config
├── test_crf_entrypoint.py           # crf_entrypoint orchestrator (issue #46)
├── test_crf_logic.py                # Cloud Run Function logic (orchestrator + worker)
├── test_crf_worker_async.py         # async worker path of the CRF (issue #45)
├── test_deploy_utils.py             # deploy_agent.py utils (env file, extra_packages)
├── test_image_reference.py          # generate_image multimodal contents + valid ImageConfig
├── test_observability.py            # shared agent_common observability callbacks
├── test_pipeline_structure.py       # agent pipeline composition + placement-env wiring
├── test_retry_agent.py              # RetryUntilKeyAgent (retry-on-empty producer wrapper)
├── test_retry_config.py             # scoped RetryConfig constants on infra agents
├── test_sanitize.py                 # lone-surrogate scrubber (agent_common.sanitize)
├── test_schemas.py                  # Pydantic schemas in the creative_agent pipeline
├── test_tools.py                    # backend tool functions (pure logic, no I/O)
├── test_tools_retry.py              # infra tools propagate (don't swallow) exceptions
├── test_trend_scout_logging.py      # trend_scout debugging-observability callbacks
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
  `test_retry_config.py`: agent composition, state init, rate limiting, citation regex,
  scoped `RetryConfig`.
- **Tools** — `test_tools.py`, `test_tools_retry.py`: pure tool logic, plus the contract
  that infra tools raise (rather than swallow errors into status dicts) so ADK retry works.
- **Deployment & fan-out** — `test_deploy_utils.py`, `test_crf_entrypoint.py`,
  `test_crf_logic.py`, `test_crf_worker_async.py`: deploy mappings/env wiring and the
  orchestrator + worker Cloud Run Function paths.
- **Async-job run model** — `test_async_runs.py`: detached kick-off returns immediately,
  `_drive_run` appends a `done`/`error` terminal marker, poll derives status + slices
  events by cursor, and resume re-runs with a `functionResponse` (resetting status to
  `running` first so multi-checkpoint interactive runs don't stop early).
- **Experiments harnesses** — `test_creative_latency_poll.py`, `test_experiment_*.py`,
  `test_quota_spread_*.py`: the pure/offline core of the `experiments/` measurement
  harnesses (event-log parsing, N-trial aggregation, 429/503 log-filter building, figure
  rendering, concurrent-batch record shaping, contention-slope analysis, quality harvest,
  and the Agent Platform Experiments uploader). No creds, no network — the live network
  drivers are integration-only. See [../experiments/README.md](../experiments/README.md).
- **Evals** (`eval/`) — end-to-end `adk eval` cases with rubric-based LLM-as-judge scoring
  (response quality + tool-use quality). One evalset + rubric config per agent. Runs
  against real APIs.
