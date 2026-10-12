# Cloud Build Optimizations Implementation Plan (4 parallel PRs)

**Goal:** Cut Cloud Build time and image size with the four changes recommended by the 2026-10-12 build analysis (40 builds):
- **F.** Agent deploys stop paying a ~50s worker queue.
- **G.** The CRF image drops pandas/pyarrow/numpy (~300 MB uncompressed).
- **H.** Untracked local files stop invalidating the web layer cache (~80s rebuilds).
- **I.** Same-source builds produce identical digests.

**Architecture:**
- F, H and I are config edits with structural tests.
- G is a small behaviour-preserving refactor of `cloud_functions/creative_fanout/main.py`: BigQuery `Row` iteration replaces `to_dataframe()`, the same pattern as `trend_scout/tools.py` in #344. It is followed by a CRF redeploy.

**Tech Stack:** Cloud Build, Docker BuildKit/buildx (`rewrite-timestamp`, `SOURCE_DATE_EPOCH`), google-cloud-bigquery `RowIterator`/`Row`, pytest.

---

## Context (evidence from the analysis)

| Finding | Evidence | Fix |
|---|---|---|
| Agent builds queue 49–53s every time | `deployment/cloudbuild.agents.yaml` `options.machineType: E2_HIGHCPU_8`. Default-machine builds start in 0–2s. The build's local work is a 7–12s `uv sync` plus light packaging; the deploys wait on Vertex. | **F**: default machine |
| CRF image 778 MB uncompressed, ~310 MB of it pyarrow (167) + pandas (72) + numpy (70) | Used only by `main.py:737` `.to_dataframe()` → `df.iterrows()` → `build_row_dict`. `google-cloud-bigquery[pandas]` is the only source. `numpy`/`pytz`/`tzdata` are pinned but never imported. | **G** |
| Web rebuilt for 92s with no frontend change | `COPY . .` cache miss caused by the gitignored but uploaded `frontend/tsconfig.tsbuildinfo` / `next-env.d.ts`, which differ between checkouts. `frontend/.dockerignore` lacks them. | **H** |
| 3 builds of one commit gave 3 api digests | The api `src` stage re-runs, and its symlink `RUN` stamps new mtimes, so only the 0.7 MB code layer differs. A no-op build is indistinguishable from a real change. | **I**: `rewrite-timestamp=true` + `SOURCE_DATE_EPOCH` |

**Constraints (every PR):**
- Worktree with `.python-version` copied (3.13), `unset VIRTUAL_ENV`.
- Gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`.
- G also runs the CRF venv check: `uv venv /tmp/crf && uv pip install --python /tmp/crf/bin/python -r cloud_functions/creative_fanout/requirements.txt pytest && GOOGLE_CLOUD_PROJECT=test-project /tmp/crf/bin/python -m pytest tests/test_crf_*.py -q`.
- TDD, no Co-Authored-By or AI attribution, open the PR without merging, no deploys.
- YAML/ignore-only PRs need a manual `python-ci.yml` dispatch (path-gated CI).

## File ownership (disjoint)

| PR | Branch | Files |
|---|---|---|
| **F** | `build/agents-default-machine` | `deployment/cloudbuild.agents.yaml` (options), `tests/test_cloudbuild_agents.py`, `deployment/README.md` (engines-via-Cloud-Build section only) |
| **G** | `build/crf-drop-pandas` | `cloud_functions/creative_fanout/main.py`, `cloud_functions/creative_fanout/requirements.txt`, `tests/test_crf_entrypoint.py`, `tests/test_crf_sql_params.py`, `pyproject.toml` (dev group: the 3 CRF-only lines + stale comment), `uv.lock`, CLAUDE.md (Requirements dev-group line), plan copy |
| **H** | `build/web-context-hygiene` | `frontend/.dockerignore`, `tests/test_build_context.py` (new) |
| **I** | `build/reproducible-digests` | `cloudbuild.yaml`, `deployment/bandit/cloudbuild.bandit.yaml`, `tests/test_creative_fanout_image.py` (its root-cloudbuild tests), `tests/test_bandit_cloudbuild.py`, `deployment/README.md` (cached-build + bandit build command snippets only) |

F and I edit different README sections. Nothing else is shared.

---

## PR F — agent builds on the default machine
1. **Test first** (`tests/test_cloudbuild_agents.py`): `options` has no `machineType`, or it is absent from the parsed YAML, with a comment explaining why. Run it and watch it fail.
2. **Change:** delete `machineType: E2_HIGHCPU_8` from `options`. Add a one-line comment noting that the default machine starts in ~1s while `E2_HIGHCPU_8` queued ~50s per build, and the work here is remote.
3. **README:** update the engines section if it mentions the machine.
4. **Commit:** `build(agents): default Cloud Build machine (drop ~50s queue per engine deploy)`.

## PR G — CRF without pandas (behaviour-preserving)

**Code-review focus:**
- Identical worker messages (field values/types, JSON-safe).
- NULL handling: `Row` gives `None` where pandas gave `NaN`/`NaT`. That's an improvement: a NULL campaign string was serialised as JSON `NaN` and seeded `"nan"`.
- `index` stays 0-based.
- No silent fallbacks.

**Task G1: tests first**
- `tests/test_crf_sql_params.py`:
  - Replace `_df_rows` with real `google.cloud.bigquery.Row(values, {"col": i, …})` fixtures (same data; NULLs as `None`). The missing-column case is a Row without `trend_context`.
  - `_orchestrator_serialize(datetime.fromisoformat(raw))` instead of `pd.Timestamp`.
  - Drop `import pandas`.
- `tests/test_crf_entrypoint.py`:
  - Empty-result tests: drop the `pd.DataFrame()` lines; a bare `MagicMock` result iterates empty.
  - Two-row tests: return `Row` objects through `bq.query.side_effect` (a separate result per call) or `result.return_value.__iter__.side_effect = lambda: iter(rows)`.
  - **Pitfall:** don't set `result.return_value = []`. `reap_stale_processing_rows` reads `.result().num_dml_affected_rows` on the same mock, swallows the error and hides it.
  - Add a regression guard like `tests/test_tools.py:936`: the fake job's `to_dataframe` raises `AssertionError("to_dataframe needs pandas; iterate rows")`.
  - Assert the dispatched JSON for a row with a NULL `brand` has `"brand": null`, not `NaN`.
- Run in the CRF venv: tests fail (they still call `to_dataframe`).

**Task G2: implement**
- `main.py`:
  - `rows = list(_run_query(bq_client, select_sql, select_params).result())`
  - `if not rows:` replaces `df.empty`
  - `for index, row in enumerate(rows):` replaces `df.iterrows()`
  - Remove `import pandas as pd` and the `pd.isna` block in `_optional_cell`. Keep the `datetime` check before the `date` check.
  - Fix the `:357`/`:367` comments and docstring ("BigQuery Row (or mapping)").
- `requirements.txt`: `google-cloud-bigquery==3.46.1` (no extra); drop `numpy`, `pytz`, `tzdata` and `python-dateutil`. bigquery requires dateutil transitively. Verify `pytz`/`tzdata` aren't needed by resolving `uv pip compile requirements.txt`.
- `pyproject.toml` dev group: remove the CRF-only `pandas`/`db-dtypes`/`pyarrow` lines and the stale comment. pandas stays via the `eval` group; db-dtypes/pyarrow go unless `uv lock` shows another user.
- Run `uv lock`. Root `requirements.txt` is unchanged (runtime set); confirm with the CI drift command.
- CLAUDE.md: update the Requirements line "plus pandas/pyarrow/db-dtypes for the CRF tests".

**Task G3: image check**
- `docker build -f cloud_functions/creative_fanout/Dockerfile -t crf:nopandas .`, then record the uncompressed size against 778 MB.
- Re-run the two local smoke probes from #357:
  - `FUNCTION_TARGET=crf_entrypoint` with an empty trigger → "nothing to process";
  - the worker with dataset `"evil; DROP"` + `agent_resource_id` → "Rejecting worker message".
- Add the plan copy. Commit.

## PR H — web build context hygiene
1. **Test first** (`tests/test_build_context.py`): `frontend/.dockerignore` lists `tsconfig.tsbuildinfo`, `next-env.d.ts`, `test-results` and `playwright-report`. These are machine-local or generated files that must not affect the image or its cache.
2. **Change:** append those entries, with a comment ("generated per checkout; uploading them invalidated the COPY . . cache → full next build").
3. **Verify `next-env.d.ts` is regenerated by `next build`:**
   - `docker build -f frontend/Dockerfile frontend` succeeds.
   - Two builds where only `frontend/tsconfig.tsbuildinfo` differs (touch/modify it) show `COPY . .` and `npm run build` as `CACHED` the second time (`docker buildx build --progress=plain`).
4. **Commit.**

## PR I — reproducible image digests
1. **Tests first:**
   - `tests/test_creative_fanout_image.py` (root cloudbuild) and `tests/test_bandit_cloudbuild.py`: every `docker buildx build` step uses `--output type=image,push=true,rewrite-timestamp=true` (no bare `--push`) and passes `--build-arg SOURCE_DATE_EPOCH=${_SOURCE_DATE_EPOCH}`.
   - Both configs define `_SOURCE_DATE_EPOCH: "0"` as the default substitution.
2. **Change** (`cloudbuild.yaml` build-api/web/crf, `cloudbuild.bandit.yaml` build-bandit):
   - Swap `--push` for the `--output …` form and add the build arg.
   - Keep `--provenance=false`, the cache flags and `--metadata-file`. Confirm the digest-extraction `sed` still finds `containerimage.digest`.
   - **Why `_SOURCE_DATE_EPOCH`:** `--no-source`/uploaded builds have no `.git`, so the caller passes `$(git log -1 --format=%ct)`. The default `0` is still reproducible.
   - Header comments explain both.
3. **README:** add `_SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)` to the two submit commands. Use the `^;^` delimiter form if the command has more than one substitution, since gcloud splits on commas.
4. **Real check, the acceptance test:**
   - Submit `cloudbuild.yaml` **twice at the same commit** from the same checkout. The api, web and crf digests must be identical across the two runs.
   - Repeat for `cloudbuild.bandit.yaml`.
   - Record warm times against today's (~30s app, ~17s bandit).
   - If an image still drifts, inspect its layers (`docker buildx imagetools inspect --raw`) and fix the cause (e.g. Next's random build ID → `generateBuildId` from `SOURCE_DATE_EPOCH`), or document it as a known exception.
5. **Commit.**

---

## Execution and deploy round
1. **t=0:** dispatch F, G, H and I implementers in parallel. Review each with code-review-and-quality (five axes; tests reviewed first; for G, a mutation check such as dropping `if not rows` should fail a test). Merge as CI goes green, in any order.
2. **After all four merge** (main = HEAD):
   - **a.** One `cloudbuild.yaml` build with `_SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)`, then a second identical submit. That verifies **I** on main, gives **H** a cross-checkout cache hit, and produces the new CRF digest for **G**.
   - **b. G deploy** (as in #357):
     1. Worker first, `--image $CRF_IMAGE --no-traffic --tag crfcheck`. Probe with an impersonated ID token (`gcloud auth print-identity-token --impersonate-service-account=934903580331-compute@developer.gserviceaccount.com --audiences=<service URL> --include-email`): a rejected message, expect 200 "Rejecting worker message". Route 100%, remove the tag.
     2. Orchestrator, same steps, with the empty-trigger probe ("nothing to process").
     3. Diff the `describe` output before/after: only the image and revision change.
   - **c. F** is verified at the next engine deploy (queue ≈ 0s instead of ~50s). It is cheap to check with `gcloud builds describe <id> --format='value(createTime,startTime)'`, and no engine redeploy is needed now.
   - **d.** Do not redeploy api, web or share: their code is unchanged. Update the runbook memory (CRF revisions + new image size, reproducible digests).

**Estimated total:** about 45–60 min for the parallel PRs (G is the longest) plus about 15 min for the round.

## Verification
- **F:** the YAML test passes; the next agent build shows queue ≈ 0s.
- **G:**
  - CRF venv tests and the full suite pass; the new NULL-brand test proves `null` instead of `NaN`.
  - The image shrinks by ~300 MB uncompressed.
  - Live probes on both services are handled; the services' env, scaling and triggers are unchanged.
  - Optional: a real batch run (one `max_rows: 1` trigger, costs one creative run).
- **H:** the second local build is fully `CACHED`; a cross-checkout Cloud Build hits the web cache.
- **I:** two same-commit builds give byte-identical api, web, crf and bandit digests.
