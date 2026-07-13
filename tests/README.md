# Tests

The Python test suite for Trend Trawler. Covers Pydantic schema validation, agent
pipeline structure, tool functions, callbacks, deployment utilities, Cloud Run Function
logic, and ADK end-to-end evals.

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
├── test_callbacks.py                # citation replacement, state init, rate limiting
├── test_config.py                   # per-agent config resolution
├── test_creative_eval.py            # creative_eval schemas, scoring logic, config
├── test_crf_entrypoint.py           # crf_entrypoint orchestrator (issue #46)
├── test_crf_logic.py                # Cloud Run Function logic (orchestrator + worker)
├── test_crf_worker_async.py         # async worker path of the CRF (issue #45)
├── test_deploy_utils.py             # deploy_agent.py utils (env file, extra_packages)
├── test_pipeline_structure.py       # agent pipeline composition + configuration
├── test_retry_config.py             # scoped RetryConfig constants on infra agents
├── test_schemas.py                  # Pydantic schemas in the creative_agent pipeline
├── test_tools.py                    # backend tool functions (pure logic, no I/O)
└── test_tools_retry.py              # infra tools propagate (don't swallow) exceptions
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
- **Evals** (`eval/`) — end-to-end `adk eval` cases with rubric-based LLM-as-judge scoring
  (response quality + tool-use quality). One evalset + rubric config per agent. Runs
  against real APIs.
