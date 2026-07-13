# Persist Creative-Eval Scores to BigQuery (Option 4) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the `CreativeEvaluationReport` non-terminal by writing a per-run summary row (pass rate, average scores, weakest dimensions, link to the full GCS report) into a new BigQuery table, joinable to the existing `trend_creatives` row.

**Architecture:** A pure, unit-testable row-builder function (`build_eval_bq_row`) converts the eval report dict + campaign context into a flat row; a thin ADK tool (`write_eval_report_to_bq`) streams that row via `bigquery.Client.insert_rows_json` (no SQL string-building → no injection, correct numeric types). `write_trends_to_bq` stashes its generated `uuid` into session state so the eval row can foreign-key back to the creative row. The tool is wired as the final step of both the `creative_agent` and `interactive_creative` orchestrators. A new `BQ_TABLE_EVALS` env var is threaded through the shared config and the deploy env-var dict (the same drift-prevention discipline the repo already enforces).

**Tech Stack:** Python 3.13, Google ADK 2.4, `google-cloud-bigquery`, pytest, uv, ruff. BigQuery dataset stays `trend_trawler` (the data store name is deliberately preserved — do NOT rename it).

**Why this design (context for the implementer):**
- Today the report is **write-only**: `creative_eval/agent.py` writes `state["creative_evaluation_report"]`, `creative_agent/tools.py:save_eval_report_to_gcs` dumps it to GCS JSON, and nothing ever reads it back. `write_trends_to_bq` stores campaign metadata but **zero scores**.
- The report's `summary` block (`creative_eval/schemas.py:57-75` — `EvaluationSummary`) already contains exactly the queryable aggregates we want: `overall_pass_rate`, `avg_ad_copy_score`, `avg_visual_score`, the pass/total counts, and `weakest_dimensions`.
- We persist a **run-level summary row** (one row per creative_agent run), not per-creative-per-dimension. That is the "cheap foundation" scope. Per-creative / per-dimension granularity is a deliberate future extension for Option 2 (the quality flywheel) and is out of scope here.
- Streaming insert (`insert_rows_json`) is chosen over the DML `INSERT ... VALUES` f-string used by `write_trends_to_bq` because the eval row is numeric-heavy; a row dict is safer and far easier to unit-test than interpolated SQL. This is a deliberate, noted departure from the neighbouring function's style.

**Preconditions:**
- Branch off fresh `main` first: `git checkout main && git pull --ff-only && git checkout -b feat/persist-eval-scores-bq`.
- `export PATH="$HOME/.local/bin:$PATH"` before any `uv`/`uvx` call (per CODE_STANDARDS.md + project memory).
- The full `uv run pytest tests/` suite requires GCP credentials (module-level `genai.Client`). If creds are unavailable in the working env, run the **targeted** test files named in each task (they import only pure functions) and note that the full-suite gate must be run where creds exist.

---

## Data model

New table `creative_evals` (env var `BQ_TABLE_EVALS`, default `'creative_evals'`), in the existing `trend_trawler` dataset. One row per run.

| Column | Type | Source |
| --- | --- | --- |
| `uuid` | STRING | new 8-char id for this eval row |
| `creative_uuid` | STRING | `state["creative_row_uuid"]` — FK to `trend_creatives.uuid` (empty string if absent) |
| `datetime` | DATETIME | run timestamp, `America/New_York`, `'YYYY-MM-DD HH:MM:SS'` |
| `target_trend` | STRING | `state["target_search_trends"]` |
| `brand` | STRING | `state["brand"]` |
| `target_product` | STRING | `state["target_product"]` |
| `overall_pass_rate` | FLOAT | `summary.overall_pass_rate` |
| `total_ad_copies` | INTEGER | `summary.total_ad_copies` |
| `ad_copies_passed` | INTEGER | `summary.ad_copies_passed` |
| `avg_ad_copy_score` | FLOAT | `summary.avg_ad_copy_score` |
| `total_visual_concepts` | INTEGER | `summary.total_visual_concepts` |
| `visual_concepts_passed` | INTEGER | `summary.visual_concepts_passed` |
| `avg_visual_score` | FLOAT | `summary.avg_visual_score` |
| `weakest_dimensions` | STRING | `summary.weakest_dimensions` joined with `,` |
| `eval_report_gcs_uri` | STRING | `state["eval_report_gcs_uri"]` — pointer to the full JSON (empty string if absent) |

`weakest_dimensions` is stored comma-joined (query later with `SPLIT(weakest_dimensions, ',')` + `UNNEST`) to keep the `bq mk` DDL a one-liner. `eval_report_gcs_uri` links each summary row to the full per-dimension report already living in GCS, so nothing is lost by keeping this table flat.

---

## Task 1: Thread `BQ_TABLE_EVALS` through config + deploy env

**Files:**
- Modify: `agent_common/config.py:81` (after `BQ_TABLE_ALL_TRENDS`)
- Modify: `.env.example:38` (after `BQ_TABLE_ALL_TRENDS='all_trends'`)
- Modify: `deployment/deploy_agent.py:47` (add to `ENV_VAR_DICT`)
- Test: `tests/test_deploy_utils.py:63` (add to `EXPECTED_ENV_VAR_KEYS`)

**Step 1: Write the failing test**

Add `"BQ_TABLE_EVALS"` to the `EXPECTED_ENV_VAR_KEYS` list in `tests/test_deploy_utils.py` (currently ends at `"BQ_TABLE_ALL_TRENDS",` on line 62):

```python
EXPECTED_ENV_VAR_KEYS = [
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT_NUMBER",
    "GOOGLE_CLOUD_STORAGE_BUCKET",
    "BUCKET",
    "BQ_PROJECT_ID",
    "BQ_DATASET_ID",
    "BQ_TABLE_TARGETS",
    "BQ_TABLE_CREATIVES",
    "BQ_TABLE_ALL_TRENDS",
    "BQ_TABLE_EVALS",
]
```

`test_all_expected_keys_present` asserts every key is present in `.env.example`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_utils.py::TestEnvVarDict::test_all_expected_keys_present -v`
Expected: FAIL — `Missing BQ_TABLE_EVALS in .env.example`.

**Step 3: Write minimal implementation**

Add to `.env.example` immediately after line 38 (`BQ_TABLE_ALL_TRENDS='all_trends'`):

```bash
BQ_TABLE_EVALS='creative_evals'
```

Add to `agent_common/config.py` immediately after line 81 (`BQ_TABLE_ALL_TRENDS = os.environ.get("BQ_TABLE_ALL_TRENDS")`):

```python
    BQ_TABLE_EVALS = os.environ.get("BQ_TABLE_EVALS")
```

Add to `deployment/deploy_agent.py` `ENV_VAR_DICT` immediately after line 47 (`"BQ_TABLE_ALL_TRENDS": os.getenv("BQ_TABLE_ALL_TRENDS"),`):

```python
    "BQ_TABLE_EVALS": os.getenv("BQ_TABLE_EVALS"),
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deploy_utils.py -v`
Expected: PASS (all `TestEnvVarDict` + `TestAgentExtraPackages` tests green).

**Step 5: Commit**

```bash
git add agent_common/config.py .env.example deployment/deploy_agent.py tests/test_deploy_utils.py
git commit -m "feat(config): add BQ_TABLE_EVALS env var for eval-score table"
```

---

## Task 2: Pure row-builder `build_eval_bq_row`

This is the core logic and the only part with branching — build it test-first, decoupled from the BigQuery client.

**Files:**
- Modify: `creative_agent/tools.py` (add `build_eval_bq_row` near `write_trends_to_bq`, ~line 1200)
- Test: `tests/test_tools.py` (append a new `TestBuildEvalBqRow` class)

**Step 1: Write the failing test**

Append to `tests/test_tools.py`:

```python
# --- build_eval_bq_row (pure eval-report -> BQ row) ---
SAMPLE_REPORT = {
    "brand": "PRS Guitars",
    "target_product": "SE CE24",
    "target_search_trend": "tswift engaged",
    "summary": {
        "total_ad_copies": 4,
        "ad_copies_passed": 3,
        "avg_ad_copy_score": 0.82,
        "total_visual_concepts": 4,
        "visual_concepts_passed": 2,
        "avg_visual_score": 0.71,
        "overall_pass_rate": 0.625,
        "weakest_dimensions": ["stopping_power", "cta_strength"],
    },
}


class TestBuildEvalBqRow:
    def _row(self, **overrides):
        from creative_agent.tools import build_eval_bq_row

        kwargs = dict(
            report=SAMPLE_REPORT,
            eval_uuid="ev123456",
            creative_uuid="cr789012",
            now_datetime="2026-07-13 10:30:00",
            target_trend="tswift engaged",
            brand="PRS Guitars",
            target_product="SE CE24",
            eval_report_gcs_uri="gs://bucket/run/creative_output/creative_eval_report.json",
        )
        kwargs.update(overrides)
        return build_eval_bq_row(**kwargs)

    def test_maps_summary_fields(self):
        row = self._row()
        assert row["overall_pass_rate"] == 0.625
        assert row["total_ad_copies"] == 4
        assert row["ad_copies_passed"] == 3
        assert row["avg_visual_score"] == 0.71

    def test_weakest_dimensions_comma_joined(self):
        row = self._row()
        assert row["weakest_dimensions"] == "stopping_power,cta_strength"

    def test_carries_ids_and_link(self):
        row = self._row()
        assert row["uuid"] == "ev123456"
        assert row["creative_uuid"] == "cr789012"
        assert row["datetime"] == "2026-07-13 10:30:00"
        assert row["eval_report_gcs_uri"].endswith("creative_eval_report.json")

    def test_numeric_coercion(self):
        # Judge/JSON round-trips can hand back ints-as-strings; row must be typed.
        report = {**SAMPLE_REPORT, "summary": {**SAMPLE_REPORT["summary"],
                  "total_ad_copies": "4", "overall_pass_rate": "0.5"}}
        row = self._row(report=report)
        assert row["total_ad_copies"] == 4 and isinstance(row["total_ad_copies"], int)
        assert row["overall_pass_rate"] == 0.5 and isinstance(row["overall_pass_rate"], float)

    def test_empty_weakest_dimensions(self):
        report = {**SAMPLE_REPORT, "summary": {**SAMPLE_REPORT["summary"], "weakest_dimensions": []}}
        assert self._row(report=report)["weakest_dimensions"] == ""

    def test_row_keys_match_table_schema(self):
        # Guard: row keys must equal the creative_evals column set exactly.
        expected = {
            "uuid", "creative_uuid", "datetime", "target_trend", "brand",
            "target_product", "overall_pass_rate", "total_ad_copies",
            "ad_copies_passed", "avg_ad_copy_score", "total_visual_concepts",
            "visual_concepts_passed", "avg_visual_score", "weakest_dimensions",
            "eval_report_gcs_uri",
        }
        assert set(self._row().keys()) == expected
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools.py::TestBuildEvalBqRow -v`
Expected: FAIL — `ImportError: cannot import name 'build_eval_bq_row'`.

**Step 3: Write minimal implementation**

Add to `creative_agent/tools.py`, immediately before `write_trends_to_bq` (line 1202):

```python
def build_eval_bq_row(
    *,
    report: dict,
    eval_uuid: str,
    creative_uuid: str,
    now_datetime: str,
    target_trend: str,
    brand: str,
    target_product: str,
    eval_report_gcs_uri: str,
) -> dict:
    """Flatten a CreativeEvaluationReport dict into one BigQuery row.

    Pure (no client, no wall-clock) so it is unit-testable. Numeric fields are
    coerced because the judge's JSON round-trip can hand back stringified numbers.
    """
    summary = report.get("summary", {})
    weakest = summary.get("weakest_dimensions") or []
    return {
        "uuid": eval_uuid,
        "creative_uuid": creative_uuid,
        "datetime": now_datetime,
        "target_trend": target_trend,
        "brand": brand,
        "target_product": target_product,
        "overall_pass_rate": float(summary.get("overall_pass_rate", 0.0)),
        "total_ad_copies": int(summary.get("total_ad_copies", 0)),
        "ad_copies_passed": int(summary.get("ad_copies_passed", 0)),
        "avg_ad_copy_score": float(summary.get("avg_ad_copy_score", 0.0)),
        "total_visual_concepts": int(summary.get("total_visual_concepts", 0)),
        "visual_concepts_passed": int(summary.get("visual_concepts_passed", 0)),
        "avg_visual_score": float(summary.get("avg_visual_score", 0.0)),
        "weakest_dimensions": ",".join(weakest),
        "eval_report_gcs_uri": eval_report_gcs_uri,
    }
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools.py::TestBuildEvalBqRow -v`
Expected: PASS (6 tests).

**Step 5: Commit**

```bash
git add creative_agent/tools.py tests/test_tools.py
git commit -m "feat(tools): add pure build_eval_bq_row eval-report flattener"
```

---

## Task 3: `write_eval_report_to_bq` tool + FK uuid stash

**Files:**
- Modify: `creative_agent/tools.py:1217` (stash uuid in `write_trends_to_bq`)
- Modify: `creative_agent/tools.py` (add `write_eval_report_to_bq` after `write_trends_to_bq`, ~line 1269)
- Modify: `creative_agent/tools.py:1-9` (add `import datetime` and `from zoneinfo import ZoneInfo`)
- Test: `tests/test_tools.py` (append `TestWriteTrendsUuidStash`)

**Step 1: Write the failing test**

The tool's own infra call (`insert_rows_json`) is not unit-tested here (matches the repo: `write_trends_to_bq` / `save_eval_report_to_gcs` have no client-level unit tests; the pure builder from Task 2 carries the logic coverage). We DO test the small, pure state contract the tool depends on: `write_trends_to_bq` must expose its generated uuid so the eval row can reference it.

Append to `tests/test_tools.py`:

```python
class TestWriteTrendsUuidStash:
    def test_stashes_creative_row_uuid(self, monkeypatch):
        """write_trends_to_bq must record its generated uuid in state so the
        eval row can foreign-key back to the creative row."""
        import creative_agent.tools as t

        class _Job:
            errors = None
            job_id = "j1"
            num_dml_affected_rows = 1

            def result(self):
                return None

        class _BQ:
            def query(self, sql):
                return _Job()

        monkeypatch.setattr(t, "_get_bigquery_client", lambda: _BQ())

        ctx = MockToolContext()
        ctx.state.update(
            {
                "gcs_folder": "2026_07_13_run",
                "agent_output_dir": "creative_output",
                "target_search_trends": "tswift engaged",
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_points": "wide tonal range",
            }
        )
        result = t.write_trends_to_bq(ctx)
        assert result["status"] == "success"
        assert ctx.state["creative_row_uuid"]  # non-empty 8-char id
        assert len(ctx.state["creative_row_uuid"]) == 8
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools.py::TestWriteTrendsUuidStash -v`
Expected: FAIL — `KeyError: 'creative_row_uuid'`.

**Step 3: Write minimal implementation**

In `creative_agent/tools.py`, add the imports (after line 8, `import json`):

```python
import datetime
from zoneinfo import ZoneInfo
```

In `write_trends_to_bq`, immediately after line 1217 (`unique_id = f"{str(uuid.uuid4())[:8]}"`), stash it:

```python
    tool_context.state["creative_row_uuid"] = unique_id
```

Add the new tool after `write_trends_to_bq` (after line 1268):

```python
def write_eval_report_to_bq(tool_context: ToolContext) -> dict:
    """Write a one-row creative-evaluation summary to BigQuery.

    Reads the report the evaluator stored in state, flattens it via
    build_eval_bq_row, and streams it to the ``BQ_TABLE_EVALS`` table. The row
    foreign-keys to the trend_creatives row via ``creative_row_uuid`` and links
    to the full per-dimension JSON already saved in GCS.
    """
    report = tool_context.state.get("creative_evaluation_report")
    if not report:
        return {
            "status": "error",
            "message": "No creative_evaluation_report found in session state.",
        }

    now_dt = datetime.datetime.now(ZoneInfo("America/New_York")).replace(
        tzinfo=None
    ).isoformat(sep=" ", timespec="seconds")

    row = build_eval_bq_row(
        report=report,
        eval_uuid=str(uuid.uuid4())[:8],
        creative_uuid=tool_context.state.get("creative_row_uuid", ""),
        now_datetime=now_dt,
        target_trend=tool_context.state.get("target_search_trends", ""),
        brand=tool_context.state.get("brand", ""),
        target_product=tool_context.state.get("target_product", ""),
        eval_report_gcs_uri=tool_context.state.get("eval_report_gcs_uri", ""),
    )

    table_id = f"{config.BQ_PROJECT_ID}.{config.BQ_DATASET_ID}.{config.BQ_TABLE_EVALS}"
    try:
        bq_client = _get_bigquery_client()
        errors = bq_client.insert_rows_json(table_id, [row])
        if errors:
            logging.error(f"Eval-row insert into {table_id} failed: {errors}")
            raise RuntimeError(f"BigQuery insert returned errors: {errors}")
        logging.info(f"Inserted eval summary row {row['uuid']} into {table_id}.")
        return {"status": "success", "eval_uuid": row["uuid"]}
    except Exception as e:
        # Propagate so ADK 2.0 RetryConfig can retry transient infra failures.
        logging.exception(f"Failed to insert eval row to bq: {e}")
        raise
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools.py -v`
Expected: PASS (all classes including the two new ones).

**Step 5: Commit**

```bash
git add creative_agent/tools.py tests/test_tools.py
git commit -m "feat(tools): add write_eval_report_to_bq tool + FK uuid stash"
```

---

## Task 4: Wire the tool into both orchestrators

**Files:**
- Modify: `creative_agent/agent.py:819-821` (AVAILABLE_TOOLS), `:847-849` (WORKFLOW), `:866` (tools list)
- Modify: `interactive_creative/agent.py:42-44` + `:72-74` (instruction), `:92` (tools list)
- Test: `tests/test_pipeline_structure.py` (run existing; add a presence assertion if the file already introspects the root tool set — see Step 1)

**Step 1: Write / adjust the test**

Inspect `tests/test_pipeline_structure.py` for an existing test that lists the root agent's tool names. If one exists, add `"write_eval_report_to_bq"` to its expected set. If none exists, add this focused test:

```python
class TestEvalBqWiring:
    def test_creative_agent_registers_eval_bq_tool(self):
        from creative_agent.agent import root_agent

        names = {getattr(t, "__name__", getattr(t, "name", "")) for t in root_agent.tools}
        assert "write_eval_report_to_bq" in names
```

(Adjust the attribute access to match how the file already reads tool identifiers — plain function tools expose `__name__`, `AgentTool`s expose `.name`.)

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline_structure.py -v`
Expected: FAIL — `write_eval_report_to_bq` not in the tool set.

**Step 3: Write minimal implementation**

`creative_agent/agent.py` — add to the `tools=[...]` list after line 866 (`tools.write_trends_to_bq,`):

```python
        tools.write_eval_report_to_bq,
```

Extend the instruction so the LLM actually calls it. Change line 821 (AVAILABLE_TOOLS item 9) region to append a 10th tool, and add a WORKFLOW step after line 849. Concretely, in `<AVAILABLE_TOOLS>` add:

```
    10. Use the `write_eval_report_to_bq` tool to log the evaluation summary (pass rate, average scores, weakest dimensions) to BigQuery.
```

In `<WORKFLOW>`, renumber so the eval-BQ write is the final persistence step (after `write_trends_to_bq`, before the Display action):

```
    9. Then, call the `write_eval_report_to_bq` tool to log the evaluation summary to BigQuery for analytics.
```

(Shift the existing "Action 1: Display…" numbering accordingly — it becomes step 10.)

`interactive_creative/agent.py` — mirror the same edits: add a tool bullet in its AVAILABLE_TOOLS list (near line 44), a workflow step in its instruction (near line 74), and add `tools.write_eval_report_to_bq,` to the `tools=[...]` list after line 92.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline_structure.py -v && uv run pytest tests/ -q`
Expected: PASS. (If creds are unavailable for the full suite, run at least `tests/test_pipeline_structure.py`, `tests/test_tools.py`, `tests/test_deploy_utils.py`.)

**Step 5: Commit**

```bash
git add creative_agent/agent.py interactive_creative/agent.py tests/test_pipeline_structure.py
git commit -m "feat(agents): call write_eval_report_to_bq as final persistence step"
```

---

## Task 5: Update the ADK eval rubric trajectory

The `creative_agent` ADK eval grades the tool trajectory; adding a pipeline step means the rubric must expect it, or the trajectory rubric silently drifts from reality.

**Files:**
- Modify: `tests/eval/creative_eval_config.json:30` (`pipeline_execution` text), `:36` (`all_tools_used` text)

**Step 1: (No unit test — this is eval config.)** Verify current text with:

Run: `grep -n "write_trends_to_bq" tests/eval/creative_eval_config.json`
Expected: two hits (pipeline_execution ordering + all_tools_used list).

**Step 2: Edit both rubric strings**

In `pipeline_execution` (line 30), change the tail `…save_creative_gallery_html, write_trends_to_bq).` to:
`…save_creative_gallery_html, write_trends_to_bq, write_eval_report_to_bq).`

In `all_tools_used` (line 36), append `write_eval_report_to_bq` to the required-tools list the same way.

**Step 3: Validate JSON**

Run: `uv run python -c "import json; json.load(open('tests/eval/creative_eval_config.json')); print('ok')"`
Expected: `ok`.

**Step 4: Commit**

```bash
git add tests/eval/creative_eval_config.json
git commit -m "test(eval): expect write_eval_report_to_bq in creative_agent trajectory"
```

> Note: the live `adk eval` run (real APIs, ~5 min/case, needs creds + a deployed-agent-free local run) is a **manual** verification, not part of the no-creds gate. Run it where creds exist:
> `PYTHONPATH="$PWD" uv run adk eval creative_agent tests/eval/evalsets/creative_agent_evalset.json --config_file_path=tests/eval/creative_eval_config.json --print_detailed_results`

---

## Task 6: Docs + BigQuery DDL

**Files:**
- Modify: `README.md:173-189` (the "create the target-trends and creatives tables" `<details>` block) + `README.md:143`/`:471` area (env + repo mentions)
- Modify: `deployment/README.md` (any `bq mk` / dataset-table section — grep first)
- Modify: `CLAUDE.md` (Data Flow section, ~line 168: BigQuery table list)

**Step 1: Add the DDL** to the README `<details>` block (after the creatives-table `bq mk`):

```bash
# creative evaluation summaries (one row per run; links to trend_creatives.uuid)
bq mk \
 -t \
 $BQ_PROJECT_ID:$BQ_DATASET_ID.$BQ_TABLE_EVALS \
 uuid:STRING,creative_uuid:STRING,datetime:DATETIME,target_trend:STRING,brand:STRING,target_product:STRING,overall_pass_rate:FLOAT,total_ad_copies:INTEGER,ad_copies_passed:INTEGER,avg_ad_copy_score:FLOAT,total_visual_concepts:INTEGER,visual_concepts_passed:INTEGER,avg_visual_score:FLOAT,weakest_dimensions:STRING,eval_report_gcs_uri:STRING
```

**Step 2: Add the env line** to the README `.env` example block (after `BQ_TABLE_ALL_TRENDS='all_trends'`, ~line 143):

```bash
BQ_TABLE_EVALS='creative_evals'
```

**Step 3: Update CLAUDE.md** — in the Data Flow → BigQuery bullet (~line 168), add `creative_evals` (per-run evaluation summaries) to the listed tables, noting it joins `trend_creatives` via `creative_uuid` and links to the full report JSON in GCS.

**Step 4: Update deployment/README.md** — grep for the dataset/table setup and add the `creative_evals` table + `BQ_TABLE_EVALS` where the other `BQ_TABLE_*` vars are documented:

Run: `grep -n "BQ_TABLE\|bq mk\|trend_creatives" deployment/README.md`

**Step 5: Commit**

```bash
git add README.md CLAUDE.md deployment/README.md
git commit -m "docs: document creative_evals table + BQ_TABLE_EVALS"
```

---

## Verification (no-creds gate)

```bash
export PATH="$HOME/.local/bin:$PATH"
uv run pytest tests/test_tools.py tests/test_deploy_utils.py tests/test_pipeline_structure.py -v
uvx ruff check . && uvx ruff format --check .
uv run python -c "import json; json.load(open('tests/eval/creative_eval_config.json')); print('eval config ok')"
# full suite (where GCP creds exist):
uv run pytest tests/ -q
# import smoke: the new tool is exported and wired
uv run python -c "from creative_agent import tools; assert hasattr(tools, 'write_eval_report_to_bq'); print('tool ok')"
```

## Verification (with-creds, manual — proves the row actually lands)

1. `bq mk` the `creative_evals` table (DDL above).
2. Run `creative_agent` end-to-end locally (`uv run adk web .`) or via `deployment/test_deployment.py --agent=creative_agent`.
3. `bq query --nouse_legacy_sql 'SELECT uuid, creative_uuid, overall_pass_rate, weakest_dimensions FROM \`$BQ_PROJECT_ID.trend_trawler.creative_evals\` ORDER BY datetime DESC LIMIT 5'` → confirm the new row, non-null scores, and that `creative_uuid` matches a `trend_creatives.uuid`.

## Sequencing

Tasks 1→2→3→4→5→6, one commit each, suite green after each. Single PR
**"feat: persist creative-eval scores to BigQuery (Option 4)"** — opened only when the user asks.

## Out of scope (deliberately deferred)

- **Per-creative / per-dimension rows** (finer granularity for the flywheel) — Option 2.
- **Reading the scores back to drive regeneration** — Option 1 (regenerate-on-low-score).
- **Injecting human checkpoint feedback into generators** — Option 3.
- Renaming the `trend_trawler` dataset (it is the data store; keep it).
- Backfilling historical GCS eval JSONs into the new table.
```
