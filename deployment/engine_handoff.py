"""Hand off the Agent Engine IDs minted by a Cloud Build deploy.

Each parallel deploy step of deployment/cloudbuild.agents.yaml writes one
``deploy_agent.py --output_json`` record into a directory. This script:

  collect  merge them into ``engine_ids.json`` (+ optional GCS upload), print
           ``agent: old -> new`` (old = the newest other engine with the same
           display-name prefix) and the ``.env`` lines; exits 1 if an expected
           agent has no record (its deploy step failed)
  get      print one agent's new resource name (used by the smoke step)
  show     print ``agent: old -> new`` from an ``engine_ids.json``
  apply    operator side: write the IDs into a local ``.env`` (old -> new shown)

    uv run python deployment/engine_handoff.py apply --ids engine_ids.json --env-file .env
"""

import argparse
import json
import os
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
    apply(args.ids, args.env_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
