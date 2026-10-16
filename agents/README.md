# `agents/` — the runnable ADK agents

This directory is ADK's `agents_dir`: `adk web agents`, `adk eval agents/<agent>` and the
api launcher `deployment/async_app.py` (`get_fast_api_app(agents_dir="agents")`) all
serve it. It holds exactly the three runnable agents, as real directories:

- `trend_scout/` — Phase 1, trend discovery and selection
- `creative_agent/` — Phase 2, the batch creative pipeline
- `interactive_creative/` — Phase 2 with human review checkpoints

ADK's `AgentLoader.list_agents()` lists every subdirectory that has an `__init__.py`, so
anything placed here becomes an "app" in `GET /list-apps`. Shared code therefore lives in
`libs/` (`agent_common`, `creative_eval`) and the bandit tooling in `bandit_stack/`.

Import names stay bare (`import creative_agent`, `from agent_common import …`): locally
the editable install's `.pth` puts `agents/` and `libs/` on `sys.path`; the api image
sets `PYTHONPATH=/app:/app/agents:/app/libs`; Agent Engine staging copies each
`AGENT_EXTRA_PACKAGES` entry flat under its folder name (`deployment/deploy_agent.py`).

Do not add an `__init__.py`, `agent.py` or `root_agent.yaml` here: a marker file would
flip ADK into single-agent mode. `tests/test_agents_dir.py` guards all of this.
