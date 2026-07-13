# `agents/` — api_server serving view (do not "tidy")

This directory exists **only** so the Cloud Run backend can run
`adk api_server agents` instead of `adk api_server .`.

ADK's `AgentLoader.list_agents()` returns *every* non-hidden subdirectory of the
agents dir — not just the ones with a runnable root agent. Pointed at the repo root
(`.`), `GET /list-apps` therefore listed `docs/`, `outputs/`, `deployment/`,
`agent_common/`, `creative_eval/`, `tests/`, … as "apps". Pointing it at this
directory — which holds one **relative symlink per runnable agent** — makes
`/list-apps` return exactly the three we serve:

- `trend_scout` → `../trend_scout`
- `creative_agent` → `../creative_agent`
- `interactive_creative` → `../interactive_creative`

The real packages stay **flat at the repo root** (the layout Agent Engine's
`extra_packages` staging depends on — see `CLAUDE.md`). These are just a view.

Two things make it work, both required — see `tests/test_agents_dir.py`:

1. **Relative** symlinks, so they resolve inside the container image
   (`/app/agents/trend_scout → /app/trend_scout`).
2. `PYTHONPATH=/app` (set in the root `Dockerfile`), so each agent's cross-package
   imports (`from creative_eval …`, `from agent_common …`) still resolve — the loader
   only puts `agents/` on `sys.path`, not the repo root.

Do not add an `__init__.py`, `agent.py`, or `root_agent.yaml` here — a marker file
would flip ADK into single-agent mode and break multi-agent serving.
