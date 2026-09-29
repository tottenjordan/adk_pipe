# P1b: Root Project → AgentPlatform SDK (P1 Tasks 3–8) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task.

**Goal:** Finish proposal P1 ([2026-09-29-p1-agentplatform-sdk-migration.md](2026-09-29-p1-agentplatform-sdk-migration.md), Tasks 3–8). Move the root uv project to `google-cloud-aiplatform` 2.x. Move every repo-owned Agent Engine call site from `vertexai.Client().agent_engines` to `agentplatform.Client().runtimes`. Redeploy the backend and the three Agent Runtimes.

**Architecture:** Keep `google-adk[eval]` 2.10.0. Lift ADK's `eval`-extra cap (`google-cloud-aiplatform<2`) with a `[tool.uv] override-dependencies` entry. That cap is policy, not a known breakage: ADK commit `fe30c015` / PR #7016 added it "matching the other extras", and google-adk 2.9.1's `eval` extra was uncapped. The aiplatform 2.2.0 wheel ships both `agentplatform` (the new API, which our code moves to) and `vertexai` (the deprecated compat API). ADK's `VertexAiSessionService` still imports `vertexai`, so ADK-owned code keeps working unchanged. Deploys keep the pickle + `extra_packages` + `staging_bucket` path, per the P1 spike findings.

**Tech Stack:** Python 3.13, uv (override-dependencies), google-adk 2.10.0, google-cloud-aiplatform 2.2.x, pytest, ruff, ty, Cloud Run (`trend-trawler-api`), Vertex AI Agent Runtime (formerly Agent Engine), Cloud Run Functions (CRF, already on standalone `google-cloud-agentplatform` 2.2.0 since #163).

**Status:** Complete 2026-09-29: P1 Tasks 3–8 done (#168–#171, #173); all three engines redeployed on 2.x (interactive_creative gained its own engine). v1 rollback engines pending deletion ≥2026-10-06.

---

## Context (verified 2026-09-29)

- **Resolution works.** A scratch copy of `pyproject.toml` with the override below plus `google-cloud-aiplatform[agent-engines]>=2.2.0,<3.0.0` gives `uv lock` → `Updated google-cloud-aiplatform v1.165.1 -> v2.2.0`. `uv export` then emits `google-adk==2.10.0` (**no extras**) and `google-cloud-aiplatform==2.2.0`. `pip install --dry-run -r` on that export succeeds, which matters because Agent Engine installs `requirements.txt` with pip, and without the extra pip never sees ADK's cap.
- **Tests pass.** A scratch env with google-adk 2.10.0 + aiplatform 2.2.0 passes all **561** tests (`tests/`, unchanged). In that env, `vertexai.Client().agent_engines.sessions` and `vertexai.agent_engines.AdkApp` still exist.
- **Still unverified live on 2.x** (each is checked before its merge/deploy below):
  - ADK `VertexAiSessionService` against the real `trend-trawler-sessions` engine (Task 1)
  - a pickled `agentplatform.frameworks.AdkApp` inside an Agent Runtime container (Task 3)
  - the `adk eval` rubric evaluators (Task 1, optional)
- **Backend image** (`Dockerfile:10`) uses `uv sync --frozen --no-dev`, so it honors the lock and the override.
- **Agent Engines are only used by** the CRF worker and the `deployment/` scripts. The backend runs agents in-process, and its only Agent Engine dependency is the session store.
- **Call sites** (current line numbers):

| File | Lines |
|---|---|
| `deployment/create_session_engine.py` | 30 `import vertexai`, 68 `.agent_engines.list()`, 83 `.agent_engines.create(`, 92 error msg, 99 `vertexai.Client(` |
| `deployment/deploy_agent.py` | 17 import, 135 docstring, 138 `vertexai.Client(`, 146 docstring, 220 `from vertexai.agent_engines import AdkApp`, 233 `.agent_engines.create(`, 269 `.agent_engines.list()`, 300–301 `.agent_engines.get(...)` + `.delete(force=True)` |
| `deployment/test_deployment.py` | 18, 64, 157 |
| `deployment/integration_test.py` | 37, 139, 153, 178 |
| `tests/test_create_session_engine.py` | 4 docstring, 50–86 (mocks `client.agent_engines.*`, `cse.vertexai`) |
| `tests/test_deploy_utils.py` | 57, 94, 125, 136 (comments only); helper `_import_deploy_agent` at 130 |

## Conventions (restate in every subagent dispatch)

- **Branches** off `main`, one PR per task: `feat/p1b-aiplatform-2x` (Task 1), `refactor/p1b-session-engine` (2), `refactor/p1b-deploy-agent` (3), `refactor/p1b-test-scripts` (4), then deploy-only Tasks 5–6, and `docs/p1b-cleanup` (7). Squash-merge each once CI is green (merge-as-you-go).
- **No attribution:** no `Co-Authored-By` trailers in commits, and no "Generated with Claude Code" or other AI attribution in PR bodies.
- **Verification loop:** `uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest tests/ -q`. `GOOGLE_CLOUD_PROJECT` must be set; `.env` provides it.
- **Dependencies:** use `uv add` / `uv remove` for `[project].dependencies`. The one documented exception is `[tool.uv] override-dependencies`, which `uv add` can't write, so it's hand-edited with a comment. After any lock change: `uv export --format requirements-txt --no-hashes --no-dev -o requirements.txt` (CI fails on drift).
- **Regions:** the Agent Runtime client uses `GCP_REGION` / `AGENT_ENGINE_LOCATION` (`us-central1`), never `global`.
- **`.env` safety:** `deploy_agent.py --create` rewrites the `<PREFIX>_AGENT_ENGINE_ID` lines in `.env`. Any scratch deploy must `cp .env /tmp/env.bak` first and `cp /tmp/env.bak .env && cmp .env /tmp/env.bak` after, then delete the scratch engine.
- **Scratch venvs** go in `/tmp`, never the repo. Never install `google-cloud-agentplatform` alongside `google-cloud-aiplatform` (both own `agentplatform/`). The root env gets `agentplatform` from aiplatform 2.x only.
- **trend-trawler-api deploys:** always pin traffic afterwards. Find the new revision by newest `creationTimestamp`, because the deploy's success line can name an old revision.
- **Outward-facing steps need the user's OK first:** the api deploy (Task 5), the Agent Runtime creates (Task 6), the CRF smoke publish (Task 6), and deleting the old engines (Task 7).

---

## Task 0: Commit this plan

**Files:**
- Create: `docs/plans/2026-09-29-p1b-root-agentplatform-migration.md` (this file)
- Modify: `docs/plans/2026-09-29-p1-agentplatform-sdk-migration.md:5` (Status line)

**Step 1:** Append to the P1 doc's `**Status:**` line:
`Gate bypassed 2026-09-29: ADK's eval-extra cap is policy (adk fe30c015), not breakage. 561 tests pass on aiplatform 2.2.0. Tasks 3–8 executing via [2026-09-29-p1b-root-agentplatform-migration.md](2026-09-29-p1b-root-agentplatform-migration.md) with a uv override.`

**Step 2:** `uv run ruff format --check docs/` doesn't lint markdown, so eyeball that every Python snippet in this file parses:
```bash
python3 - <<'EOF'
import ast, re
s = open("docs/plans/2026-09-29-p1b-root-agentplatform-migration.md").read()
for i, block in enumerate(re.findall(r"```python\n(.*?)```", s, re.S)):
    ast.parse(block)
print("ok")
EOF
```
Expected: `ok`.

**Step 3:** Commit and PR:
```bash
git add docs/plans/2026-09-29-p1b-root-agentplatform-migration.md docs/plans/2026-09-29-p1-agentplatform-sdk-migration.md
git commit -m "docs(p1): plan for root agentplatform migration via uv override"
```

---

## Task 1 (P1 Task 3): Root dependency → aiplatform 2.x via uv override

**Files:**
- Create: `tests/test_sdk_versions.py`
- Modify: `pyproject.toml:13` (our own pin), `pyproject.toml:36-37` (`[tool.uv]`), `uv.lock`, `requirements.txt`
- Modify: `.github/dependabot.yml:14-25` (uv-ecosystem aiplatform ignore + comment)

**Step 1: Write the failing test.** Create `tests/test_sdk_versions.py`:

```python
"""Guard the SDK surface the repo depends on (P1b).

The root env takes google-cloud-aiplatform 2.x through a `[tool.uv]
override-dependencies` entry (google-adk's `eval` extra caps it <2 as policy).
aiplatform 2.x ships both `agentplatform` (the API our deploy scripts use) and
`vertexai` (the deprecated compat API ADK's VertexAiSessionService still
imports), so both surfaces must be present.
"""

import importlib.metadata

import agentplatform
import vertexai


def test_aiplatform_is_2x():
    major = int(importlib.metadata.version("google-cloud-aiplatform").split(".")[0])
    assert major == 2


def test_agentplatform_client_exposes_runtimes_and_sessions():
    client = agentplatform.Client(project="p", location="us-central1")
    assert hasattr(client, "runtimes")
    assert hasattr(client, "sessions")


def test_agentplatform_frameworks_adkapp_importable():
    from agentplatform.frameworks import AdkApp  # noqa: F401


def test_vertexai_compat_still_present_for_adk_session_service():
    # google/adk/sessions/vertex_ai_session_service.py calls
    # vertexai.Client(...).aio.agent_engines.sessions.*; drop this test once ADK
    # moves to agentplatform (then the override can go too).
    client = vertexai.Client(project="p", location="us-central1")
    assert hasattr(client.agent_engines, "sessions")
```

**Step 2: Run it to verify it fails.**
Run: `uv run pytest tests/test_sdk_versions.py -q`
Expected: FAIL on `test_aiplatform_is_2x` (`assert 1 == 2`), `test_agentplatform_client_exposes_runtimes_and_sessions` (1.165.1's bundled `agentplatform.Client` has no `runtimes`), and the `frameworks` import.

**Step 3: Implement.**
1. Hand-edit `[tool.uv]` in `pyproject.toml`. This is the documented exception to the no-hand-edit rule:
   ```toml
   [tool.uv]
   package = false
   # google-adk[eval] 2.10 caps google-cloud-aiplatform <2 as policy (adk fe30c015,
   # "matching the other extras"), not because of a known break; 2.9.1's eval
   # extra was uncapped. aiplatform 2.x still ships the `vertexai` compat module
   # ADK's VertexAiSessionService imports. Remove this override once google-adk
   # allows aiplatform 2.x (re-check: P1 doc, Task 0).
   override-dependencies = [
       "google-cloud-aiplatform[agent-engines,evaluation]>=2.2.0,<3.0.0",
   ]
   ```
2. Update our own pin with uv. The command rewrites `pyproject.toml:13` and re-locks:
   ```bash
   uv add "google-cloud-aiplatform[agent-engines]>=2.2.0,<3.0.0"
   uv export --format requirements-txt --no-hashes --no-dev -o requirements.txt
   ```
3. Check the pins:
   ```bash
   grep -n "^google-adk\|^google-cloud-aiplatform\|^google-cloud-agentplatform" requirements.txt
   ```
   Expected: `google-adk==2.10.0` (no `[eval]`), `google-cloud-aiplatform==2.2.x`, and **no** `google-cloud-agentplatform` line.
4. Prove pip (Agent Engine's installer) accepts the export:
   ```bash
   rm -rf /tmp/pipchk && uv venv -q -p 3.13 --seed /tmp/pipchk
   /tmp/pipchk/bin/python -m pip install -q --dry-run -r requirements.txt; echo "exit=$?"
   ```
   Expected: `exit=0`.
5. `.github/dependabot.yml` (uv entry): delete the `google-cloud-aiplatform` semver-major ignore and its `# aiplatform 2.x = AgentPlatform SDK migration (proposal P1).` comment. Then add this to the uv entry's header comment block:
   `# pyproject [tool.uv] override-dependencies lifts google-adk[eval]'s aiplatform <2 cap; a google-adk bump never removes it — drop it by hand when ADK allows 2.x.`

**Step 4: Run tests to verify they pass.**
Run: `uv run pytest tests/test_sdk_versions.py -q` → 4 passed.
Then run the full verification loop. Expected: 565 passed; ruff and ty clean. If `ty` reports new `unresolved-attribute` errors at `vertexai.Client(...).agent_engines` call sites, leave them to Tasks 2–4, which remove those call sites. Tolerate them only if they're on lines Tasks 2–4 delete; otherwise fix them here.

**Step 5: Live pre-merge check — ADK session service on aiplatform 2.x.** Real GCP; read/write only to a throwaway user id:
```bash
set -a; source .env; set +a
SESSION_SERVICE_URI=$(gcloud run services describe trend-trawler-api --project hybrid-vertex --region us-central1 \
  --format='value(spec.template.spec.containers[0].env)' | tr ';' '\n' | grep -o "agentengine://[^'\"}]*")
echo "$SESSION_SERVICE_URI"   # expect agentengine://projects/<num>/locations/us-central1/reasoningEngines/<id>
SESSION_SERVICE_URI="$SESSION_SERVICE_URI" ALLOW_ORIGINS=http://localhost:3000 \
  uv run uvicorn deployment.async_app:app --port 8000 &  # stop with: kill %1
sleep 20
SID=$(curl -s -X POST localhost:8000/apps/trend_scout/users/p1b_check/sessions -H 'content-type: application/json' -d '{}' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "session $SID"
curl -s -o /dev/null -w "get %{http_code}\n" localhost:8000/apps/trend_scout/users/p1b_check/sessions/$SID
curl -s -o /dev/null -w "delete %{http_code}\n" -X DELETE localhost:8000/apps/trend_scout/users/p1b_check/sessions/$SID
kill %1
```
Expected: a numeric session id, `get 200`, and `delete 200` or `204`. If the env lookup prints nothing, get the URI from `deployment/README.md` ("Persistent sessions" section) or `gcloud run services describe` by eye. **If create fails, stop and report. Don't merge.**

**Step 6 (optional, ~5 min, real API calls): one ADK eval case.** This exercises the rubric evaluators from the eval extra:
```bash
PYTHONPATH="$PWD" uv run adk eval trend_scout tests/eval/evalsets/trend_scout_evalset.json \
  --config_file_path=tests/eval/eval_config.json --print_detailed_results
```
Expected: it runs to a score table without import errors. The score itself isn't the gate.

**Step 7: Commit, PR, merge on green.**
```bash
git add pyproject.toml uv.lock requirements.txt tests/test_sdk_versions.py .github/dependabot.yml
git commit -m "chore(deps): google-cloud-aiplatform 2.x via uv override (google-adk eval-extra cap is policy)"
git push -u origin feat/p1b-aiplatform-2x && gh pr create --fill
```
PR body: the override rationale and the Step 5 result. No attribution.

---

## Task 2 (P1 Task 4): `create_session_engine.py` → `runtimes`

**Files:**
- Modify: `deployment/create_session_engine.py:30,68,83,92,99`
- Test: `tests/test_create_session_engine.py:4,50-86`

**Step 1: Write the failing test.** In `tests/test_create_session_engine.py`:
```bash
sed -i 's/client\.agent_engines\./client.runtimes./g; s/cse\.vertexai, "Client"/cse.agentplatform, "Client"/' tests/test_create_session_engine.py
```
Change the module docstring (line 4) from `vertexai.Client().agent_engines` to `agentplatform.Client().runtimes`. Then append:
```python
def test_create_or_reuse_never_touches_agent_engines():
    client = MagicMock(spec=["runtimes"])  # agent_engines access -> AttributeError
    client.runtimes.list.return_value = iter([])  # 2.x list() is a generator
    client.runtimes.create.return_value = _engine(
        "projects/p/locations/r/reasoningEngines/9", "s"
    )
    assert cse.create_or_reuse(client, "s") == (
        "projects/p/locations/r/reasoningEngines/9",
        True,
    )
```
Check that `_engine` and `create_or_reuse`'s signature match the existing tests in this file, and adapt the test rather than the code.

**Step 2: Run it to verify it fails.**
Run: `uv run pytest tests/test_create_session_engine.py -q`
Expected: FAIL with `module ... has no attribute 'agentplatform'` and `Mock object has no attribute 'agent_engines'`.

**Step 3: Implement** in `deployment/create_session_engine.py`:
- `:30` `import vertexai` → `import agentplatform`
- `:68` `client.agent_engines.list()` → `client.runtimes.list()`
- `:83` `client.agent_engines.create(` → `client.runtimes.create(`
- `:92` `"agent_engines.create returned no resource"` → `"runtimes.create returned no resource"` (the test's `returned no resource` regex still matches)
- `:99` `vertexai.Client(` → `agentplatform.Client(`
- Update any docstring or comment in the file that says `agent_engines` / `vertexai`.

**Step 4: Run tests to verify they pass.**
Run: `uv run pytest tests/test_create_session_engine.py -q` → all pass. Then the full loop.

**Step 5: Live read-only check.** This lists runtimes and doesn't create one, because the script reuses an engine found by display name:
```bash
set -a; source .env; set +a
uv run python -c "
import os, agentplatform
c = agentplatform.Client(project=os.environ['GOOGLE_CLOUD_PROJECT'], location='us-central1')
print([r.api_resource.display_name for r in c.runtimes.list()])"
```
Expected: a list that includes `trend-trawler-sessions`. Don't run `create_session_engine.py` itself. It would reuse the engine, but there's nothing to gain.

**Step 6: Commit, PR, merge on green.**
```bash
git add deployment/create_session_engine.py tests/test_create_session_engine.py
git commit -m "refactor(deploy): create_session_engine uses agentplatform runtimes API"
```

---

## Task 3 (P1 Task 5): `deploy_agent.py` → `runtimes` + `agentplatform.frameworks.AdkApp`

**Files:**
- Modify: `deployment/deploy_agent.py:17,135,138,146,220,233,269,300-301`
- Test: `tests/test_deploy_utils.py` (append; `MagicMock` is already imported at line 9)

**Step 1: Write the failing tests.** Append to `tests/test_deploy_utils.py`:
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
        # A generator is always truthy, so the old `if not remote_agents` never
        # reported an empty project.
        assert "No agents found." in caplog.text

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

**Step 2: Run to verify they fail.**
Run: `uv run pytest tests/test_deploy_utils.py -q -k TestRuntimesApi`
Expected: 3 FAIL (`agent_engines` AttributeError via spec; `module has no attribute 'agentplatform'`).

**Step 3: Implement** in `deployment/deploy_agent.py`:
- `:17` `import vertexai` → `import agentplatform`
- `:135` docstring → `"""Return a cached Agent Runtime (agentplatform) client, creating it on first use."""`
- `:138` `vertexai.Client(` → `agentplatform.Client(`
- `:146` docstring `agent_engines.create` → `runtimes.create`
- `:220` `from vertexai.agent_engines import AdkApp` → `from agentplatform.frameworks import AdkApp`
- `:233` `_get_client().agent_engines.create(` → `_get_client().runtimes.create(`. Leave the config dict unchanged: pickle + `extra_packages` + `staging_bucket`, per the spike findings.
- `:269` → `remote_agents = list(_get_client().runtimes.list())`
- `:300-301` → `_get_client().runtimes.delete(name=RESOURCE_NAME, force=True)` (one line; drop the `remote_agent` variable)

**Step 4: Run tests to verify they pass.**
Run: `uv run pytest tests/test_deploy_utils.py -q` → all pass. Then the full loop.

**Step 5: Live scratch deploy.** This checks the pickled `agentplatform.frameworks.AdkApp` on an aiplatform 2.x container. It creates one billable engine and deletes it. **Ask the user first.**
```bash
cp .env /tmp/env.bak
uv run python deployment/deploy_agent.py --version=p1b_scratch --agent=trend_scout --create 2>&1 | tee /tmp/p1b_scratch.log
grep -o "projects/[0-9]*/locations/us-central1/reasoningEngines/[0-9]*" /tmp/p1b_scratch.log | tail -1 | tee /tmp/p1b_scratch_id.txt
cp /tmp/env.bak .env && cmp .env /tmp/env.bak && echo ENV_RESTORED
```
Expected: `Successfully created remote agent: projects/.../reasoningEngines/<id>` (about 6–7 min), then `ENV_RESTORED`. Record the resource name **immediately**.

Then run a session round-trip on the scratch engine with the new API:
```bash
set -a; source .env; set +a
NAME=$(cat /tmp/p1b_scratch_id.txt) uv run python - <<'EOF'
import asyncio, os, agentplatform
c = agentplatform.Client(project=os.environ["GOOGLE_CLOUD_PROJECT"], location="us-central1")
r = c.runtimes.get(name=os.environ["NAME"])
async def main():
    s = await r.async_create_session(user_id="p1b_check")
    sid = s["id"] if isinstance(s, dict) else s.id
    async for ev in r.async_stream_query(user_id="p1b_check", session_id=sid, message="ping"):
        print("first event ok:", str(ev)[:120]); break
    await r.async_delete_session(user_id="p1b_check", session_id=sid)
    print("session round-trip ok")
asyncio.run(main())
EOF
```
Expected: `first event ok: ...` then `session round-trip ok`. If the engine fails to start, pull its logs (`deployment/README.md`, the `resource.type="aiplatform.googleapis.com/ReasoningEngine"` query) and **stop and report**.

Teardown (mandatory, even on failure):
```bash
uv run python deployment/deploy_agent.py --resource_id=$(basename $(cat /tmp/p1b_scratch_id.txt)) --delete
uv run python deployment/deploy_agent.py --list | grep -c p1b_scratch   # expect 0 (also exercises the new list())
gsutil -m rm -r "gs://$GOOGLE_CLOUD_STORAGE_BUCKET/adk-pipe/*/p1b_scratch/" 2>/dev/null || true
git diff --quiet .env && echo ENV_CLEAN
```

**Step 6: Commit, PR, merge on green.**
```bash
git add deployment/deploy_agent.py tests/test_deploy_utils.py
git commit -m "refactor(deploy): deploy_agent uses agentplatform runtimes API and frameworks.AdkApp"
```

---

## Task 4 (P1 Task 6): `test_deployment.py` + `integration_test.py` + legacy-API guard

**Files:**
- Create: `tests/test_no_legacy_agent_engines_api.py`
- Modify: `deployment/test_deployment.py:18,64,157`, `deployment/integration_test.py:37,139,153,178`

**Step 1: Write the failing guard test.** Create `tests/test_no_legacy_agent_engines_api.py`:
```python
"""Repo-owned Agent Runtime call sites must use agentplatform, not the
deprecated vertexai.Client().agent_engines API (P1). ADK's own
VertexAiSessionService still uses vertexai internally; that's out of scope.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNED = ["deployment", "cloud_functions"]
LEGACY = re.compile(
    r"^\s*(import vertexai|from vertexai\b)|vertexai\.Client\(|\.agent_engines\."
)


def test_no_legacy_agent_engines_calls():
    offenders = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for d in SCANNED
        for path in sorted((ROOT / d).rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if LEGACY.search(line)
    ]
    assert not offenders, "\n".join(offenders)
```

**Step 2: Run it to verify it fails.**
Run: `uv run pytest tests/test_no_legacy_agent_engines_api.py -q`
Expected: FAIL, listing exactly the `test_deployment.py` and `integration_test.py` lines above (Tasks 2–3 already cleaned the rest). If it lists anything else, fix it here too.

**Step 3: Implement.** In both scripts: `import vertexai` → `import agentplatform`, `vertexai.Client(` → `agentplatform.Client(`, and `client.agent_engines.get(` → `client.runtimes.get(`. Update any comment or docstring naming `agent_engines`.

**Step 4: Run tests to verify they pass.**
Run: `uv run pytest tests/test_no_legacy_agent_engines_api.py -q` → PASS. Then the full loop.

**Step 5: Live read-only check** against the current (v1) engines:
```bash
uv run python -W error::FutureWarning deployment/integration_test.py --check health
```
Expected: every agent PASSes, and there's no `FutureWarning: The vertexai.Client class is deprecated`. `-W error` would turn that warning into a crash in our own code path.

**Step 6: Commit, PR, merge on green.**
```bash
git add deployment/test_deployment.py deployment/integration_test.py tests/test_no_legacy_agent_engines_api.py
git commit -m "refactor(deploy): test/integration scripts use agentplatform runtimes API"
```

---

## Task 5 (P1 Task 7): Redeploy the backend (`trend-trawler-api`)

No code change: ADK owns the session client, and Task 1 Step 5 already proved it on 2.x. **Ask the user before deploying.**

**Step 1:** Record the current serving revision for rollback:
```bash
gcloud run services describe trend-trawler-api --project hybrid-vertex --region us-central1 --format='value(status.traffic)'
```

**Step 2:** Deploy from an up-to-date `main`, using the safe-redeploy recipe. Pass no env flags (env and IAP are preserved) and keep the required runtime flags:
```bash
git checkout main && git pull
gcloud run deploy trend-trawler-api --project hybrid-vertex --region us-central1 --source . \
  --no-cpu-throttling --min-instances 1 --memory 8Gi --cpu 4 --timeout 900 \
  --service-account tt-api-sa@hybrid-vertex.iam.gserviceaccount.com --no-allow-unauthenticated --quiet
```

**Step 3:** Pin traffic to the **newest** revision. The success line can name an old one:
```bash
gcloud run revisions list --service trend-trawler-api --project hybrid-vertex --region us-central1 \
  --sort-by="~metadata.creationTimestamp" --limit 3
gcloud run services update-traffic trend-trawler-api --project hybrid-vertex --region us-central1 --to-revisions <new>=100
gcloud run services describe trend-trawler-api --project hybrid-vertex --region us-central1 --format='value(status.traffic)'
```
Expected: `<new>` at 100, and the env still includes `SESSION_SERVICE_URI`.

**Step 4: Smoke.** This runs `interactive_creative` end to end through `/runs` (session create, duplicate-start guard, 3 checkpoint resumes, since=0 replay):
```bash
uv run python /tmp/tt_smoke.py https://trend-trawler-api-qqzji3hyoa-uc.a.run.app
```
If `/tmp/tt_smoke.py` is gone, do the same by hand in the IAP-gated web UI: start an `interactive_creative` run, reload mid-run, and approve all 3 checkpoints. Expected: `DONE`, 3 checkpoints, replay event count == cursor, and `creative_evaluation_report` SET.
**Rollback:** `update-traffic --to-revisions <prev>=100`.

---

## Task 6 (P1 Task 8a): Redeploy the three Agent Runtimes + CRF smoke

Creates three new engines; the old v1 engines stay as the rollback. **Ask the user first.** This also clears the stale-engine backlog: the deployed `creative_agent` engine predates #114–#118.

**Step 1:** Record the current engine IDs (the rollback targets):
```bash
grep "_AGENT_ENGINE_ID" .env | tee /tmp/p1b_v1_engine_ids.txt
```

**Step 2:** Deploy each agent. `.env` is updated with the new IDs, which is intended here:
```bash
uv run python deployment/deploy_agent.py --version=v2 --agent=trend_scout --create
uv run python deployment/deploy_agent.py --version=v2 --agent=creative_agent --create
uv run python deployment/deploy_agent.py --version=v2 --agent=interactive_creative --create
```
Expected: `Successfully created remote agent: projects/.../reasoningEngines/<id>` for each (about 6–7 min each).

**Step 3: Verify.**
```bash
uv run python deployment/integration_test.py --check all                        # expect all PASS
uv run python deployment/test_deployment.py --agent=creative_agent --user_id=p1b_test  # streams to completion
```

**Step 4: CRF smoke on the new `creative_agent` engine.** This costs one full creative run (about 10 min). The worker takes the engine id from the message, so no CRF redeploy is needed:
```bash
NEW_ID=$(grep "^CREATIVE_AGENT_ENGINE_ID" .env | cut -d= -f2 | tr -d "'\"" | xargs basename)
gcloud pubsub topics publish creative-eventarc-topic --project hybrid-vertex \
  --message "{\"bq_dataset\":\"trend_trawler\",\"bq_table\":\"target_trends_crf\",\"agent_resource_id\":\"$NEW_ID\",\"max_rows\":1}"
```
`max_rows: 1` dispatches exactly the oldest unprocessed row (orchestrator cap: `CRF_MAX_ROWS_PER_RUN=3`). Expected: an orchestrator log line `Selecting up to 1 unprocessed rows.`, a worker `Deleted session` log, and that row's `processed_status` = `PROCESSED` in BigQuery.

**Rollback:** restore the v1 IDs from `/tmp/p1b_v1_engine_ids.txt` into `.env`. The CRF needs nothing: it uses whatever id the message carries.

---

## Task 7 (P1 Task 8b): Docs, Dependabot, old-engine cleanup

**Files:** `CLAUDE.md` (Naming line 16, Requirements), `README.md:9`, `deployment/README.md` (SDK references), `tests/test_deploy_utils.py:57,94,125,136` (comments), `.github/dependabot.yml:1-4` (header), P1 doc Status line.

**Step 1:** Docs:
- `CLAUDE.md:16` / `README.md:9`: replace "The code still uses the aiplatform 1.x `agent_engines` API; SDK migration is P1 …" with "The code uses the AgentPlatform SDK (`agentplatform.Client().runtimes`); the root env gets it from google-cloud-aiplatform 2.x via a uv override (see `pyproject.toml` `[tool.uv]`)."
- In the CLAUDE.md Requirements list, add `google-cloud-aiplatform 2.x (via [tool.uv] override-dependencies — google-adk[eval] caps <2)`.
- Comments in `tests/test_deploy_utils.py`: `vertexai.Client` → `agentplatform.Client`.
- `deployment/README.md`: replace every `vertexai.Client` / `agent_engines` SDK mention with `agentplatform.Client` / `runtimes` (`grep -n "vertexai\|agent_engines" deployment/README.md`).
- `.github/dependabot.yml:1-4`: keep the header. Only reword it if no aiplatform ignores remain (the uv one went in Task 1; the pip entry already targets agentplatform).
- P1 doc Status: `Complete <date>: Tasks 3–8 done via p1b (#…).`

**Step 2:** Verify: full loop green, plus:
```bash
grep -rn "vertexai\.Client\|\.agent_engines\.\|from vertexai" deployment cloud_functions   # expect no output
```

**Step 3:** Commit, PR, merge on green:
```bash
git commit -am "docs: AgentPlatform SDK migration complete (root env on aiplatform 2.x via uv override)"
```

**Step 4 (≥7 days after Task 6, needs user OK):** delete the v1 engines listed in `/tmp/p1b_v1_engine_ids.txt`. Copy that list into memory as well, since `/tmp` may not survive.
```bash
uv run python deployment/deploy_agent.py --resource_id=<old-id> --delete   # x3
uv run python deployment/deploy_agent.py --list                            # only the v2 engines + trend-trawler-sessions remain
```

---

## Rollback (summary)

| Layer | Rollback |
|---|---|
| Root deps (Task 1) | `git revert` the Task 1 commit → `uv sync --locked`; `requirements.txt` reverts with it |
| Deploy/test scripts (Tasks 2–4) | `git revert` (depends on Task 1: revert them first, then Task 1) |
| trend-trawler-api (Task 5) | `update-traffic --to-revisions <prev>=100` |
| Agent Runtimes (Task 6) | restore v1 IDs in `.env`; v1 engines kept ≥7 days; CRF messages name the engine explicitly |
| Override removal (future) | when a google-adk release allows aiplatform 2.x, delete the `[tool.uv] override-dependencies` block and `test_vertexai_compat_still_present_for_adk_session_service` once ADK stops importing `vertexai`; `uv lock` must still resolve aiplatform 2.x |

## Done criteria

- `grep -rn "vertexai\.Client\|\.agent_engines\.\|from vertexai" deployment cloud_functions` → empty, and `tests/test_no_legacy_agent_engines_api.py` enforces it.
- `tests/test_sdk_versions.py` green: aiplatform 2.x, `runtimes`/`sessions`, `frameworks.AdkApp`, and the `vertexai` compat that ADK still needs.
- Both python-ci jobs green.
- Backend on a new revision at 100%, with the interactive smoke passing.
- Three v2 Agent Runtimes live, `integration_test.py --check all` PASS, CRF `max_rows: 1` smoke → `PROCESSED`.
- Dependabot: no aiplatform major ignore in the uv entry, and the override is documented.
- Any `FutureWarning: vertexai.Client` now comes only from ADK's session service. That's acceptable until ADK migrates.
