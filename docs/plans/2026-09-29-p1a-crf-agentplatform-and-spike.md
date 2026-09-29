# P1a: CRF → AgentPlatform SDK + `source_packages` Spike Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task.
> Scoped from [P1](2026-09-29-p1-agentplatform-sdk-migration.md) (Tasks 1–2 only; Tasks 3–8 gated).

**Goal:** Do the two *unblocked* parts of proposal P1:
1. Move the Cloud Run Function worker off the deprecated `vertexai.Client().agent_engines` onto `agentplatform.Client().runtimes`, using the standalone `google-cloud-agentplatform` 2.2.0 wheel, then redeploy it.
2. Run a time-boxed spike measuring whether `source_packages` deploys (no GCS staging bucket) are viable and no slower on cold start than today's pickle + `staging_bucket` path.

**Architecture:** The CRF function is its own dependency island (its own `requirements.txt`, no ADK), so it can take the agentplatform 2.x wheel today. The root uv project stays on aiplatform 1.165.1. The spike runs entirely on 1.165.1 (which already supports `source_packages`) against two scratch engines that are torn down afterwards. P1 Tasks 3–8 stay gated (see Context).

**Tech Stack:** Python 3.13, uv, google-cloud-agentplatform 2.2.0 (CRF only), google-cloud-aiplatform 1.165.1 (root), pytest, ruff, ty, Cloud Run Functions + Eventarc, Vertex AI Agent Runtime (formerly Agent Engine).

---

## Context

- Source proposal: `docs/plans/2026-09-29-p1-agentplatform-sdk-migration.md`. Its call-site table and upstream facts are still accurate. Line numbers drifted by 1 in `deployment/deploy_agent.py` (e.g. the client is at `:138`, `AdkApp` import at `:220`); that file isn't touched in this plan.
- **Gate re-checked 2026-09-29: still FAIL.** google-adk 2.10.0 (latest on PyPI) requires `google-cloud-aiplatform<2` in the `all`/`eval`/`gcp`/`test` extras. PyPI latest: agentplatform 2.2.0, aiplatform 2.2.0. So P1 Tasks 3–8 are out of scope here.
- **Scope (user-chosen):** CRF migration and deploy, plus the spike.

## Conventions (restate in every subagent dispatch)

- Branches off `main`: `feat/p1-crf-agentplatform` (Task 1), `spike/p1-source-packages` (Task 2). Squash-merge after CI is green (the established merge-as-you-go pattern).
- **No `Co-Authored-By` trailers or AI attribution in commits, and no "Generated with Claude Code" in PR bodies.**
- Verification loop: `uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest tests/ -q` (`GOOGLE_CLOUD_PROJECT` must be set; `.env` provides it).
- Put scratch venvs in **`/tmp/crf-venv`**, not the repo. `.crf-venv` isn't gitignored, so ruff would lint it.
- **Never install `google-cloud-agentplatform` and `google-cloud-aiplatform` in one env.** Both own top-level `agentplatform/`, and the co-install silently shadows `runtimes`.
- The Agent Runtime client uses `GCP_REGION` (`us-central1`), never `global`.
- CRF redeploys **must re-pass the full `--set-env-vars`** (`--set-env-vars` replaces the whole env; `GOOGLE_CLOUD_PROJECT` is required).

---

## Task 0: Commit the plan

1. `git checkout -b feat/p1-crf-agentplatform`
2. Copy this file to `docs/plans/2026-09-29-p1a-crf-agentplatform-and-spike.md`.
3. In the P1 doc's `**Status:**` line, append: `Gate re-checked 2026-09-29: FAIL (google-adk 2.10.0 still caps aiplatform <2). Tasks 1–2 executing via 2026-09-29-p1a-crf-agentplatform-and-spike.md.`
4. `uv run ruff format docs/plans/2026-09-29-p1a-*.md`. The Python CI doesn't run on docs-only changes, so malformed snippets would break the next Python PR.
5. `git commit -m "docs(p1): execution plan for CRF agentplatform migration + source_packages spike"`

## Task 1: CRF worker → `agentplatform.Client().runtimes`

**Files:**
- Modify: `cloud_functions/creative_fanout/requirements.txt:3`
- Modify: `cloud_functions/creative_fanout/main.py:47` (import), `:91-104` (`_get_vertex_client`), `:303` (`.agent_engines.get`)
- Modify: `cloud_functions/creative_fanout/session.py:24` (docstring)
- Modify: `.github/dependabot.yml:40-42` (pip-ecosystem aiplatform ignore)
- Test: `tests/test_crf_worker_async.py:63,124,223` + 2 new tests

**Step 1: Write the failing tests.** In `tests/test_crf_worker_async.py`, change the three `fake_vertex.agent_engines.get.return_value = remote_agent` lines (63, 124, 223) to `fake_vertex.runtimes.get.return_value = remote_agent`. Then append:

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

Before running, check that the existing helpers match: how the file imports `main`, whether the `create_agent_run` keyword names match `main.py:283-290`, and the `msg_dict` keys the function reads. Adapt the test to them rather than changing `main.py` to fit the test.

**Step 2: Run the tests to verify they fail.**
Run: `uv run pytest tests/test_crf_worker_async.py -q`
Expected: FAIL with `Mock object has no attribute 'agent_engines'` and `module 'main' has no attribute 'agentplatform'`.

**Step 3: Implement.**
- `requirements.txt:3`: change `google-cloud-aiplatform[agent_engines]==1.165.1` to `google-cloud-agentplatform[agent_engines]==2.2.0`. Delete the aiplatform line entirely.
- `main.py:47`: change `import vertexai` to `import agentplatform`.
- `main.py:100`: change `_vertex_client = vertexai.Client(` to `_vertex_client = agentplatform.Client(`. Keep the `project=` and `location=` args unchanged, and update the docstring to say "Agent Runtime client".
- `main.py:303`: change `_get_vertex_client().agent_engines.get(` to `_get_vertex_client().runtimes.get(`.
- `session.py:24`: change the docstring reference ``agent_engines.get`` to ``client.runtimes.get``.
- Keep using the handle's `async_create_session` / `async_stream_query` / `async_delete_session` (AdkApp class methods), **not** `client.sessions`. That keeps the `agent_session` contract and its tests unchanged.
- `.github/dependabot.yml`: in the `pip` entry, replace the `google-cloud-aiplatform` major ignore (and its comment) with:
  ```yaml
        # google-cloud-agentplatform 3.x would be a new SDK major; migrate deliberately.
        - dependency-name: "google-cloud-agentplatform"
          update-types: ["version-update:semver-major"]
  ```
  Leave the `uv` entry's aiplatform ignore in place; it's gated.

**Step 4: Run the tests in both environments.**
```bash
uv run pytest tests/test_crf_*.py -q          # root env: aiplatform 1.165.1's bundled agentplatform; client is mocked
uv venv -q -p 3.13 /tmp/crf-venv && VIRTUAL_ENV=/tmp/crf-venv uv pip install -q -r cloud_functions/creative_fanout/requirements.txt pytest
/tmp/crf-venv/bin/python -m pytest tests/test_crf_*.py -q   # mirrors the python-ci "CRF own deps" job
/tmp/crf-venv/bin/python -c "import agentplatform as a; c=a.Client(project='p',location='us-central1'); print(hasattr(c,'runtimes')); import importlib.util as u; print(u.find_spec('vertexai'))"
```
Expected: all pass. The last command prints `True` then `None` (no aiplatform in the CRF env).

Then run the full verification loop. If `ty` flags `runtimes` as `unresolved-attribute` (the root env's 1.165.1 `agentplatform.Client` lacks it), add this on the `main.py` `.runtimes.get(` line:
`# ty: ignore[unresolved-attribute]  # CRF ships google-cloud-agentplatform 2.x; root env has aiplatform 1.x's copy until P1 Task 3`

**Step 5: Live read-only check (before merge).** This verifies that an engine deployed with 1.165.1 still registers the methods the worker calls:
```bash
set -a; source .env; set +a
/tmp/crf-venv/bin/python -c "import os,agentplatform; r=agentplatform.Client(project=os.environ['GOOGLE_CLOUD_PROJECT'],location='us-central1').runtimes.get(name=os.environ['CREATIVE_AGENT_ENGINE_ID']); print(r.api_resource.name, hasattr(r,'async_stream_query'), hasattr(r,'async_create_session'), hasattr(r,'async_delete_session'))"
```
Expected: `projects/.../reasoningEngines/<id> True True True`. If any is `False`, **stop and report**; don't merge.

**Step 6: Commit, then open the PR.**
```bash
git add cloud_functions/creative_fanout tests/test_crf_worker_async.py .github/dependabot.yml
git commit -m "feat(crf): move worker to agentplatform.Client().runtimes (google-cloud-agentplatform 2.2.0)"
git push -u origin feat/p1-crf-agentplatform && gh pr create --fill
```
The PR body must have no attribution line. Merge (squash) once both python-ci jobs are green.

**Step 7: Redeploy from `main`.**
1. `git checkout main && git pull`
2. Run the worker deploy (`deployment/README.md` §3, the `gcloud run deploy $CREATIVE_WORKER_CRF_NAME` block at ~`:290`) with its full `--set-env-vars` line. That line must include `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_PROJECT_NUMBER`, `GCP_REGION` and `AGENT_WORKER_USER_ID=crf_worker`.
3. Run the orchestrator deploy (`gcloud run deploy $CREATIVE_CRF_NAME`, ~`:254`, same source) with its full env line, which adds `CREATIVE_WORKER_TOPIC_NAME=creative-worker-queue-topic`.
4. Verify:
   ```bash
   gcloud run services describe creative-worker-crf  --region us-central1 --format='yaml(status.traffic,spec.template.spec.containers[0].env)'
   gcloud run services describe creative-trawler-crf --region us-central1 --format='yaml(status.traffic,spec.template.spec.containers[0].env)'
   gcloud eventarc triggers list --location us-central1   # creative-eventarc-topic + creative-worker-queue-topic triggers intact
   ```
   Expected: the new revision at 100% on both services (they follow LATEST; no pin needed), env intact, and both triggers present. Rollback targets: worker `creative-worker-crf-00011-lbt`, orchestrator `creative-trawler-crf-00006-2l5`.
5. **Smoke (outward-facing; costs one full creative_agent run on the stale-but-working engine):** follow README §5 to publish one orchestrator message. Expect a worker log line with `Deleted session` and the row going to `PROCESSED`. If there are no unprocessed rows, the orchestrator no-ops. Report that instead of fabricating a row, and ask the user before inserting a test row.
6. Update memory (the `adk-pipe-work-status` CRF revisions).

## Task 2: Spike: `source_packages` deploy of `trend_scout` + cold-start

**Creates two billable scratch Agent Engines. Confirm with the user right before step 3.** Time box: 1 day. Nothing merges except a findings section.

**Files:**
- Create (scratch, never merged): `trend_scout/runtime_entry.py`, `/tmp/p1_spike.py`, `/tmp/p1_coldstart.py`
- Modify: `docs/plans/2026-09-29-p1-agentplatform-sdk-migration.md` (append "Spike findings")

1. `git checkout main && git pull && git checkout -b spike/p1-source-packages`
2. `trend_scout/runtime_entry.py`. Deploy the **resumable `App`**, mirroring `resolve_deploy_target` in `deployment/deploy_agent.py`:
   ```python
   from vertexai.agent_engines import AdkApp  # agentplatform.frameworks after P1 Task 3

   from trend_scout.agent import app

   adk_app = AdkApp(app=app)
   ```
3. `/tmp/p1_spike.py` (run with `uv run python /tmp/p1_spike.py`). It deploys both twins with `min_instances=0` and **does not call `deploy_agent.deploy_agent()`**, because that function's `update_env_file` would overwrite the prod `SCOUT_AGENT_ENGINE_ID` in `.env`:
   ```python
   import time

   from vertexai._genai import _agent_engines_utils as u

   from deployment.deploy_agent import ENV_VAR_DICT, _get_client
   from trend_scout.runtime_entry import adk_app

   client = _get_client()
   methods = [
       u._to_dict(m)
       for m in u._generate_class_methods_spec_or_raise(
           agent=adk_app, operations=u._get_registered_operations(agent=adk_app)
       )
   ]
   common = {
       "env_vars": ENV_VAR_DICT,
       "min_instances": 0,
       "max_instances": 2,
       "resource_limits": {"cpu": "4", "memory": "8Gi"},
       "container_concurrency": 9,
   }
   t = time.time()
   src = client.agent_engines.create(
       config={
           **common,
           "display_name": "p1-spike-trend-scout-src",
           "source_packages": ["trend_scout", "agent_common", "requirements.txt"],
           "entrypoint_module": "trend_scout.runtime_entry",
           "entrypoint_object": "adk_app",
           "requirements_file": "requirements.txt",
           "class_methods": methods,
       }
   )  # no staging_bucket
   print("src", src.api_resource.name, f"{time.time() - t:.0f}s")
   t = time.time()
   ctl = client.agent_engines.create(
       agent=adk_app,
       config={
           **common,
           "display_name": "p1-spike-trend-scout-pickle",
           "requirements": "./requirements.txt",
           "extra_packages": ["./trend_scout", "./agent_common"],
           "staging_bucket": f"gs://{ENV_VAR_DICT['GOOGLE_CLOUD_STORAGE_BUCKET']}",
           "gcs_dir_name": "adk-pipe/p1-spike/staging",
       },
   )
   print("ctl", ctl.api_resource.name, f"{time.time() - t:.0f}s")
   ```
   Record both resource names **immediately** (for teardown). If `ENV_VAR_DICT` lacks the bucket key, read `os.environ["GOOGLE_CLOUD_STORAGE_BUCKET"]` instead. If the `source_packages` deploy fails, capture the error: that is itself the finding (e.g. `requirements_file` path semantics, or the flat-import layout inside the tarball).
4. **Staging check:** `gsutil ls -r gs://$GOOGLE_CLOUD_STORAGE_BUCKET/adk-pipe/ | grep -c p1-spike`. Expected: objects only under the pickle twin's `p1-spike/staging`, none from the `src` deploy.
5. **Cold-start measurement.** `/tmp/p1_coldstart.py <resource_name>` does `runtimes`-free 1.x calls: `client.agent_engines.get(name=...)`, then times `async_create_session(user_id="p1spike")`, then time to the **first** event of `async_stream_query(user_id=..., session_id=..., message="ping")`. It then stops iterating and calls `async_delete_session`. Run it against both engines, interleaved, **5 times each**, idling ≥15 min between rounds so instances scale to zero. The scale-to-zero idle window is undocumented; if round-1 and round-2 latencies look warm, lengthen the idle and note it.
6. **Findings** (append to the P1 doc under `## Spike findings (2026-09-xx)`): a table of deploy duration and create-session / first-event latency (p50 and max per twin), the staging-bucket result, whether flat imports resolved, and any errors. Apply the decision rule: adopt `source_packages` in P1 Task 5 only if deploys succeed and the cold-start p50 is no worse than +10%.
7. **Teardown (mandatory, even on failure):**
   ```bash
   uv run python deployment/deploy_agent.py --resource_id=<src-id> --delete
   uv run python deployment/deploy_agent.py --resource_id=<ctl-id> --delete
   uv run python deployment/deploy_agent.py --list          # neither p1-spike engine listed
   gsutil -m rm -r gs://$GOOGLE_CLOUD_STORAGE_BUCKET/adk-pipe/p1-spike/
   git diff --quiet .env || echo "STOP: .env changed"       # prod engine IDs untouched
   ```
8. Commit **only** the P1 doc change (drop `trend_scout/runtime_entry.py`):
   `git commit -m "docs(p1): record source_packages cold-start spike results"`. Then open the PR and squash-merge.

---

## Verification (end to end)

- **CI:** both python-ci jobs are green (root env + CRF own-deps).
- `grep -rn "vertexai\|agent_engines" cloud_functions/` returns nothing.
- The live `runtimes.get` check printed `True True True`.
- Both CRF services are on new revisions at 100% with env intact, both Eventarc triggers are present, and the one-message smoke reached `PROCESSED`, or a documented no-op.
- **Spike:** the findings table is in the P1 doc, both scratch engines are deleted, and `.env` is unchanged.
- **Memory:** the work-status board reflects the new CRF revisions and the spike outcome.

## Out of scope (still gated)

P1 Tasks 3–8 (root aiplatform 2.x bump, `create_session_engine.py` / `deploy_agent.py` / test-script migration, backend redeploy, Agent Runtime redeploys) wait for a google-adk release that lifts the `<2` cap. Re-run P1 Task 0 before starting them.
