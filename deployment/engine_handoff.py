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
           then routes 100% of its (pinned) traffic to the new revision and
           verifies revision, env and traffic; refuses while a trends row is PROCESSING unless ``--force``.
           The worker's Cloud Tasks queue (``CRF_TASKS_QUEUE``, default
           ``creative-worker-queue``, in ``CRF_TASKS_LOCATION`` else ``GCP_REGION``)
           is paused before the busy check and resumed afterwards, even on a
           refusal or an error, so no task starts between the check and the
           rollout; the check also refuses while a queued task's last attempt
           is in flight (``--force`` skips the checks, not the pause)

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
    """Raise WorkerUpdateError while a trends row is PROCESSING, or while the
    (paused) queue has a task in flight that may not have locked its row yet:
    updating the worker rolls out a new revision, which kills the run in flight."""
    busy = processing_rows()
    if busy:
        raise WorkerUpdateError(
            f"worker busy: {busy} row(s) PROCESSING; re-run once they finish "
            "(or pass --force, which kills the run in flight)"
        )
    dispatched = in_flight_tasks()
    if dispatched:
        raise WorkerUpdateError(
            f"worker busy: {dispatched} task(s) in flight on the queue; re-run "
            "once they finish (or pass --force, which kills the run in flight)"
        )


# The worker's Cloud Tasks queue; same env names and defaults as the CRF config
# (cloud_functions/creative_fanout/config.py).
WORKER_QUEUE = "creative-worker-queue"


def _worker_queue() -> tuple[str, str]:
    """``(queue, location)`` of the worker's Cloud Tasks queue."""
    return (
        os.getenv("CRF_TASKS_QUEUE") or WORKER_QUEUE,
        os.getenv("CRF_TASKS_LOCATION") or os.getenv("GCP_REGION") or "us-central1",
    )


def _queue_cmd(verb: str) -> list[str]:
    queue, location = _worker_queue()
    return ["gcloud", "tasks", "queues", verb, queue, "--location", location, "--quiet"]


def pause_worker_queue() -> None:
    """Pause the worker's queue so no task is dispatched (tasks can still be
    enqueued) between the busy check and the revision rollout: a task started
    in that window would be killed by the new revision. Raises
    WorkerUpdateError when gcloud fails (the queue is then not paused).

    https://cloud.google.com/sdk/gcloud/reference/tasks/queues/pause
    """
    cmd = _queue_cmd("pause")
    print(f"pausing queue {cmd[4]} ({cmd[6]})")
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        raise WorkerUpdateError(
            f"can't pause queue {cmd[4]} ({cmd[6]}): {e}; nothing was changed"
        ) from e


def in_flight_tasks() -> int:
    """How many of the queue's tasks have their latest attempt in flight
    (``lastAttempt.dispatchTime`` set, no ``lastAttempt.responseTime``). Pausing
    stops new dispatches only, and a task dispatched just before the pause may
    not have marked its row PROCESSING yet. Raises WorkerUpdateError when the
    tasks can't be listed.

    https://cloud.google.com/sdk/gcloud/reference/tasks/list
    https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks#Attempt
    """
    queue, location = _worker_queue()
    cmd = ["gcloud", "tasks", "list", "--queue", queue, "--location", location]
    try:
        out = subprocess.run(
            [*cmd, "--format=json"], check=True, capture_output=True, text=True
        )
        tasks = json.loads(out.stdout or "[]")
    except (subprocess.CalledProcessError, OSError, ValueError) as e:
        raise WorkerUpdateError(
            f"can't list the tasks of queue {queue} ({location}): {e} "
            "(or pass --force to skip the check)"
        ) from e
    return sum(
        1
        for t in tasks
        if (t.get("lastAttempt") or {}).get("dispatchTime")
        and not (t.get("lastAttempt") or {}).get("responseTime")
    )


def resume_worker_queue() -> bool:
    """Resume the worker's queue; False (after a loud message naming the manual
    command) when that fails. Never raises, so it can't mask the error that
    ended the update.

    https://cloud.google.com/sdk/gcloud/reference/tasks/queues/resume
    """
    cmd = _queue_cmd("resume")
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        print(
            f"!!! queue {cmd[4]} ({cmd[6]}) is STILL PAUSED: resume failed ({e}). "
            "No batch row runs until it is resumed; run:\n"
            f"  {' '.join(cmd[:-1])}"
        )
        return False
    print(f"resumed queue {cmd[4]} ({cmd[6]})")
    return True


class WorkerTrafficError(RuntimeError):
    """The worker's new revision was created but is not verifiably serving."""


def _gcloud_json(args: list[str]) -> dict:
    """Run a read-only ``gcloud ... --format=json`` command and parse it."""
    out = subprocess.run(
        ["gcloud", *args, "--format=json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout or "{}")


def _describe_service(service: str, region: str) -> dict:
    return _gcloud_json(["run", "services", "describe", service, "--region", region])


def _serving(svc: dict) -> dict[str, int]:
    """``{revision: percent}`` of the service's routed traffic (percent > 0)."""
    serving: dict[str, int] = {}
    for t in (svc.get("status") or {}).get("traffic") or []:
        rev, pct = t.get("revisionName"), int(t.get("percent") or 0)
        if rev and pct:
            serving[rev] = serving.get(rev, 0) + pct
    return serving


def _to_revisions(serving: dict[str, int]) -> str:
    return ",".join(f"{rev}={pct}" for rev, pct in sorted(serving.items()))


def _traffic_cmd(service: str, region: str, to_revisions: str) -> str:
    return (
        f"gcloud run services update-traffic {service} --region {region} "
        f"--to-revisions {to_revisions}"
    )


def update_worker(resource_name: str, service: str = WORKER_SERVICE) -> None:
    """Set ``service``'s ``CREATIVE_AGENT_ENGINE_ID`` to ``resource_name`` and
    make the new revision serve 100% of the traffic (shell-free gcloud; a
    failing gcloud call raises CalledProcessError). The worker accepts the full
    resource name or a bare id.

    The worker service is traffic-pinned (traffic assigned to a named revision,
    not "latest"), so ``services update`` alone creates the new revision at 0%
    and the old engine keeps serving (seen 2026-10-15). After the update:

    1. ``services describe`` -> ``status.latestCreatedRevisionName`` = the
       revision the update created;
    2. ``revisions describe`` it: its ``Ready`` condition must be ``True`` and
       its container env must carry the new ``CREATIVE_AGENT_ENGINE_ID``;
    3. unless it already serves 100% (a service that follows the latest
       revision), ``services update-traffic --to-revisions <new>=100``. The
       explicit revision rather than ``--to-latest``: it keeps the pin model
       the service already uses (rollback = pin the previous revision again)
       and routes exactly the revision verified in step 2, never a newer one a
       concurrent deploy created in between;
    4. ``services describe`` again: ``status.traffic`` must give the new
       revision 100%.

    A failed check raises WorkerTrafficError naming the manual fix and the
    rollback command (the revisions that served before the update).

    gcloud references:
    https://cloud.google.com/sdk/gcloud/reference/run/services/update-traffic
    https://cloud.google.com/sdk/gcloud/reference/run/services/describe
    https://cloud.google.com/sdk/gcloud/reference/run/revisions/describe
    (JSON fields ``status.latestCreatedRevisionName`` / ``status.traffic``:
    https://cloud.google.com/run/docs/reference/rest/v1/namespaces.services)
    """
    region = os.getenv("GCP_REGION", "us-central1")
    before = _serving(_describe_service(service, region))
    rollback = (
        _traffic_cmd(service, region, _to_revisions(before)) if before else "(none)"
    )

    def fail(problem: str, new: str | None) -> WorkerTrafficError:
        fix = (
            f"\n  fix (once {new} is Ready with the new env): "
            f"{_traffic_cmd(service, region, f'{new}=100')}"
            if new
            else ""
        )
        return WorkerTrafficError(
            f"{problem}\n  traffic before the update: "
            f"{_to_revisions(before) or 'unknown'}{fix}\n  rollback: {rollback}"
        )

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

    svc = _describe_service(service, region)
    new = (svc.get("status") or {}).get("latestCreatedRevisionName")
    if not new:
        raise fail("can't tell which revision the update created", None)
    rev = _gcloud_json(["run", "revisions", "describe", new, "--region", region])
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in (rev.get("status") or {}).get("conditions") or []
    )
    if not ready:
        raise fail(
            f"new revision {new} is not Ready; traffic left on "
            f"{_to_revisions(_serving(svc)) or 'unknown'}",
            new,
        )
    env_values = [
        e.get("value")
        for c in (rev.get("spec") or {}).get("containers") or []
        for e in c.get("env") or []
        if e.get("name") == WORKER_ENV_KEY
    ]
    if env_values != [resource_name]:
        raise fail(
            f"new revision {new} has {WORKER_ENV_KEY}={env_values!r}, "
            f"expected {resource_name!r}",
            None,
        )
    if _serving(svc).get(new) != 100:
        print(f"{service}: routing 100% to {new} (was {_to_revisions(_serving(svc))})")
        subprocess.run(
            [
                "gcloud",
                "run",
                "services",
                "update-traffic",
                service,
                "--region",
                region,
                "--to-revisions",
                f"{new}=100",
                "--quiet",
            ],
            check=True,
        )
        svc = _describe_service(service, region)
    serving = _serving(svc)
    if serving.get(new) != 100:
        raise fail(
            f"{new} does not serve 100% after update-traffic "
            f"(traffic: {_to_revisions(serving) or 'none'})",
            new,
        )
    print(f"{service}: {new} serves 100% with {WORKER_ENV_KEY}={resource_name}")


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
        help="with --update-worker: skip the PROCESSING-rows check (the queue "
        "is still paused and resumed)",
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
    if record is None:
        apply(args.ids, args.env_file)
        print(
            f"{args.worker_service}: no {WORKER_AGENT} record in {args.ids}; "
            "worker not updated."
        )
        return 0
    # Pause the queue first so no task starts between the check and the
    # rollout. If the pause fails the queue isn't paused: nothing to resume.
    try:
        pause_worker_queue()
    except WorkerUpdateError as e:
        print(f"Refusing to update {args.worker_service}: {e}")
        return 1
    rc = 1
    try:
        if not args.force:
            check_worker_idle()
        apply(args.ids, args.env_file)
        update_worker(record["resource_name"], args.worker_service)
        rc = 0
    except WorkerUpdateError as e:
        print(f"Refusing to update {args.worker_service}: {e}")
    except (WorkerTrafficError, subprocess.CalledProcessError) as e:
        print(f"Updating {args.worker_service} failed: {e}")
    finally:
        # Always resume: on success, a refusal, or any error (which propagates).
        if not resume_worker_queue():
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
