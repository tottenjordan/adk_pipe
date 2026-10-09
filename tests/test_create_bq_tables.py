"""deployment/create_bq_tables.sh with a stub ``bq`` on PATH (no GCP)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deployment" / "create_bq_tables.sh"

# contracts.md §3 column lists, in order.
BANDIT_EXPERIMENTS = (
    "experiment_id:STRING,user_id:STRING,session_id:STRING,app_name:STRING,"
    "created_at:TIMESTAMP,updated_at:TIMESTAMP,status:STRING,scenario:STRING,"
    "ctr_mode:STRING,reward_mode:STRING,arms:STRING,config_uri:STRING,"
    "model_resource:STRING,endpoint_id:STRING,deployed_model_id:STRING,"
    "ttl_expires_at:TIMESTAMP,stopped_at:TIMESTAMP,traffic_execution:STRING,"
    "progress:STRING,error:STRING,scenario_overrides:STRING,"
    "deploy_lease_until:TIMESTAMP,deploy_lease_owner:STRING,policy_discount:FLOAT,traffic_runs:STRING"
)
BANDIT_EVENTS = (
    "experiment_id:STRING,episode:INTEGER,round:INTEGER,batch:INTEGER,"
    "request_id:STRING,ts:TIMESTAMP,policy:STRING,segment:STRING,context:STRING,"
    "arm:STRING,propensity:FLOAT,reward:FLOAT,clicked:INTEGER,dwell_s:FLOAT,"
    "p_chosen:FLOAT,p_optimal:FLOAT,optimal_arm:STRING,regret:FLOAT,"
    "model_version:STRING,latency_ms:FLOAT,traffic_run:INTEGER"
)
BANDIT_METRICS = (
    "experiment_id:STRING,episode:INTEGER,policy:STRING,horizon:INTEGER,"
    "total_reward:FLOAT,total_clicks:INTEGER,cumulative_regret:FLOAT,"
    "pct_optimal:FLOAT,steps_to_converge:INTEGER,curve:STRING,arm_share:STRING,"
    "per_segment:STRING,arm_stats:STRING,created_at:TIMESTAMP,traffic_run:INTEGER,"
    "shift_response:STRING,regimes:STRING"
)

# creative_ratings needs a REPEATED column (fail_reasons), which the inline
# `bq mk` schema can't express, so it is created from a JSON schema file.
RATINGS_SCHEMA = SCRIPT.parent / "bq_schemas" / "creative_ratings.json"
CREATIVE_RATINGS = (
    "rating_id:STRING,session_id:STRING,app_name:STRING,creative_key:STRING,"
    "kind:STRING,user_id:STRING,verdict:STRING,score:INTEGER,note:STRING,"
    "judge_overall:FLOAT,judge_passed:BOOLEAN,judge_gates_passed:BOOLEAN,"
    "judge_model:STRING,judge_source:STRING,judge_version:STRING,"
    "learning_used:BOOLEAN,brand:STRING,visual_style:STRING,"
    "tone_style:STRING,angle_id:STRING,fail_reasons:STRING:REPEATED,"
    "created_at:TIMESTAMP,updated_at:TIMESTAMP"
)


def _ratings_schema() -> list[dict]:
    return json.loads(RATINGS_SCHEMA.read_text())


def _run(tmp_path: Path, exists: bool) -> list[str]:
    stub = tmp_path / "bq"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{tmp_path}/calls"\n'
        'case "$1 $2" in show*|*\\ show) exit ' + ("0" if exists else "1") + ";; esac\n"
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "BQ_PROJECT_ID": "p",
        "BQ_DATASET_ID": "d",
        "BQ_TABLE_TARGETS": "t",
        "BQ_TABLE_CREATIVES": "c",
        "BQ_TABLE_EVALS": "e",
    }
    for k in (
        "BQ_TABLE_BANDIT_EXPERIMENTS",
        "BQ_TABLE_BANDIT_EVENTS",
        "BQ_TABLE_RATINGS",
        "BQ_TABLE_SHARES",
        "BQ_TABLE_PERSON_REFS",
    ):
        env.pop(k, None)
    env["BQ_TABLE_BANDIT_METRICS"] = "my_metrics"
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True)
    return (tmp_path / "calls").read_text().splitlines()


@pytest.mark.subprocess
def test_creates_bandit_tables_per_contract(tmp_path):
    calls = [c for c in _run(tmp_path, exists=False) if c.startswith("mk -t")]
    by_table = {c.split()[-2]: c for c in calls}
    assert by_table["p:d.bandit_experiments"].endswith(BANDIT_EXPERIMENTS)
    events = by_table["p:d.bandit_events"]
    assert events.endswith(BANDIT_EVENTS)
    assert (
        "--time_partitioning_field ts --time_partitioning_type DAY "
        "--clustering_fields experiment_id" in events
    )
    assert by_table["p:d.my_metrics"].endswith(BANDIT_METRICS)
    assert len(calls) == 9


@pytest.mark.subprocess
def test_creates_creative_ratings_table(tmp_path):
    calls = [c for c in _run(tmp_path, exists=False) if c.startswith("mk -t")]
    by_table = {c.split()[-2]: c for c in calls}
    ratings = by_table["p:d.creative_ratings"]
    assert ratings.split()[-1] == str(RATINGS_SCHEMA)
    assert "--clustering_fields user_id,session_id" in ratings


@pytest.mark.subprocess
def test_creates_creative_shares_table(tmp_path):
    calls = [c for c in _run(tmp_path, exists=False) if c.startswith("mk -t")]
    by_table = {c.split()[-2]: c for c in calls}
    shares = by_table["p:d.creative_shares"]
    assert shares.split()[-1] == str(
        SCRIPT.parent / "bq_schemas" / "creative_shares.json"
    )
    assert "--clustering_fields owner_user" in shares


@pytest.mark.subprocess
def test_creates_person_references_table(tmp_path):
    calls = [c for c in _run(tmp_path, exists=False) if c.startswith("mk -t")]
    by_table = {c.split()[-2]: c for c in calls}
    refs = by_table["p:d.person_references"]
    assert refs.split()[-1] == str(
        SCRIPT.parent / "bq_schemas" / "person_references.json"
    )
    assert "--clustering_fields owner_user" in refs


def test_ratings_schema_file_lists_the_columns_in_order():
    got = [
        ":".join(
            [f["name"], f["type"]]
            + ([f["mode"]] if f.get("mode", "NULLABLE") != "NULLABLE" else [])
        )
        for f in _ratings_schema()
    ]
    assert got == CREATIVE_RATINGS.split(",")


def test_ratings_schema_matches_store_columns():
    from runserver.ratings_store import RATING_COLUMN_TYPES

    bq = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
    got = {}
    for f in _ratings_schema():
        typ = bq.get(f["type"], f["type"])
        got[f["name"]] = f"ARRAY<{typ}>" if f.get("mode") == "REPEATED" else typ
    assert got == RATING_COLUMN_TYPES


@pytest.mark.subprocess
def test_idempotent_when_tables_exist(tmp_path):
    calls = _run(tmp_path, exists=True)
    assert not [c for c in calls if " mk " in f" {c} "]


@pytest.mark.subprocess
def test_creative_evals_schema_matches_eval_row_columns(tmp_path):
    """The `bq mk` creative_evals schema names exactly the MERGE's columns."""
    from creative_agent.bq_tools import EVAL_COLUMN_TYPES

    calls = [c for c in _run(tmp_path, exists=False) if c.startswith("mk -t")]
    (evals,) = [c for c in calls if c.split()[-2] == "p:d.e"]
    bq_mk_types = {"FLOAT64": "FLOAT", "INT64": "INTEGER"}
    expected = {f"{c}:{bq_mk_types.get(t, t)}" for c, t in EVAL_COLUMN_TYPES.items()}
    assert set(evals.split()[-1].split(",")) == expected
