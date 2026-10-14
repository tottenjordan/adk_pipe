"""Hand off the Agent Engine IDs minted by a Cloud Build deploy.

Each parallel deploy step of deployment/cloudbuild.agents.yaml writes one
``deploy_agent.py --output_json`` record into a directory. This script:

  collect  merge them into ``engine_ids.json`` (+ optional GCS upload), print
           ``agent: old -> new`` (old = the newest other engine with the same
           display-name prefix) and the ``.env`` lines; exits 1 if an expected
           agent has no record (its deploy step failed)
  get      print one agent's new resource name (used by the smoke step)
  show     print ``agent: old -> new`` from an ``engine_ids.json``
  apply    operator side: write the IDs into a local ``.env`` (old -> new shown);
           ``--update-worker`` also sets the batch worker's
           ``CREATIVE_AGENT_ENGINE_ID`` env var (``gcloud run services update``),
           refusing while a trends row is PROCESSING unless ``--force``

    uv run python deployment/engine_handoff.py apply --ids engine_ids.json --env-file .env
    uv run python deployment/engine_handoff.py apply --ids engine_ids.json --update-worker
"""

import argparse
import json
import os
import re
import subprocess
import sys

import dotenv

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from deployment.deploy_agent import (  # noqa: E402
    AGENT_DEPLOY_SPECS,
    engine_env_key,
    update_env_file,
)


def load_records(records_dir: str) -> dict[str, dict]:
    """Read every ``<agent>.json`` deploy record in ``records_dir``."""
    records = {}
    for agent in AGENT_DEPLOY_SPECS:
        path = os.path.join(records_dir, f"{agent}.json")
        if os.path.exists(path):
            with open(path) as f:
                records[agent] = json.load(f)
    return records


def previous_engine(runtimes, agent: str, exclude: str) -> str | None:
    """The newest engine (other than ``exclude``) with ``agent``'s display-name prefix.

    Best effort, for the build log's old -> new line only: the operator's .env
    (shown by ``apply``) is the authoritative "old" ID.
    """
    prefix = f"{AGENT_DEPLOY_SPECS[agent]['display_name']}-"
    candidates = [
        r.api_resource
        for r in runtimes
        if (r.api_resource.display_name or "").startswith(prefix)
        and r.api_resource.name != exclude
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda a: str(a.create_time)).name


def old_to_new_lines(engines: dict[str, dict]) -> list[str]:
    return [
        f"{agent}: {rec.get('previous') or '?'} -> {rec['resource_name']}"
        for agent, rec in sorted(engines.items())
    ]


def env_lines(engines: dict[str, dict]) -> list[str]:
    return [
        f"{engine_env_key(agent)}='{rec['resource_name']}'"
        for agent, rec in sorted(engines.items())
    ]


def collect(
    records_dir: str,
    out_path: str,
    expected: list[str],
    commit: str = "",
    runtimes=None,
) -> tuple[dict, list[str]]:
    """Merge the deploy records into ``out_path``; return (document, missing agents)."""
    engines = load_records(records_dir)
    if runtimes is not None:
        for agent, rec in engines.items():
            rec["previous"] = previous_engine(runtimes, agent, rec["resource_name"])
    doc = {"commit": commit, "engines": engines}
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    missing = [a for a in expected if a not in engines]

    print(f"New Agent Engines (commit {commit or 'unknown'}):")
    for line in old_to_new_lines(engines):
        print(f"  {line}")
    print("\n.env lines (or run `engine_handoff.py apply`):")
    for line in env_lines(engines):
        print(f"  {line}")
    if missing:
        print(f"\nMISSING (deploy step failed): {', '.join(missing)}")
    return doc, missing


def _engines(ids_path: str) -> dict[str, dict]:
    with open(ids_path) as f:
        return json.load(f)["engines"]


def get(ids_path: str, agent: str) -> str:
    return _engines(ids_path)[agent]["resource_name"]


def show(ids_path: str) -> None:
    for line in old_to_new_lines(_engines(ids_path)):
        print(line)


def apply(ids_path: str, env_file: str) -> None:
    """Write every engine ID from ``ids_path`` into ``env_file`` (old -> new)."""
    engines = _engines(ids_path)
    current = dotenv.dotenv_values(env_file) if os.path.exists(env_file) else {}
    for agent, rec in sorted(engines.items()):
        key = engine_env_key(agent)
        print(f"{key}: {current.get(key) or '<unset>'} -> {rec['resource_name']}")
        update_env_file(agent, rec["resource_name"], env_file)


# The batch worker (cloud_functions/creative_fanout) runs the creative engine
# named in its CREATIVE_AGENT_ENGINE_ID env var.
WORKER_SERVICE = "creative-worker-crf"
WORKER_AGENT = "creative_agent"
WORKER_ENV_KEY = "CREATIVE_AGENT_ENGINE_ID"
# Project / dataset / table ids spliced into the busy check's SQL.
_BQ_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,1024}")


class WorkerUpdateError(RuntimeError):
    """The worker update was refused or failed; nothing was changed."""


def _bigquery_client(project: str):
    from google.cloud import bigquery

    return bigquery.Client(project=project)


def _bq_name(key: str, value: str | None) -> str:
    """A set, well-formed BigQuery name for the busy check's SQL."""
    if not value:
        raise WorkerUpdateError(
            f"can't check for PROCESSING rows: {key} is unset (set it in .env, "
            "or pass --force to skip the check)"
        )
    if not _BQ_ID_RE.fullmatch(value):
        raise WorkerUpdateError(f"can't check for PROCESSING rows: bad {key}={value!r}")
    return value


def processing_rows() -> int:
    """How many rows of the trends table are PROCESSING (a worker run in flight).

    The table is ``BQ_PROJECT_ID`` (else ``GOOGLE_CLOUD_PROJECT``) .
    ``BQ_DATASET_ID`` . ``BQ_TABLE_TARGETS``, the env deploy_agent.py loads
    from the repo ``.env``. Raises WorkerUpdateError when it can't be named.
    """
    project, dataset, table = (
        _bq_name(
            "BQ_PROJECT_ID",
            os.getenv("BQ_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"),
        ),
        _bq_name("BQ_DATASET_ID", os.getenv("BQ_DATASET_ID")),
        _bq_name("BQ_TABLE_TARGETS", os.getenv("BQ_TABLE_TARGETS")),
    )
    sql = (
        f"SELECT COUNT(*) AS n FROM `{project}.{dataset}.{table}` "
        "WHERE processed_status = 'PROCESSING'"
    )
    rows = _bigquery_client(project).query_and_wait(sql, wait_timeout=60)
    return int(next(iter(rows))[0])


def check_worker_idle() -> None:
    """Raise WorkerUpdateError while a trends row is PROCESSING: updating the
    worker rolls out a new revision, which kills the run in flight."""
    busy = processing_rows()
    if busy:
        raise WorkerUpdateError(
            f"worker busy: {busy} row(s) PROCESSING; re-run once they finish "
            "(or pass --force, which kills the run in flight)"
        )


def update_worker(resource_name: str, service: str = WORKER_SERVICE) -> None:
    """Set ``service``'s ``CREATIVE_AGENT_ENGINE_ID`` to ``resource_name``
    (shell-free gcloud; a failure raises CalledProcessError). The worker
    accepts the full resource name or a bare id."""
    region = os.getenv("GCP_REGION", "us-central1")
    print(f"{service}: {WORKER_ENV_KEY} -> {resource_name}")
    subprocess.run(
        [
            "gcloud",
            "run",
            "services",
            "update",
            service,
            "--region",
            region,
            "--update-env-vars",
            f"{WORKER_ENV_KEY}={resource_name}",
            "--quiet",
        ],
        check=True,
    )


def _storage_client():
    from google.cloud import storage

    return storage.Client()


def upload(path: str, gcs_uri: str) -> None:
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {gcs_uri}")
    bucket, _, blob = gcs_uri[len("gs://") :].partition("/")
    _storage_client().bucket(bucket).blob(blob).upload_from_filename(
        path, content_type="application/json"
    )
    print(f"Uploaded {path} to {gcs_uri}")


def _list_runtimes():
    from deployment.deploy_agent import _get_client

    return list(_get_client().runtimes.list())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--dir", required=True, help="directory of <agent>.json records")
    c.add_argument("--out", required=True, help="merged engine_ids.json path")
    c.add_argument("--expect", default="", help="comma-separated selected agents")
    c.add_argument("--commit", default="")
    c.add_argument("--upload", default="", help="gs:// URI to copy engine_ids.json to")
    c.add_argument(
        "--previous",
        action="store_true",
        help="look up each agent's previous engine (Agent Runtime list)",
    )

    g = sub.add_parser("get")
    g.add_argument("--ids", required=True)
    g.add_argument("--agent", required=True, choices=list(AGENT_DEPLOY_SPECS))

    s = sub.add_parser("show")
    s.add_argument("--ids", required=True)

    a = sub.add_parser("apply")
    a.add_argument("--ids", required=True)
    a.add_argument("--env-file", default=os.path.join(project_root, ".env"))
    a.add_argument(
        "--update-worker",
        action="store_true",
        help=f"also set the batch worker's {WORKER_ENV_KEY} env var (gcloud)",
    )
    a.add_argument(
        "--force",
        action="store_true",
        help="with --update-worker: skip the PROCESSING-rows check",
    )
    a.add_argument("--worker-service", default=WORKER_SERVICE)

    args = parser.parse_args(argv)
    if args.cmd == "collect":
        expected = [x for x in args.expect.split(",") if x]
        runtimes = None
        if args.previous:
            try:
                runtimes = _list_runtimes()
            except Exception as e:  # informational only; never fail the hand-off
                print(f"(previous-engine lookup failed: {e})")
        _, missing = collect(args.dir, args.out, expected, args.commit, runtimes)
        if args.upload:
            upload(args.out, args.upload)
        return 1 if missing else 0
    if args.cmd == "get":
        print(get(args.ids, args.agent))
        return 0
    if args.cmd == "show":
        show(args.ids)
        return 0
    if not args.update_worker:
        apply(args.ids, args.env_file)
        return 0
    # The busy check runs before anything is written, so a refusal changes
    # nothing (neither .env nor the worker): re-run once the worker is idle.
    record = _engines(args.ids).get(WORKER_AGENT)
    try:
        if record is not None and not args.force:
            check_worker_idle()
        apply(args.ids, args.env_file)
        if record is None:
            print(
                f"{args.worker_service}: no {WORKER_AGENT} record in {args.ids}; "
                "worker not updated."
            )
            return 0
        update_worker(record["resource_name"], args.worker_service)
    except WorkerUpdateError as e:
        print(f"Refusing to update {args.worker_service}: {e}")
        return 1
    except subprocess.CalledProcessError as e:
        print(f"Updating {args.worker_service} failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
