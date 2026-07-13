# Rename the Phase-1 Agent: `trend_trawler` → `trend_scout`

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this task-by-task.
> On execution, first copy this plan to `docs/plans/2026-07-13-rename-trend-scout.md`.
> Branch `refactor/rename-trend-scout` already exists (off fresh `main`, PR #51 merged).

## Context

The product is **"Trend Trawler"**, but the Phase-1 agent package is *also* `trend_trawler/`, so the
name collides with the system. We're renaming just the **agent** to `trend_scout` (decided with the
user) to remove the ambiguity, while keeping the product — and the whole data layer — named after
"Trend Trawler".

**Critical constraint (why this is surgical, not a `sed`):** `trend_trawler` is overloaded. It is
both the agent package/identifier **and** the live **BigQuery dataset name** (`BQ_DATASET_ID`, SQL
`FROM …trend_trawler.…`, `"bq_dataset"` payloads). Renaming the dataset would break every deployed
fan-out run. So the rename must distinguish *agent identifier* from *data store*.

**Approved scope — "+ agentic_wf tag":** rename everything agent-scoped **plus** the `agentic_wf`
provenance value. Leave the BigQuery dataset + columns + service + product banner.

**Flat layout stays** — `git mv trend_trawler trend_scout` keeps the package at the repo root (Agent
Engine bundles by relative path; see the note added to README/CLAUDE.md in PR #51).

## Scope

| RENAME → `trend_scout` | LEAVE (system / product / data) |
| --- | --- |
| `trend_trawler/` dir → `trend_scout/` + all `from trend_trawler…` imports | BQ dataset `trend_trawler` (`BQ_DATASET_ID`, SQL `FROM`, `"bq_dataset"` payloads + their tests) |
| Module symbol `trend_trawler = Agent(name="trend_trawler")` + `root_agent` (`trend_trawler/agent.py:170,172,242`) | `trawler_gcs` / `trawler_date` BQ columns, `trawler_output` |
| `AGENT_EXTRA_PACKAGES` + `AGENT_DEPLOY_SPECS` keys/module/`env_prefix`(`TRAWLER`→`SCOUT`)/`display_name`(`trend-scout-agent`)/`gcs_subdir`(`scout`) (`deploy_agent.py:59,64,77-82,263`) | `creative-trawler-crf` service + `agent-workflow=trend-trawler` labels |
| Env var `TRAWLER_AGENT_ENGINE_ID` → `SCOUT_AGENT_ENGINE_ID` (`.env.example`, `test_deployment.py:160`, `integration_test.py:5,52`) | `cloud_functions/trawler_scheduler/` (system scheduler stub) |
| Frontend selector value **and label**, `Agent` type union, `/apps/trend_trawler/` routing + `appName` defaults/conditionals (`types.ts:47`, `page.tsx:40,105,157`, `run/…/page.tsx`, `results/…/page.tsx`, FE tests) | `trend_trawler_banner.png` + "Trend Trawler" product brand text |
| `agentic_wf: "trend_trawler"` → `"trend_scout"` (7× `creative_agent/agent.py`, 1× `interactive_creative/agent.py`) | BQ dataset prose that names the *store* (not the agent) |
| Evalset file `tests/eval/evalsets/trend_trawler_evalset.json` → `trend_scout_evalset.json` (+ any app-name inside) | |
| Docs: agent references in `README.md`, `CLAUDE.md`, `deployment/README.md`, `docs/notes/*`, `docs/baselines/main.md`, `docs/architecture/README.md`, `frontend/README.md`, `deploy-to-agent-engine.ipynb`, `cloud_functions/creative_fanout/main.py:17` docstring | |

## Tasks (one commit each; keep the suite green after each)

**Task 1 — Python + package + deploy wiring (pytest must stay green):**
`git mv trend_trawler trend_scout`; rename the module symbol + ADK `name=` + `root_agent`; update every
`from trend_trawler…`/`import trend_trawler` (tests: `test_retry_config`, `test_pipeline_structure`,
`test_tools`, `test_tools_retry`, `test_callbacks`, `test_config`); update `deploy_agent.py`
(`AGENT_EXTRA_PACKAGES`, `AGENT_DEPLOY_SPECS`, `env_prefix`→`SCOUT`, `display_name`, `gcs_subdir`,
comments); `test_deployment.py` + `integration_test.py` (agent-key + env var); flip the `agentic_wf`
values; `.env.example` `TRAWLER_AGENT_ENGINE_ID`→`SCOUT_AGENT_ENGINE_ID`. Internal package imports
(`from .config` etc.) need no change. **Do NOT touch** `bq_dataset`/`BQ_DATASET_ID`/SQL. Commit.

**Task 2 — Frontend:** `types.ts` union; `page.tsx` default + `SelectItem` value **and** visible label
(→ "Trend Scout"); the `appName`/`app` defaults + `appName !== "trend_trawler"` conditionals in
`run/[sessionId]/page.tsx` + `results/[sessionId]/page.tsx`; FE tests (`form-validation`, `api-client`
incl. the `/apps/trend_trawler/…` URL). `cd frontend && npm test` green. Commit.

**Task 3 — Evals:** `git mv` the evalset to `trend_scout_evalset.json`; update any `app_name`/agent
field inside it; confirm `tests/eval/*.json` configs need no change (none reference it today). Commit.

**Task 4 — Docs:** update agent references in the docs listed in the Scope table (Phase-1 bullet,
"choose `trend_scout`", repo-tree dir, `adk eval trend_scout …` command, notebook). **Leave** dataset
prose, `bq_dataset`, and the product banner. Commit.

## Verification

**No-creds gate:**
```bash
export PATH="$HOME/.local/bin:$PATH"
uv run pytest tests/ -q
uvx ruff check . && uvx ruff format --check .
uv run python -c "import trend_scout.agent as a; print(a.root_agent.name)"   # -> trend_scout
cd frontend && npm test && cd ..
# agent identifier fully gone:
grep -rn "trend_trawler" --include='*.py' --include='*.ts' --include='*.tsx' --include='*.json' . \
  | grep -vE '\.venv|/outputs/|/node_modules/|docs/plans/2026-07' \
  | grep -vE 'bq_dataset|BQ_DATASET_ID|\.trend_trawler\.|dataset|banner'   # -> expect no hits
# data store deliberately preserved:
grep -rn '"bq_dataset": "trend_trawler"\|BQ_DATASET_ID' --include='*.py' . | head   # -> still present
```
Then `grep '^#' README.md` / skim to confirm docs read `trend_scout` for the agent and `trend_trawler`
only for the dataset/product.

**With-creds (optional, proves the renamed source deploys):**
- `python deployment/deploy_agent.py --version=v1 --agent=trend_scout --create` → writes
  `SCOUT_AGENT_ENGINE_ID` to `.env`.
- `python deployment/test_deployment.py --agent=trend_scout --user_id=$USER_ID` (inserts a BQ row).
- Delete the old `trend-trawler-agent` engine when satisfied.

## Sequencing
Tasks 1→2→3→4, one commit each. Single PR "refactor: rename Phase-1 agent trend_trawler → trend_scout"
— opened only when the user asks.
