# P1: AgentPlatform SDK Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task.

**Status:** proposal (not started). **Partly blocked:** the root uv project is gated on a google-adk release (see "Blockers & gate"). The Cloud Run Function (Task 1) and the cold-start spike (Task 2) are **unblocked today**. Gate re-checked 2026-09-29: FAIL (google-adk 2.10.0 still caps aiplatform <2). Tasks 1–2 executing via [2026-09-29-p1a-crf-agentplatform-and-spike.md](2026-09-29-p1a-crf-agentplatform-and-spike.md). Task 2 spike **done** 2026-09-29: keep `extra_packages` + `staging_bucket` (see [Spike findings](#spike-findings-2026-09-29)).
**Goal:** Move every Agent Engine call site off the deprecated `vertexai.Client().agent_engines` onto `agentplatform.Client().runtimes` / `.sessions`. Lift the `google-cloud-aiplatform<2` pin and its Dependabot hold, without breaking deploys, the CRF fan-out, or the persistent-session backend.
**Architecture:** There are two dependency islands, and we migrate them separately:
1. **CRF function** (`cloud_functions/creative_fanout/`, its own `requirements.txt`, no ADK). It can switch now to the standalone `google-cloud-agentplatform` distribution.
2. **Root uv project** (deploy scripts, backend, agents). It pulls aiplatform through `google-adk[eval]`, which caps `<2`. It moves to `google-cloud-aiplatform>=2.2` only after ADK lifts that cap. That 2.x wheel bundles *both* `agentplatform` (new API) and `vertexai` (the deprecated compat API that ADK still imports).

The deployed engines are then redeployed so the pickled `AdkApp` comes from `agentplatform.frameworks`.
**Tech Stack:** Python 3.13, uv, google-adk 2.10, google-cloud-aiplatform 1.165.1 → 2.2.x, google-cloud-agentplatform 2.2.0 (CRF only), pytest, ruff, ty, Cloud Run, Cloud Run Functions, Vertex AI Agent Runtime (formerly Agent Engine).

---

## Conventions

- Branch off `main` for each PR (`feat/p1-crf-agentplatform`, `spike/p1-source-packages`, `feat/p1-root-agentplatform`). Squash-merge.
- **Commit messages must NEVER include `Co-Authored-By` trailers or any AI attribution.** PR bodies must not either (CODE_STANDARDS.md §1; user memory).
- Use `uv` for everything: `uv add` / `uv remove`, never hand-edit `pyproject.toml` deps. Root `requirements.txt` is regenerated with `uv export --format requirements-txt --no-hashes --no-dev -o requirements.txt`. CI fails on drift (#153).
- Verification loop for each task: `uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest tests/ -q`.
- Regions: the Agent Runtime client uses `GCP_REGION` (`us-central1`), never `GOOGLE_CLOUD_LOCATION=global`.
- **Never install `google-cloud-agentplatform` and `google-cloud-aiplatform` in the same environment.** Both wheels own the top-level `agentplatform/` package (verified below).

## Why / current state

- aiplatform 1.165.1 already emits `FutureWarning: The vertexai.Client class is deprecated. Please use agentplatform.Client instead.` on every client construction (verified locally).
- Pins today:
  - `pyproject.toml:14`: `google-cloud-aiplatform[agent-engines]>=1.165.1,<2.0.0`
  - `cloud_functions/creative_fanout/requirements.txt:3`: `google-cloud-aiplatform[agent_engines]==1.165.1`
  - Root `requirements.txt:139`: `google-cloud-aiplatform==1.165.1` (the Agent Engine container requirements)
- Dependabot holds aiplatform majors at `.github/dependabot.yml:18-20` (uv) and `:35-37` (pip / CRF).

**Call sites** (from `grep -rn "vertexai\|agent_engines\|reasoning_engine\|aiplatform"`, excluding `.venv`/`uv.lock`):

| File | Lines | Usage |
|---|---|---|
| `deployment/deploy_agent.py` | 17, 136-143, 147 (docstring), 221, 234, 270, 301-302 | `import vertexai`; `vertexai.Client`; `from vertexai.agent_engines import AdkApp`; `.agent_engines.create/list/get`; `remote_agent.delete(force=True)` |
| `deployment/test_deployment.py` | 18, 64-67, 157 | module-level `vertexai.Client`; `.agent_engines.get` |
| `deployment/integration_test.py` | 37, 139-142, 153, 178 | `vertexai.Client`; `.agent_engines.get` (x2) |
| `deployment/create_session_engine.py` | 30, 68, 83, 92, 99-102 | `.agent_engines.list/create` (agent-less session store) |
| `cloud_functions/creative_fanout/main.py` | 47, 100-103, 303-305 | `vertexai.Client`; `.agent_engines.get`; then `remote_agent.async_create_session / async_stream_query / async_delete_session` |
| `cloud_functions/creative_fanout/session.py` | 24 (docstring only) | uses the handle's `async_create_session` / `async_delete_session`. No SDK import. |
| `deployment/async_app.py` | 32-38 | `get_fast_api_app(session_service_uri=SESSION_SERVICE_URI)` → ADK `VertexAiSessionService` → `import vertexai` + `vertexai.Client(...).aio.agent_engines.sessions.*` inside ADK (`google/adk/sessions/vertex_ai_session_service.py:34,576-586,224,274,338`) |
| tests | `tests/test_create_session_engine.py:4,50-86`, `tests/test_crf_worker_async.py:63,124,223` | mock `client.agent_engines.*` |
| out of scope | `experiments/quota_spread/upload_to_vertex.py:155` (`google.cloud.aiplatform` Experiments, still in aiplatform 2.x); `creative_agent/image_tools.py:94`, `creative_eval/evaluate.py:46` (`genai.Client(vertexai=True)`, unrelated) | none |

## Upstream facts

### Verified 2026-09-29 (PyPI JSON, wheel inspection, throwaway venvs in `/tmp`, local `.venv`)

1. **`google-cloud-agentplatform` exists.** Current version is **2.2.0** (2026-09-23). Releases: 2.0.0, 2.0.1, 2.1.0, 2.1.3, 2.2.0. Its top-level package is only `agentplatform`, and it does **not** depend on google-cloud-aiplatform. Extras: `agent-engines` (alias `agent_engines`), `adk` (`google-adk>=1.27,<3`), and others.
2. **aiplatform 2.x exists.** Current is **2.2.0** (2026-09-23). **2.0.0 is yanked** ("Incorrect version bump. Use latest 1.x stable release."). 2.0.1, 2.1.0, 2.1.3 and 2.2.0 are live. The 2.2.0 wheel ships `agentplatform/`, `vertexai/`, `google/` and `vertex_ray/`.
3. **Client surfaces:**
   - aiplatform 2.2.0 `agentplatform.Client` has `runtimes`, `sessions`, `memory_banks`, `sandboxes`, `evals` and more. It has **no `agent_engines`**.
   - aiplatform 2.2.0 `vertexai.Client` still has `agent_engines` and emits the deprecation warning.
   - aiplatform **1.165.1 also bundles an `agentplatform` package**, but its `Client` exposes `agent_engines` only (no `runtimes`/`sessions`).
4. **New API names (2.2.0 `agentplatform/_genai/runtimes.py`, `sessions.py`):**
   - `client.runtimes.create(*, agent=None, config=AgentRuntimeConfig)`. Config keys include `requirements`, `extra_packages`, `staging_bucket`, `display_name`, `env_vars`, `min_instances`, `source_packages`, `entrypoint_module`, `entrypoint_object`, `requirements_file`, `class_methods`, `agent_framework`.
   - `client.runtimes.get(*, name)` → `types.Runtime` with `.api_resource` and the spec's registered methods (`async_stream_query`, `stream_query`, `async_create_session`, …).
   - `client.runtimes.list(*, config)` → **generator** of `Runtime`.
   - `client.runtimes.delete(*, name, force)`, plus `Runtime.delete()`.
   - `client.sessions.create(*, name, user_id, config)` / `.get` / `.list` / `.delete` / `.events`.
5. **`AdkApp` import path:** `from agentplatform.frameworks import AdkApp` (was `vertexai.agent_engines.AdkApp`).
6. **`source_packages` is already available in 1.165.1** (`vertexai/_genai/agent_engines.py`, 30 references). The spike does not need the migration. When `source_packages` is set, `agent`, `extra_packages`, **`staging_bucket`** and `requirements` are ignored. `entrypoint_module`, `entrypoint_object` and `class_methods` are required.
7. **google-adk 2.10.0 (latest, 2026-09-25) caps aiplatform `<2` in every extra**, including `eval` (the repo uses `google-adk[eval]`): `google-cloud-aiplatform[evaluation]>=1.148,<2 ; extra == "eval"`. `uv pip compile` of `google-adk[eval]==2.10.0` + `google-cloud-aiplatform>=2` is **unsatisfiable**.
8. **ADK 2.10 `VertexAiSessionService` hard-imports `vertexai`** and calls `vertexai.Client(...).aio.agent_engines.sessions.*`. An environment with only `google-cloud-agentplatform` (no aiplatform) therefore cannot run the `agentengine://` session backend.
9. **Co-install clobbers silently.** Installing aiplatform 1.165.1 + agentplatform 2.2.0 together succeeds, but the result is a hybrid `agentplatform/` whose `Client` had **only `agent_engines`**: the new API was shadowed, with no error.
10. **The CRF dependency set resolves with no aiplatform.** Replacing CRF `requirements.txt:3` with `google-cloud-agentplatform[agent_engines]==2.2.0` resolves cleanly against the other CRF pins. In that venv, `agentplatform.Client` has `runtimes` and `sessions`, and `import vertexai` fails as expected.
11. The docs rebrand exists: Agent Runtime pages under `docs.cloud.google.com/gemini-enterprise-agent-platform/...`, and an "Agent Platform SDK for Python: version 2.0.1 migration guide". Web search summaries of GoogleCloudPlatform/generative-ai PR #3107 list the renames above.

### Unverified / contradicted

- **"Errors become `GoogleAPICallError` subclasses"** is **not supported by the code.** `runtimes.py` / `sessions.py` are built on `google.genai._api_module`, so HTTP errors surface as `google.genai.errors.APIError` (a plain `Exception`). Only `_runtimes_utils.py:878` catches `google.api_core.exceptions.NotFound`. The repo only catches bare `Exception` at these call sites, so there is no impact either way. Confirm at the first live failure.
- **Not verified live:** whether `client.runtimes.get()` on an engine *deployed with 1.165.1* registers the same `async_create_session` / `async_stream_query` methods. It should, because registration is driven by the server-side `spec.class_methods`. Task 1 verifies this against the live `creative_agent` engine before merging.
- Whether a pickled `agentplatform.frameworks.AdkApp` inside a container with aiplatform 2.2.0 imports cleanly. Its `_warn()` / telemetry paths lazily `import google.cloud.aiplatform`, which is present in aiplatform 2.x. Verified in Task 6 via scratch deploy.
- The removal date of `vertexai.Client`. The migration guide page did not render through WebFetch, and the Google Developer Knowledge MCP failed with an auth error.
- Cold-start delta of `source_packages` vs pickle+staging-bucket. Unknown; Task 2 measures it.

## Blockers & gate

**BLOCKER (root project):** google-adk 2.10.0 requires `google-cloud-aiplatform<2` in `eval`/`gcp`/`all`/`test`, and its `VertexAiSessionService` imports `vertexai`. Until an ADK release lifts the cap, the root project cannot take aiplatform 2.x. The 1.165.1-bundled `agentplatform` has no `runtimes`, and adding `google-cloud-agentplatform` beside it clobbers files.

**Gate (Task 0) passes when the latest google-adk:**
- (a) allows `google-cloud-aiplatform>=2` in `eval` and `gcp`, **or** depends on `google-cloud-agentplatform` instead, **and**
- (b) its `VertexAiSessionService` imports a module present in the target env. That is `vertexai` if we pin aiplatform 2.x, or `agentplatform` if ADK switched.

Path A (ADK lifts the cap, still imports `vertexai`) → root pins `google-cloud-aiplatform[agent-engines]>=2.2,<3`. Path B (ADK moves to agentplatform) → root pins `google-cloud-agentplatform[agent-engines]`, and `experiments/quota_spread` needs `uv run --with google-cloud-aiplatform`.

**Not blocked:** Task 1 (CRF function) and Task 2 (spike, runs on 1.165.1).

**Rollout order:** CRF first (independent island, small blast radius, unblocked) → spike (informs the deploy config) → [gate] → root dependency bump → session-engine script → deploy scripts → test/integration scripts → backend redeploy → Agent Runtime redeploys → Dependabot/docs cleanup.

---

## Task 0: Gate check (repeat before starting Task 3)

**Files:** none (read-only).

Step 1. Installed and latest ADK constraints:
```bash
uv run python -c "import importlib.metadata as m; print([r for r in m.requires('google-adk') if 'aiplatform' in r or 'agentplatform' in r])"
curl -s https://pypi.org/pypi/google-adk/json | python3 -c "import json,sys; i=json.load(sys.stdin)['info']; print(i['version']); print([r for r in i['requires_dist'] if 'aiplatform' in r or 'agentplatform' in r])"
```
Expected **today** (FAIL): `2.10.0` and every entry ends `,<2`.

Step 2. Resolver proof:
```bash
printf 'google-adk[eval]\ngoogle-cloud-aiplatform[agent-engines]>=2.2\n' | uv pip compile -q -p 3.13 -
```
Expected today: `... your requirements are unsatisfiable.` Gate passes when it prints a lock containing `google-cloud-aiplatform==2.x`.

Step 3. The session-service import after a candidate bump (in a `/tmp` venv, never the repo):
```bash
uv venv -q -p 3.13 /tmp/gate && VIRTUAL_ENV=/tmp/gate uv pip install -q "google-adk[eval]" "google-cloud-aiplatform[agent-engines]>=2.2"
grep -n "^  import vertexai\|import agentplatform" /tmp/gate/lib/python3.13/site-packages/google/adk/sessions/vertex_ai_session_service.py
```
Pick Path A (`vertexai`) or Path B (`agentplatform`). Record the result in this doc's Status line. No commit.

## Task 1: Migrate the CRF function to `google-cloud-agentplatform` (unblocked)

**Files:** `cloud_functions/creative_fanout/requirements.txt:3`, `cloud_functions/creative_fanout/main.py:47,91-104,303-305`, `cloud_functions/creative_fanout/session.py:24`, `tests/test_crf_worker_async.py:63,124,223` (+ new tests).

**Step 1: failing tests.** In `tests/test_crf_worker_async.py`, change the three `fake_vertex.agent_engines.get.return_value = remote_agent` lines (63, 124, 223) to `fake_vertex.runtimes.get.return_value = remote_agent`, then append:
```python
def test_worker_uses_runtimes_api_not_agent_engines(monkeypatch):
    """agentplatform.Client exposes `runtimes`; `agent_engines` no longer exists."""

    async def _create_session(*, user_id):
        return {"id": "sess-1"}

    async def _stream(**kwargs):
        return
        yield  # pragma: no cover

    async def _delete_session(*, user_id, session_id):
        return None

    remote_agent = MagicMock()
    remote_agent.async_create_session = _create_session
    remote_agent.async_stream_query = _stream
    remote_agent.async_delete_session = _delete_session
    fake_client = MagicMock(spec=["runtimes"])  # agent_engines access -> AttributeError
    fake_client.runtimes.get.return_value = remote_agent
    monkeypatch.setattr(main, "_get_vertex_client", lambda: fake_client)

    msg = {
        "index": 0,
        "brand": "B",
        "target_product": "p",
        "key_selling_point": "k",
        "target_audience": "a",
        "target_search_trend": "t",
    }
    asyncio.run(main.create_agent_run(agent_id="123", msg_dict=msg, user_id="u_0"))

    name = fake_client.runtimes.get.call_args.kwargs["name"]
    assert name.endswith("/reasoningEngines/123")


def test_vertex_client_is_agentplatform_client(monkeypatch):
    ctor = MagicMock()
    monkeypatch.setattr(main.agentplatform, "Client", ctor)
    monkeypatch.setattr(main, "_vertex_client", None)
    main._get_vertex_client()
    ctor.assert_called_once_with(
        project=main.config.GOOGLE_CLOUD_PROJECT, location=main.config.GCP_REGION
    )
```
Run: `uv run pytest tests/test_crf_worker_async.py -q`. Expected: **FAIL** (`AttributeError: Mock object has no attribute 'agent_engines'`, and `module ... has no attribute 'agentplatform'`).

**Step 2: implement.**
- `requirements.txt:3` → `google-cloud-agentplatform[agent_engines]==2.2.0`. Delete the aiplatform line; never keep both.
- `main.py:47` → `import agentplatform`.
- `main.py:100` → `_vertex_client = agentplatform.Client(`. Keep `project=config.GOOGLE_CLOUD_PROJECT, location=config.GCP_REGION`. Update the docstring to say "Agent Runtime client".
- `main.py:303` → `remote_agent = _get_vertex_client().runtimes.get(`.
- `session.py:24` docstring → ``(from ``client.runtimes.get``)``.
- Keep using the handle's `async_create_session` / `async_delete_session` (AdkApp class methods), **not** `client.sessions`. They are the same server-side operations, and the `agent_session` contract and tests stay unchanged.

**Step 3: verify both environments.**
```bash
uv run pytest tests/test_crf_*.py -q                  # root env (aiplatform 1.165.1's agentplatform; tests mock the client)
uv venv -q .crf-venv && VIRTUAL_ENV=.crf-venv uv pip install -q -r cloud_functions/creative_fanout/requirements.txt pytest
.crf-venv/bin/python -m pytest tests/test_crf_*.py -q  # mirrors the CI CRF job
uv run ty check
```
Expected: all pass. If `ty` flags `runtimes` as `unresolved-attribute` (the root env's 1.165.1 `agentplatform.Client` lacks it), add `# ty: ignore[unresolved-attribute]  # CRF ships google-cloud-agentplatform 2.x; root env has aiplatform 1.x's shadow copy until Task 3` on `main.py:303`. Remove it in Task 3.

**Step 4: live read-only check before merge** (verifies the unverified method-registration fact):
```bash
VIRTUAL_ENV=.crf-venv uv run --no-project python -c "import os,agentplatform; r=agentplatform.Client(project=os.environ['GOOGLE_CLOUD_PROJECT'],location='us-central1').runtimes.get(name=os.environ['CREATIVE_AGENT_ENGINE_ID']); print(r.api_resource.name, hasattr(r,'async_stream_query'), hasattr(r,'async_create_session'))"
```
Expected: `projects/.../reasoningEngines/<id> True True`.

**Step 5: commit.** `git commit -m "feat(crf): move worker to agentplatform.Client().runtimes (google-cloud-agentplatform 2.2.0)"`

**Step 6: redeploy** (after merge, from `main`). Use the exact `gcloud run deploy $CREATIVE_WORKER_CRF_NAME ...` and `$CREATIVE_CRF_NAME ...` commands in `deployment/README.md` §3.1/§3.3, **re-passing the full `--set-env-vars` list**. Deploy the worker first, then the orchestrator (same source). CRF services follow LATEST, and Eventarc triggers bind by service name, so do not recreate triggers. Verify:
```bash
gcloud run services describe creative-worker-crf --region us-central1 --format='value(status.traffic)'   # new rev @100
gcloud eventarc triggers list --location us-central1                                                     # triggers intact
```
Then publish one trend message (see README §3 smoke) and confirm a `PROCESSED` row plus `Deleted session` in the worker logs. **Rollback:** `gcloud run services update-traffic creative-worker-crf --to-revisions <prev>=100` (same for the orchestrator), then revert the PR.

**Also in this PR:** delete the pip-ecosystem ignore at `.github/dependabot.yml:35-37`. Replace it with an ignore for `google-cloud-agentplatform` majors (`>=3`) if you want symmetry.

## Task 2: Spike: `source_packages` deploy of `trend_scout` + cold-start (unblocked, time-boxed 1 day)

**Creates and deletes a scratch GCP resource. Run only with explicit user go-ahead.** Output is a findings section appended to this doc. No production change.

1. Branch `spike/p1-source-packages`. Add `trend_scout/runtime_entry.py` (scratch, not merged):
   ```python
   from vertexai.agent_engines import (
       AdkApp,
   )  # 1.165.1; agentplatform.frameworks after Task 3
   from trend_scout.agent import root_agent

   adk_app = AdkApp(agent=root_agent)
   ```
2. Scratch deploy script (`uv run python - <<'EOF' ... EOF`). Generate `class_methods` from the local app with `vertexai._genai._agent_engines_utils._generate_class_methods_spec_or_raise` (1.165.1 `:611`), then:
   ```python
   client.agent_engines.create(
       config={
           "display_name": "p1-spike-trend-scout-src",
           "source_packages": ["trend_scout", "agent_common"],
           "entrypoint_module": "trend_scout.runtime_entry",
           "entrypoint_object": "adk_app",
           "requirements_file": "requirements.txt",
           "class_methods": class_methods,
           "env_vars": ENV_VAR_DICT,
           "min_instances": 0,
       }
   )  # NO staging_bucket
   ```
   Deploy a control twin with the current pickle path: `deploy_agent.deploy_agent("trend_scout", "p1spike")` with `min_instances` overridden to 0 in the scratch copy.
3. Measure: 5 cold starts each (wait until scaled to zero, ≥15 min idle). Time `get()` → `async_create_session` → first event of `async_stream_query` using `deployment/test_deployment.py`'s flow. Record p50/max deploy duration and first-event latency in a table.
4. Record: did `source_packages` actually skip GCS staging (check `gs://$GOOGLE_CLOUD_STORAGE_BUCKET/adk-pipe/` for new objects)? Do the flat imports (`from agent_common ...`) resolve?
5. Teardown: `uv run python deployment/deploy_agent.py --resource_id=<id> --delete` for both. Verify with `--list`.
6. Commit only the findings: `git commit -m "docs(p1): record source_packages cold-start spike results"`.

**Decision rule:** adopt `source_packages` in Task 5 only if deploys succeed and cold-start is no worse than +10%. Otherwise keep `extra_packages` + `staging_bucket`.

---

**Tasks 3-8 are GATED on Task 0 passing.**

## Task 3: Bump the root dependency

**Files:** `pyproject.toml:14`, `uv.lock`, `requirements.txt`.

Path A:
```bash
uv add "google-cloud-aiplatform[agent-engines]>=2.2.0,<3.0.0"
uv export --format requirements-txt --no-hashes --no-dev -o requirements.txt
uv run python -c "import agentplatform as a; c=a.Client(project='p',location='us-central1'); print(hasattr(c,'runtimes'), hasattr(c,'sessions'))"
```
Expected: `True True`, and `grep -c agentplatform requirements.txt` → `0` (it ships inside aiplatform). Path B: `uv remove google-cloud-aiplatform && uv add "google-cloud-agentplatform[agent-engines]>=2.2,<3"`.

Run the full loop (`ruff`, `ty`, `pytest`). Expected: the suite passes unchanged (call sites still use the deprecated `vertexai` compat API). Remove the Task 1 `ty: ignore` if one was added. Remove the uv-ecosystem ignore at `.github/dependabot.yml:18-20`.

Commit: `git commit -m "chore(deps): google-cloud-aiplatform 2.x (unblocked by google-adk <ver>)"`

## Task 4: `create_session_engine.py` → `runtimes`

**Files:** `deployment/create_session_engine.py:30,68,83,92,99`, `tests/test_create_session_engine.py:4,50-86`.

Step 1 (red): in the test file, `sed -i 's/client\.agent_engines\./client.runtimes./g; s/cse\.vertexai, "Client"/cse.agentplatform, "Client"/'`, update the module docstring (line 4), and add:
```python
def test_create_or_reuse_never_touches_agent_engines():
    client = MagicMock(spec=["runtimes"])
    client.runtimes.list.return_value = iter([])  # 2.x list() is a generator
    client.runtimes.create.return_value = _engine(
        "projects/p/locations/r/reasoningEngines/9", "s"
    )
    assert cse.create_or_reuse(client, "s") == (
        "projects/p/locations/r/reasoningEngines/9",
        True,
    )
```
`uv run pytest tests/test_create_session_engine.py -q` → **FAIL** (`no attribute 'agent_engines'` / `'agentplatform'`).

Step 2 (green): `import agentplatform` (line 30). `client.runtimes.list()` (68). `client.runtimes.create(` (83). Message `"runtimes.create returned no resource"` (92; the test regex `returned no resource` still matches). `agentplatform.Client(` (99). Re-run → PASS.

Commit: `git commit -m "refactor(deploy): create_session_engine uses agentplatform runtimes API"`

## Task 5: `deploy_agent.py` → `runtimes` + `agentplatform.frameworks.AdkApp`

**Files:** `deployment/deploy_agent.py:17,128-143,147,221,234,270-274,301-302`, `tests/test_deploy_utils.py` (append).

Step 1 (red), appended to `tests/test_deploy_utils.py` (reuses `_import_deploy_agent`):
```python
class TestRuntimesApi:
    def test_list_agents_uses_runtimes_and_handles_empty_generator(
        self, monkeypatch, caplog
    ):
        da = _import_deploy_agent()
        client = MagicMock(spec=["runtimes"])
        client.runtimes.list.return_value = iter([])
        monkeypatch.setattr(da, "_get_client", lambda: client)
        with caplog.at_level("INFO"):
            da.list_agents()
        assert (
            "No agents found." in caplog.text
        )  # generator bug: old code never hit this

    def test_delete_uses_runtimes_delete_with_force(self, monkeypatch):
        da = _import_deploy_agent()
        client = MagicMock(spec=["runtimes"])
        monkeypatch.setattr(da, "_get_client", lambda: client)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "123")
        da.delete("456")
        client.runtimes.delete.assert_called_once_with(
            name=f"projects/123/locations/{da.AGENT_ENGINE_LOCATION}/reasoningEngines/456",
            force=True,
        )

    def test_client_is_agentplatform(self, monkeypatch):
        da = _import_deploy_agent()
        ctor = MagicMock()
        monkeypatch.setattr(da.agentplatform, "Client", ctor)
        monkeypatch.setattr(da, "_client", None)
        da._get_client()
        assert ctor.call_args.kwargs["location"] == da.AGENT_ENGINE_LOCATION
```
(Add `from unittest.mock import MagicMock` to the imports if it is absent.) Run `uv run pytest tests/test_deploy_utils.py -q` → **FAIL**.

Step 2 (green):
- `:17` `import agentplatform`; `:139` `agentplatform.Client(`; `:147` docstring `runtimes.create`.
- `:221` `from agentplatform.frameworks import AdkApp`.
- `:234` `_get_client().runtimes.create(`. Config is unchanged unless Task 2 adopted `source_packages`.
- `:270` `remote_agents = list(_get_client().runtimes.list())`. This fixes the latent generator-truthiness bug that exists on 1.x too.
- `:301-302` → `_get_client().runtimes.delete(name=RESOURCE_NAME, force=True)`.

Re-run → PASS. Full loop green.

Commit: `git commit -m "refactor(deploy): deploy_agent uses agentplatform runtimes API and frameworks.AdkApp"`

## Task 6: `test_deployment.py` + `integration_test.py`

**Files:** `deployment/test_deployment.py:18,64,157`, `deployment/integration_test.py:37,139,153,178`.

These are live-only scripts with no unit tests today. Change `import vertexai` → `import agentplatform`, `vertexai.Client(` → `agentplatform.Client(`, and `client.agent_engines.get(` → `client.runtimes.get(`.
```bash
grep -rn "vertexai\.Client\|agent_engines\|from vertexai" deployment cloud_functions   # expect: no output
uv run python -W error::FutureWarning deployment/integration_test.py --check health  # expect: all agents PASS, no deprecation warning
```
Commit: `git commit -m "refactor(deploy): test/integration scripts use agentplatform runtimes API"`

## Task 7: Backend (`async_app.py` / `VertexAiSessionService`) redeploy

No code change is expected: ADK owns the session client. Verify locally against the real session engine:
```bash
SESSION_SERVICE_URI=agentengine://projects/<num>/locations/us-central1/reasoningEngines/<sessions-id> \
  ALLOW_ORIGINS=http://localhost:3000 uv run uvicorn deployment.async_app:app --port 8000
curl -s -X POST localhost:8000/apps/trend_scout/users/p1/sessions -H 'content-type: application/json' -d '{}'   # expect JSON with "id"
```
Deploy with the safe-redeploy recipe (`deployment/README.md` §8 plus Step 2 flags: `--no-cpu-throttling --min-instances 1`, full env incl. `SESSION_SERVICE_URI`). **Then always pin traffic.** The deploy's success line can name an old revision, so find the newest by timestamp:
```bash
gcloud run revisions list --service trend-trawler-api --region us-central1 --sort-by="~metadata.creationTimestamp" --limit 3
gcloud run services update-traffic trend-trawler-api --region us-central1 --to-revisions <new>=100
gcloud run services describe trend-trawler-api --region us-central1 --format='value(status.traffic)'   # expect <new> 100
```
Smoke: start an `interactive_creative` run from the IAP-gated web UI, reload mid-run (the poll replays from `since=0`), and resume a checkpoint. **Rollback:** `update-traffic --to-revisions <prev>=100` (the previous revision stays tagged at 0%).

## Task 8: Redeploy Agent Runtimes + cleanup

1. Redeploy all three (new pickles reference `agentplatform.frameworks.AdkApp`). This also clears the stale-engine backlog:
   ```bash
   uv run python deployment/deploy_agent.py --version=v2 --agent=trend_scout --create
   uv run python deployment/deploy_agent.py --version=v2 --agent=creative_agent --create
   uv run python deployment/deploy_agent.py --version=v2 --agent=interactive_creative --create
   ```
   Expected: `Successfully created remote agent: projects/.../reasoningEngines/<id>` each time, and `.env` IDs updated.
2. Verify: `uv run python deployment/integration_test.py --check all` → all PASS. Then `uv run python deployment/test_deployment.py --agent=creative_agent --user_id=p1_test` streams to completion.
3. Point the CRF worker at the new creative_agent ID if it is env-configured (re-pass the full `--set-env-vars`), and re-run the one-message CRF smoke.
4. Keep the old v1 engines for 7 days as the rollback. Then delete them with `--resource_id=<old> --delete`.
5. Docs: update the CLAUDE.md "Commands" comments and `tests/test_deploy_utils.py` comments that mention `vertexai.Client` (lines 58, 95, 126, 137). Update the `deployment/README.md` SDK references. Remove the hold rationale in `.github/dependabot.yml:1-4` if no ignores remain for aiplatform.
6. Commit: `git commit -m "docs: AgentPlatform SDK migration complete; drop aiplatform major hold"`

## Rollback (summary)

| Layer | Rollback |
|---|---|
| CRF worker/orchestrator | `gcloud run services update-traffic <svc> --to-revisions <prev>=100`; revert PR (requirements back to aiplatform 1.165.1) |
| Root deps | `git revert` the Task 3 commit → `uv sync --locked`; requirements.txt comes back with it |
| trend-trawler-api | `update-traffic --to-revisions <prev-tagged>=100` (never `--to-latest` blindly) |
| Agent Runtimes | restore old engine IDs in `.env` / CRF env; old engines are kept 7 days |
| Dependabot | re-add the `google-cloud-aiplatform` semver-major ignore |

## Done criteria

- `grep -rn "vertexai.Client\|agent_engines\|from vertexai" deployment cloud_functions` returns nothing.
- CI green (both Python jobs incl. the CRF-own-deps job).
- No `FutureWarning: vertexai.Client` in our code paths. ADK's own session service may still emit it (Path A) until ADK migrates, which is acceptable and tracked upstream.
- Both aiplatform ignores are removed from `.github/dependabot.yml`.

## Spike findings (2026-09-29)

Two scratch `trend_scout` engines (resumable `App` via `AdkApp(app=app)`), both `min_instances=0`, `max_instances=2`, 4 CPU / 8Gi, concurrency 9, on google-cloud-aiplatform 1.165.1. Both are torn down. Cold = ≥15 min idle, verified cold because every cold round started a fresh container in the logs. **Reduced sample: n=3 cold rounds per twin, not the planned 5 (user-approved).** Each round ran both twins, alternating which went first.

| twin | deploy duration | cold `create_session` p50 / max | cold first-event p50 / max | cold total p50 | container boot p50 (logs) | warm reference (create / first-event) |
|---|---|---|---|---|---|---|
| `source_packages` (no staging) | 349 s | 23.0 s / 38.9 s | 3.8 s / 64.8 s | 26.8 s | 19.2 s | 0.51 s / 2.53 s |
| pickle + `extra_packages` + `staging_bucket` | 390 s | 18.1 s / 19.8 s | 4.3 s / 12.0 s | 22.3 s | 14.8 s | 0.47 s / 0.55 s |

Raw cold totals, rounds 1–3: src 103.7 / 26.8 / 19.0 s; ctl 31.8 / 22.2 / 22.3 s. Deploy durations are the server-side create→update span (client-side: src 355.5 s, ctl ~394 s).

- **Staging bucket:** `source_packages` wrote **nothing** to GCS. No objects appeared under `adk-pipe/p1-spike/` or anywhere else in the bucket during the src deploy window, because the tarball is sent inline (base64 `inline_source.source_archive`). The pickle twin wrote the usual 3 objects (`agent_engine.pkl`, `requirements.txt`, `dependencies.tar.gz`).
- **Flat imports:** these resolved in the src twin. The code lands at `/code/trend_scout/...` with `/code` on the path, so `from agent_common ...` and `from trend_scout ...` imported fine. The first event (`trend_scout` `_state_init` state_delta from the `agent_common`-seeded callback) came back normally.
- **Cold-start behaviour:** src's worst case came in round 1, the first boot after deploy. That container took 35 s to boot (vs 16 s for ctl), and the service then scaled out a *second* cold container for the stream call, which produced the 64.8 s first-event. Once the image was cached (rounds 2–3), src boots were 19.2 s and 13.2 s vs 14.8 s and 14.2 s for ctl, so the two are close but noisy. In the src container, each uvicorn worker spends about 20 s longer between importing the agent and "Application startup complete" (telemetry/setup) than the unpickle path does.
- **Errors / workarounds:** the src deploy succeeded first try, with no fixes needed. `requirements_file: "requirements.txt"` is a path relative to the tarball root, so `requirements.txt` must also be listed in `source_packages`. `class_methods` is required; generate it with `_generate_class_methods_spec_or_raise(agent=..., operations=_get_registered_operations(agent=...))`, which gives 13 methods. The SDK logs "agent framework None ... Defaulting to custom" for source deploys, which is harmless. A scratch script outside the repo needs `PYTHONPATH=$PWD` because `_create_base64_encoded_tarball` tars paths relative to the cwd and rejects paths outside it.
- **Decision: keep `extra_packages` + `staging_bucket` in Task 5.** Deploys succeed, but cold `create_session` p50 is +27% and cold total p50 is +20%, both over the +10% bar. The only win, skipping staging, doesn't offset that. A re-spike with ≥5 post-deploy-warmed rounds could flip the result, since rounds 2–3 were near parity.
