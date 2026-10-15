"""``engine_handoff.py apply --update-worker``: push the new creative engine to
the batch worker's ``CREATIVE_AGENT_ENGINE_ID`` env var, refusing while a row
is PROCESSING (a revision change would kill the run in flight). gcloud and
BigQuery are mocked.
"""

import json
import subprocess
from unittest.mock import MagicMock

import pytest

RES = "projects/1/locations/us-central1/reasoningEngines/{}"


@pytest.fixture
def eh(monkeypatch):
    try:
        import deployment.engine_handoff as engine_handoff
    except ImportError as e:  # agentplatform/absl missing in a bare env
        pytest.skip(f"engine_handoff import unavailable: {e}")
    monkeypatch.setenv("BQ_PROJECT_ID", "bq-proj")
    monkeypatch.setenv("BQ_DATASET_ID", "trend_trawler")
    monkeypatch.setenv("BQ_TABLE_TARGETS", "target_trends_crf")
    monkeypatch.setenv("GCP_REGION", "us-central1")
    return engine_handoff


@pytest.fixture
def ids(tmp_path):
    def _write(engines):
        path = tmp_path / "engine_ids.json"
        doc = {
            "commit": "abc",
            "engines": {
                a: {"resource_name": RES.format(i)} for a, i in engines.items()
            },
        }
        path.write_text(json.dumps(doc))
        return str(path)

    return _write


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("")
    return str(path)


@pytest.fixture
def bq(eh, monkeypatch):
    """A BigQuery client double whose COUNT(*) answers ``bq.processing``."""
    client = MagicMock()
    client.processing = 0
    client.query_and_wait.side_effect = lambda sql, **kw: [(client.processing,)]
    projects = []

    def _client(project):
        projects.append(project)
        return client

    monkeypatch.setattr(eh, "_bigquery_client", _client)
    client.projects = projects
    return client


class FakeGcloud:
    """A stateful stand-in for the gcloud CLI (``subprocess.run``).

    ``services update`` mints the next revision (Ready unless ``ready`` is
    False) carrying the new env; traffic follows it only when the service
    tracks the latest revision (``pinned`` False), like Cloud Run.
    ``services update-traffic --to-revisions R=100`` moves the traffic.
    ``services describe`` / ``revisions describe`` answer JSON.
    """

    def __init__(self):
        self.calls = []
        self.pinned = True
        self.ready = True
        self.env_override = None  # env value the new revision really carries
        self.ignore_update_traffic = False
        self.revisions = {"creative-worker-crf-00022-old": "old-engine"}
        self.ready_by_rev = {"creative-worker-crf-00022-old": True}
        self.latest = "creative-worker-crf-00022-old"
        self.traffic = [{"revisionName": self.latest, "percent": 100}]

    def _traffic(self):
        if not self.pinned:
            return [
                {"revisionName": self.latest, "percent": 100, "latestRevision": True}
            ]
        return self.traffic

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        verb = cmd[1:4]
        out = ""
        if verb == ["run", "services", "update"]:
            env = cmd[cmd.index("--update-env-vars") + 1].split("=", 1)[1]
            n = len(self.revisions) + 22
            name = f"{cmd[4]}-000{n}-new"
            self.revisions[name] = self.env_override or env
            self.ready_by_rev[name] = self.ready
            self.latest = name
        elif verb == ["run", "services", "update-traffic"]:
            rev, pct = cmd[cmd.index("--to-revisions") + 1].split("=")
            if not self.ignore_update_traffic:
                self.traffic = [{"revisionName": rev, "percent": int(pct)}]
        elif verb == ["run", "services", "describe"]:
            ready = self.latest if self.ready_by_rev[self.latest] else None
            out = json.dumps(
                {
                    "status": {
                        "latestCreatedRevisionName": self.latest,
                        "latestReadyRevisionName": ready,
                        "traffic": self._traffic(),
                    }
                }
            )
        elif verb == ["run", "revisions", "describe"]:
            rev = cmd[4]
            out = json.dumps(
                {
                    "spec": {
                        "containers": [
                            {
                                "env": [
                                    {"name": "OTHER", "value": "x"},
                                    {
                                        "name": "CREATIVE_AGENT_ENGINE_ID",
                                        "value": self.revisions[rev],
                                    },
                                ]
                            }
                        ]
                    },
                    "status": {
                        "conditions": [
                            {
                                "type": "Ready",
                                "status": "True" if self.ready_by_rev[rev] else "False",
                            }
                        ]
                    },
                }
            )
        else:  # pragma: no cover - an unexpected gcloud call fails the test
            raise AssertionError(f"unexpected gcloud call: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    def mutating(self):
        """The calls that change the service (no describes)."""
        return [c for c, _ in self.calls if "describe" not in c]


@pytest.fixture
def gcloud(monkeypatch):
    fake = FakeGcloud()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def run(gcloud):
    """The mutating gcloud commands (update / update-traffic), in order."""
    return gcloud


def _traffic_cmd(rev, service="creative-worker-crf", region="us-central1"):
    return [
        "gcloud",
        "run",
        "services",
        "update-traffic",
        service,
        "--region",
        region,
        "--to-revisions",
        f"{rev}=100",
        "--quiet",
    ]


def _expected_cmd(service="creative-worker-crf", rid=11):
    return [
        "gcloud",
        "run",
        "services",
        "update",
        service,
        "--region",
        "us-central1",
        "--update-env-vars",
        f"CREATIVE_AGENT_ENGINE_ID={RES.format(rid)}",
        "--quiet",
    ]


def test_update_worker_checks_processing_rows_then_updates(eh, ids, env_file, bq, run):
    path = ids({"creative_agent": 11, "trend_scout": 33})
    argv = ["apply", "--ids", path, "--env-file", env_file, "--update-worker"]
    assert eh.main(argv) == 0
    (sql,) = [c.args[0] for c in bq.query_and_wait.call_args_list]
    flat = " ".join(sql.split())
    assert "SELECT COUNT(*)" in flat
    assert "FROM `bq-proj.trend_trawler.target_trends_crf`" in flat
    assert "WHERE processed_status = 'PROCESSING'" in flat
    assert bq.projects == ["bq-proj"]
    assert run.mutating()[0] == _expected_cmd()
    for _, kwargs in run.calls:
        assert kwargs.get("check") is True
        assert not kwargs.get("shell")
    # .env is still written as before.
    assert f"CREATIVE_AGENT_ENGINE_ID='{RES.format(11)}'" in open(env_file).read()


def test_update_worker_refuses_while_a_row_is_processing(
    eh, ids, env_file, bq, run, capsys
):
    bq.processing = 2
    path = ids({"creative_agent": 11})
    argv = ["apply", "--ids", path, "--env-file", env_file, "--update-worker"]
    assert eh.main(argv) != 0
    assert run.calls == []
    assert "worker busy" in capsys.readouterr().out
    # Nothing changed: re-run once the worker is idle.
    assert open(env_file).read() == ""


def test_update_worker_force_skips_the_busy_check(eh, ids, env_file, bq, run):
    bq.processing = 2
    path = ids({"creative_agent": 11})
    argv = ["apply", "--ids", path, "--env-file", env_file, "--update-worker"]
    assert eh.main([*argv, "--force"]) == 0
    bq.query_and_wait.assert_not_called()
    assert run.mutating()[0] == _expected_cmd()


def test_update_worker_service_override(eh, ids, env_file, bq, run):
    path = ids({"creative_agent": 12})
    argv = ["apply", "--ids", path, "--env-file", env_file, "--update-worker"]
    assert eh.main([*argv, "--worker-service", "load-test-worker"]) == 0
    assert run.mutating()[0] == _expected_cmd("load-test-worker", 12)
    assert all(
        cmd[4] == "load-test-worker" for cmd, _ in run.calls if cmd[2] == "services"
    )


def test_update_worker_region_from_env(eh, ids, env_file, bq, run, monkeypatch):
    monkeypatch.setenv("GCP_REGION", "europe-west4")
    path = ids({"creative_agent": 11})
    eh.main(["apply", "--ids", path, "--env-file", env_file, "--update-worker"])
    for cmd, _ in run.calls:
        assert cmd[cmd.index("--region") + 1] == "europe-west4"


def test_update_worker_without_a_creative_record_is_a_noop(
    eh, ids, env_file, bq, run, capsys
):
    path = ids({"trend_scout": 33})
    argv = ["apply", "--ids", path, "--env-file", env_file, "--update-worker"]
    assert eh.main(argv) == 0
    assert run.calls == []
    bq.query_and_wait.assert_not_called()
    assert "no creative_agent record" in capsys.readouterr().out
    assert f"SCOUT_AGENT_ENGINE_ID='{RES.format(33)}'" in open(env_file).read()


def test_apply_without_update_worker_never_touches_the_worker(
    eh, ids, env_file, bq, run
):
    path = ids({"creative_agent": 11})
    assert eh.main(["apply", "--ids", path, "--env-file", env_file]) == 0
    assert run.calls == []
    bq.query_and_wait.assert_not_called()


def test_update_worker_gcloud_failure_exits_nonzero(eh, ids, env_file, bq, monkeypatch):
    def _fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", _fail)
    path = ids({"creative_agent": 11})
    argv = ["apply", "--ids", path, "--env-file", env_file, "--update-worker"]
    assert eh.main(argv) != 0


@pytest.mark.parametrize("unset", ["BQ_DATASET_ID", "BQ_TABLE_TARGETS"])
def test_update_worker_without_the_table_env_refuses(
    eh, ids, env_file, bq, run, monkeypatch, capsys, unset
):
    monkeypatch.delenv(unset, raising=False)
    path = ids({"creative_agent": 11})
    argv = ["apply", "--ids", path, "--env-file", env_file, "--update-worker"]
    assert eh.main(argv) != 0
    assert run.calls == []
    assert unset in capsys.readouterr().out


def test_update_worker_rejects_a_malformed_table_name(
    eh, ids, env_file, bq, run, monkeypatch
):
    monkeypatch.setenv("BQ_TABLE_TARGETS", "t` WHERE 1=1 --")
    path = ids({"creative_agent": 11})
    argv = ["apply", "--ids", path, "--env-file", env_file, "--update-worker"]
    assert eh.main(argv) != 0
    bq.query_and_wait.assert_not_called()
    assert run.calls == []


def test_project_falls_back_to_google_cloud_project(
    eh, ids, env_file, bq, run, monkeypatch
):
    monkeypatch.delenv("BQ_PROJECT_ID", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gcp-proj")
    path = ids({"creative_agent": 11})
    eh.main(["apply", "--ids", path, "--env-file", env_file, "--update-worker"])
    (sql,) = [c.args[0] for c in bq.query_and_wait.call_args_list]
    assert "`gcp-proj.trend_trawler.target_trends_crf`" in sql


# --- traffic: the new revision must serve 100% (the worker is traffic-pinned) ---

ARGV_TAIL = ["--update-worker"]


def _apply(eh, path, env_file):
    return eh.main(["apply", "--ids", path, "--env-file", env_file, *ARGV_TAIL])


def test_pinned_worker_traffic_moves_to_the_new_revision(
    eh, ids, env_file, bq, gcloud, capsys
):
    path = ids({"creative_agent": 11})
    assert _apply(eh, path, env_file) == 0
    new = gcloud.latest
    assert new != "creative-worker-crf-00022-old"
    assert gcloud.mutating() == [_expected_cmd(), _traffic_cmd(new)]
    assert gcloud.traffic == [{"revisionName": new, "percent": 100}]
    out = capsys.readouterr().out
    assert f"{new} serves 100%" in out


def test_worker_following_latest_is_verified_without_moving_traffic(
    eh, ids, env_file, bq, gcloud, capsys
):
    gcloud.pinned = False
    path = ids({"creative_agent": 11})
    assert _apply(eh, path, env_file) == 0
    assert gcloud.mutating() == [_expected_cmd()]
    # ...but it was still checked: the service and the revision were described.
    described = [c[1:4] for c, _ in gcloud.calls if "describe" in c]
    assert ["run", "services", "describe"] in described
    assert ["run", "revisions", "describe"] in described
    assert f"{gcloud.latest} serves 100%" in capsys.readouterr().out


def test_new_revision_not_ready_fails_and_leaves_traffic(
    eh, ids, env_file, bq, gcloud, capsys
):
    gcloud.ready = False
    path = ids({"creative_agent": 11})
    assert _apply(eh, path, env_file) != 0
    assert gcloud.mutating() == [_expected_cmd()]  # no update-traffic
    assert gcloud.traffic == [
        {"revisionName": "creative-worker-crf-00022-old", "percent": 100}
    ]
    out = capsys.readouterr().out
    assert "not Ready" in out
    assert "creative-worker-crf-00022-old" in out  # where traffic still is


def test_new_revision_with_the_wrong_env_fails(eh, ids, env_file, bq, gcloud, capsys):
    gcloud.env_override = "something-else"
    path = ids({"creative_agent": 11})
    assert _apply(eh, path, env_file) != 0
    assert gcloud.mutating() == [_expected_cmd()]
    assert "CREATIVE_AGENT_ENGINE_ID" in capsys.readouterr().out


def test_traffic_verification_mismatch_fails_with_the_manual_fix(
    eh, ids, env_file, bq, gcloud, capsys
):
    gcloud.ignore_update_traffic = True  # update-traffic "succeeds" but nothing moves
    path = ids({"creative_agent": 11})
    assert _apply(eh, path, env_file) != 0
    new = gcloud.latest
    out = capsys.readouterr().out
    assert "update-traffic creative-worker-crf" in out
    assert f"--to-revisions {new}=100" in out
    # and the rollback to the previously serving revision
    assert "--to-revisions creative-worker-crf-00022-old=100" in out
