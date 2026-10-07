"""Print the judge-human calibration report for the human creative ratings.

Reads ``creative_ratings`` from BigQuery (``BQ_PROJECT_ID`` / ``BQ_DATASET_ID`` /
``BQ_TABLE_RATINGS``, all users unless ``--user``) or a CSV export of that table,
and prints the same report as ``GET /ratings/{user}/calibration``
(``runserver/calibration.py``): agreement + Cohen's kappa of the judge's pass
verdict (and its deterministic gates, when recorded) against the human verdict,
and Spearman's rho between the judge's overall score and the human 1-5 score,
overall and per kind. Protocol: docs/notes/judge-calibration.md.

By default only ratings whose judge fields came from the run's GCS report
(``judge_source = 'gcs'``) count: the session-state copy can be seeded by a client
and would skew an all-users report. ``--include-all`` counts every rating.

    set -a && source .env && set +a
    uv run python scripts/eval_calibration.py                      # BigQuery, all users
    uv run python scripts/eval_calibration.py --user me@example.com
    uv run python scripts/eval_calibration.py --csv ratings.csv --json
    uv run python scripts/eval_calibration.py --include-all        # incl. state-sourced
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # run as a script: make the flat packages importable
    sys.path.insert(0, str(ROOT))

from runserver.calibration import calibration_report, format_report  # noqa: E402


def read_csv(path: Path, user: str | None = None) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if user:
        rows = [r for r in rows if (r.get("user_id") or "").strip().lower() == user]
    return rows


TRUSTED_SOURCE = "gcs"


def trusted_only(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ratings whose judge fields came from the run's GCS report."""
    return [r for r in rows if (r.get("judge_source") or "").strip() == TRUSTED_SOURCE]


def read_bigquery(user: str | None = None) -> list[dict[str, Any]]:
    from google.cloud import bigquery

    from agent_common.clients import get_bigquery_client
    from runserver.ratings_store import build_calibration_sql, table_name

    sql, params = build_calibration_sql(table_name(), user)
    job = get_bigquery_client().query(
        sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
    )
    return [dict(r.items()) for r in job.result()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--csv", type=Path, help="CSV export of creative_ratings")
    parser.add_argument("--user", help="only this user's ratings (email)")
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="also count ratings whose judge fields came from session state "
        "(client-seedable) or are missing; default: judge_source='gcs' only",
    )
    args = parser.parse_args(argv)
    user = args.user.strip().lower() if args.user else None
    rows = read_csv(args.csv, user) if args.csv else read_bigquery(user)
    kept = rows if args.include_all else trusted_only(rows)
    report = calibration_report(kept)
    report["excluded_untrusted"] = len(rows) - len(kept)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))
        if report["excluded_untrusted"]:
            print(
                f"(excluded {report['excluded_untrusted']} ratings without a GCS-sourced "
                "judge verdict; --include-all counts them)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
