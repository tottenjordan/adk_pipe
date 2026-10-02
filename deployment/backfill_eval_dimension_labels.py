"""Backfill `creative_evals.weakest_dimension_labels` for rows written before it existed.

`creative_agent.bq_tools.build_eval_bq_row` now writes a readable
`weakest_dimension_labels` column ("Trend connection, Copy quality") next to the
snake_case `weakest_dimensions`. Rows from before that change have it NULL; this
script fills them with the same labels (`creative_eval.dimensions`, mirrored from
the frontend), preserving the original dimension order. Unknown keys fall back to
sentence case, exactly like `dimension_label`. Idempotent: only NULL rows are
touched, so re-running is a no-op.

Run the `ALTER TABLE ... ADD COLUMN IF NOT EXISTS weakest_dimension_labels STRING`
migration first (deployment/README.md).

Usage:
    # dry run: print SQL, bytes it would scan, and how many rows are NULL
    uv run python deployment/backfill_eval_dimension_labels.py \\
        --table=<PROJECT>.trend_trawler.creative_evals
    # apply
    uv run python deployment/backfill_eval_dimension_labels.py \\
        --table=<PROJECT>.trend_trawler.creative_evals --execute
"""

import os
import re
import sys

from absl import app, flags

# Add the project root to sys.path (same as deploy_agent.py) so the flat packages
# import when run as `python deployment/<script>.py`.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from creative_eval.dimensions import DIMENSION_LABELS  # noqa: E402

FLAGS = flags.FLAGS
flags.DEFINE_string(
    "table", None, "Fully-qualified table: <project>.<dataset>.creative_evals"
)
flags.DEFINE_bool(
    "execute", False, "Run the UPDATE (default: dry run + affected-row count only)."
)

_TABLE_RE = re.compile(r"[\w-]+\.\w+\.\w+", re.ASCII)


def _validate_table(table: str) -> str:
    """Reject anything but `project.dataset.table` (it is interpolated into SQL)."""
    if not _TABLE_RE.fullmatch(table):
        raise ValueError(f"--table must be <project>.<dataset>.<table>, got {table!r}")
    return table


def _sql_string_literal(value: str) -> str:
    """Single-quoted BigQuery string literal (backslash escaping, GoogleSQL)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_backfill_sql(table: str) -> str:
    """UPDATE filling NULL `weakest_dimension_labels` from `weakest_dimensions`.

    Mirrors `creative_eval.dimensions.dimension_labels_csv`: known keys map to
    their label; unknown keys become sentence case (runs of `_` -> space, trim,
    collapse whitespace, lowercase, capitalize first char); joined with ", " in
    the stored order. Empty/NULL `weakest_dimensions` -> ''.
    """
    _validate_table(table)
    whens = "\n".join(
        f"        WHEN {_sql_string_literal(k)} THEN {_sql_string_literal(v)}"
        for k, v in DIMENSION_LABELS.items()
    )
    words = "TRIM(REGEXP_REPLACE(REGEXP_REPLACE(d, r'_+', ' '), r'\\s+', ' '))"
    fallback = f"CONCAT(UPPER(SUBSTR({words}, 1, 1)), LOWER(SUBSTR({words}, 2)))"
    return f"""UPDATE `{table}`
SET weakest_dimension_labels = IFNULL(
  (
    SELECT STRING_AGG(
      CASE d
{whens}
        ELSE {fallback}
      END,
      ', ' ORDER BY off
    )
    FROM UNNEST(SPLIT(weakest_dimensions, ',')) AS d WITH OFFSET AS off
    WHERE TRIM(d) != ''
  ),
  ''
)
WHERE weakest_dimension_labels IS NULL
"""


def main(argv: list[str]) -> None:
    del argv
    from google.cloud import bigquery

    table = _validate_table(FLAGS.table)
    sql = build_backfill_sql(table)
    # Bill the job to the table's own project (it may differ from the .env one).
    client = bigquery.Client(project=table.split(".", 1)[0])

    if not FLAGS.execute:
        print(sql)
        dry = client.query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        )
        print(f"Dry run OK: would process {dry.total_bytes_processed} bytes.")
        count_sql = (
            f"SELECT COUNT(*) AS n FROM `{table}` "
            "WHERE weakest_dimension_labels IS NULL"
        )
        n = next(iter(client.query(count_sql).result()))["n"]
        print(f"Rows to backfill (weakest_dimension_labels IS NULL): {n}")
        print("Re-run with --execute to apply.")
        return

    job = client.query(sql)
    job.result()
    print(f"Backfill done: {job.num_dml_affected_rows} rows updated in {table}.")


if __name__ == "__main__":
    flags.mark_flag_as_required("table")
    app.run(main)
