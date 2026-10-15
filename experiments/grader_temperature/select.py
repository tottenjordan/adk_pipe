"""Choose the human-rated ``creative_agent`` sessions the harness re-grades.

Reads ``creative_ratings`` (parameterised query; never the free-text ``note``),
keeps sessions with both an ad-copy and a visual rating, and picks ``--n``
deterministically: most recent first (ties broken by a ``--seed``-keyed hash),
stratified so the set holds at least ``--min-fails`` human fails (kappa needs
both classes). Writes ``data/selection.json`` (session ids, user ids, rating
rows) and prints the choice.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.grader_temperature.select \\
      --n 10 --seed 20261015 [--exclude <sid>,<sid>]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import APP_NAME, SELECTION_PATH, write_json

DEFAULT_TABLE = "hybrid-vertex.trend_trawler.creative_ratings"
DEFAULT_N = 10
DEFAULT_SEED = 20261015
MIN_FAILS = 12
QUERY_LIMIT = 5000

COLUMNS = (
    "rating_id",
    "session_id",
    "app_name",
    "creative_key",
    "kind",
    "user_id",
    "verdict",
    "score",
    "judge_overall",
    "judge_passed",
    "judge_gates_passed",
    "judge_model",
    "judge_source",
    "judge_version",
    "fail_reasons",
    "created_at",
)


def build_query(table: str) -> str:
    """The parameterised SELECT (``@app_name``, ``@limit``); ``table`` is ours."""
    if not all(
        part.replace("-", "").replace("_", "").isalnum() for part in table.split(".")
    ):
        raise ValueError(f"invalid table name {table!r}")
    return (
        f"SELECT {', '.join(COLUMNS)} FROM `{table}` "
        "WHERE app_name = @app_name AND verdict IN ('pass', 'fail') "
        "ORDER BY created_at DESC LIMIT @limit"
    )


def fetch_rows(client: Any, table: str = DEFAULT_TABLE) -> list[dict[str, Any]]:
    """The rating rows as plain dicts (``client`` = a ``bigquery.Client``)."""
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("app_name", "STRING", APP_NAME),
            bigquery.ScalarQueryParameter("limit", "INT64", QUERY_LIMIT),
        ]
    )
    rows = client.query_and_wait(build_query(table), job_config=job_config)
    return [_plain(dict(row.items())) for row in rows]


def _plain(row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime.datetime | datetime.date):
            value = value.isoformat()
        elif isinstance(value, tuple):
            value = list(value)
        out[key] = value
    return out


def _tiebreak(seed: int, sid: str) -> str:
    return hashlib.sha256(f"{seed}:{sid}".encode()).hexdigest()


def session_summaries(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per session: rating rows, kinds, fails, latest ``created_at``, users."""
    by_sid: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("app_name", APP_NAME) == APP_NAME and row.get("session_id"):
            by_sid[str(row["session_id"])].append(row)
    out = {}
    for sid, rs in by_sid.items():
        users = Counter(str(r.get("user_id") or "") for r in rs)
        out[sid] = {
            "rows": list(rs),
            "kinds": {r.get("kind") for r in rs},
            "fails": sum(1 for r in rs if r.get("verdict") == "fail"),
            "latest": max(str(r.get("created_at") or "") for r in rs),
            # The session's owner rated it; the most frequent id if several.
            "user_id": users.most_common(1)[0][0],
        }
    return out


def choose_sessions(
    rows: Iterable[Mapping[str, Any]],
    *,
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    min_fails: int = MIN_FAILS,
    exclude: Iterable[str] = (),
) -> dict[str, Any]:
    """Deterministic, stratified choice of ``n`` sessions (pure).

    Eligible = both kinds rated, not excluded. Ranked most recent first (ties by
    a seed-keyed hash). Stage 1 takes fail-bearing sessions in rank order until
    the set holds ``min_fails`` human fails; stage 2 fills the remaining slots
    in rank order. Returns ``{session_ids, sessions, ratings, total_fails,
    min_fails_met, ...}`` with the sessions in rank order.
    """
    excluded = set(exclude)
    summaries = {
        sid: s
        for sid, s in session_summaries(rows).items()
        if {"ad_copy", "visual"} <= s["kinds"] and sid not in excluded
    }
    ranked = sorted(
        summaries,
        key=lambda sid: (summaries[sid]["latest"], _tiebreak(seed, sid)),
        reverse=True,
    )
    chosen: list[str] = []
    fails = 0
    for sid in ranked:
        if fails >= min_fails or len(chosen) >= n:
            break
        if summaries[sid]["fails"]:
            chosen.append(sid)
            fails += summaries[sid]["fails"]
    for sid in ranked:
        if len(chosen) >= n:
            break
        if sid not in chosen:
            chosen.append(sid)
            fails += summaries[sid]["fails"]
    chosen.sort(key=ranked.index)
    return {
        "app_name": APP_NAME,
        "seed": seed,
        "n": n,
        "min_fails": min_fails,
        "excluded": sorted(excluded),
        "eligible_sessions": len(ranked),
        "session_ids": chosen,
        "sessions": [
            {
                "session_id": sid,
                "user_id": summaries[sid]["user_id"],
                "n_ratings": len(summaries[sid]["rows"]),
                "fails": summaries[sid]["fails"],
                "latest": summaries[sid]["latest"],
            }
            for sid in chosen
        ],
        "ratings": [r for sid in chosen for r in summaries[sid]["rows"]],
        "total_fails": fails,
        "min_fails_met": fails >= min_fails,
    }


def describe(selection: Mapping[str, Any]) -> str:
    lines = [
        f"Selected {len(selection['session_ids'])} of {selection['eligible_sessions']} "
        f"eligible sessions (seed {selection['seed']}); human fails "
        f"{selection['total_fails']} (need >= {selection['min_fails']}: "
        f"{'met' if selection['min_fails_met'] else 'NOT MET'})"
    ]
    for s in selection["sessions"]:
        lines.append(
            f"  {s['session_id']}  user={s['user_id']}  ratings={s['n_ratings']}  "
            f"fails={s['fails']}  latest={s['latest']}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, client: Any = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--min-fails", type=int, default=MIN_FAILS)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--exclude", default="", help="comma-separated session ids")
    parser.add_argument("--out", type=Path, default=SELECTION_PATH)
    args = parser.parse_args(argv)

    if client is None:
        from google.cloud import bigquery

        client = bigquery.Client(project=args.table.split(".")[0])
    selection = choose_sessions(
        fetch_rows(client, args.table),
        n=args.n,
        seed=args.seed,
        min_fails=args.min_fails,
        exclude=[s.strip() for s in args.exclude.split(",") if s.strip()],
    )
    write_json(args.out, selection)
    print(describe(selection))
    print(f"Wrote {args.out}")
    return selection


if __name__ == "__main__":
    main()
