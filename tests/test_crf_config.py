"""Env-driven CRF config (`cloud_functions/creative_fanout/config.py`).

One frozen ``CrfConfig`` from ``load_config()``: every setting clamped
(unset/invalid -> default), read lazily (``config.X`` follows the current
environment), cross-checked (the safety margin covers the post-budget waits;
the reaper never fires while a worker can still run), and nothing raises at
import/load (a bad env var must not crash-loop the instance), except the
required project on use.
"""

import dataclasses
import logging

import pytest

from cloud_functions.creative_fanout import main
from cloud_functions.creative_fanout.config import (
    CrfConfig,
    _build,
    config,
    default_reap_minutes,
    load_config,
)


def test_project_read_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    assert config.GOOGLE_CLOUD_PROJECT == "my-proj"


def test_unset_project_raises_at_use_time_not_load(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    cfg = load_config()  # loading itself must not raise
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT is not set"):
        _ = cfg.GOOGLE_CLOUD_PROJECT


def test_blank_project_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "   ")
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        _ = config.GOOGLE_CLOUD_PROJECT


def test_project_number_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "123456789")
    assert config.GOOGLE_CLOUD_PROJECT_NUMBER == "123456789"


def test_project_number_falls_back_to_project_id(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_NUMBER", raising=False)
    assert config.GOOGLE_CLOUD_PROJECT_NUMBER == "my-proj"


def test_project_number_without_project_raises(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_NUMBER", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        _ = config.GOOGLE_CLOUD_PROJECT_NUMBER


def test_worker_user_id_default(monkeypatch):
    monkeypatch.delenv("AGENT_WORKER_USER_ID", raising=False)
    assert config.AGENT_WORKER_USER_ID == "crf_worker"


def test_worker_user_id_read_lazily(monkeypatch):
    """No import-time freeze: the accessor follows the current environment."""
    monkeypatch.setenv("AGENT_WORKER_USER_ID", "batch_bot")
    assert config.AGENT_WORKER_USER_ID == "batch_bot"


def test_region_default(monkeypatch):
    monkeypatch.delenv("GCP_REGION", raising=False)
    assert config.GCP_REGION == "us-central1"


def test_config_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        load_config().CRF_MAX_ROWS_PER_RUN = 9  # type: ignore[misc]


def test_load_config_is_cached_per_environment(monkeypatch):
    assert load_config() is load_config()
    monkeypatch.setenv("CRF_POLL_INTERVAL_SECS", "45")
    assert load_config().POLL_INTERVAL_SECS == 45.0


# ------------------------------------------------------------
# Defaults + clamps
# ------------------------------------------------------------
_TIMING_ENV = (
    "CRF_WORKER_TIMEOUT_SECS",
    "CRF_BUDGET_SAFETY_SECS",
    "CRF_STREAM_TIMEOUT_SECS",
    "CRF_POLL_INTERVAL_SECS",
    "CRF_IDLE_SECS",
    "CRF_SESSION_READ_TIMEOUT_SECS",
    "CRF_SESSION_DELETE_TIMEOUT_SECS",
    "CRF_BQ_QUERY_TIMEOUT_SECS",
    "REAP_STALE_PROCESSING_MINUTES",
    "MAX_PROCESSING_ATTEMPTS",
    "CRF_MAX_ROWS_PER_RUN",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _TIMING_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults(clean_env):
    cfg = load_config()
    assert cfg.WORKER_TIMEOUT_SECS == 1800.0
    assert cfg.BUDGET_SAFETY_SECS == 150.0
    assert cfg.STREAM_TIMEOUT_SECS == 1500.0
    assert cfg.POLL_INTERVAL_SECS == 30.0
    assert cfg.IDLE_SECS == 600.0
    assert cfg.SESSION_READ_TIMEOUT_SECS == 60.0
    assert cfg.SESSION_DELETE_TIMEOUT_SECS == 30.0
    assert cfg.BQ_QUERY_TIMEOUT_SECS == 60.0
    assert cfg.REAP_STALE_PROCESSING_MINUTES == 45
    assert cfg.MAX_PROCESSING_ATTEMPTS == 3
    assert cfg.CRF_MAX_ROWS_PER_RUN == 3
    assert cfg.ROOT_AGENT_NAME == "root_agent"


@pytest.mark.parametrize(
    ("name", "raw", "field", "expected"),
    [
        ("CRF_IDLE_SECS", "180", "IDLE_SECS", 600.0),  # give-up fallback >= 600
        ("CRF_IDLE_SECS", "99999", "IDLE_SECS", 3600.0),
        ("CRF_STREAM_TIMEOUT_SECS", "nope", "STREAM_TIMEOUT_SECS", 1500.0),
        ("CRF_STREAM_TIMEOUT_SECS", "nan", "STREAM_TIMEOUT_SECS", 1500.0),
        ("CRF_SESSION_READ_TIMEOUT_SECS", "6000", "SESSION_READ_TIMEOUT_SECS", 120.0),
        ("CRF_SESSION_DELETE_TIMEOUT_SECS", "300", "SESSION_DELETE_TIMEOUT_SECS", 30.0),
        ("CRF_WORKER_TIMEOUT_SECS", "10", "WORKER_TIMEOUT_SECS", 300.0),
        ("CRF_MAX_ROWS_PER_RUN", "0", "CRF_MAX_ROWS_PER_RUN", 1),
        ("CRF_MAX_ROWS_PER_RUN", "-4", "CRF_MAX_ROWS_PER_RUN", 1),
        ("CRF_MAX_ROWS_PER_RUN", "7", "CRF_MAX_ROWS_PER_RUN", 7),
        ("CRF_MAX_ROWS_PER_RUN", "3x", "CRF_MAX_ROWS_PER_RUN", 3),
        ("MAX_PROCESSING_ATTEMPTS", "0", "MAX_PROCESSING_ATTEMPTS", 1),
        ("MAX_PROCESSING_ATTEMPTS", "abc", "MAX_PROCESSING_ATTEMPTS", 3),
        ("REAP_STALE_PROCESSING_MINUTES", "4.5", "REAP_STALE_PROCESSING_MINUTES", 45),
    ],
)
def test_clamps_and_invalid_values(clean_env, name, raw, field, expected):
    clean_env.setenv(name, raw)
    assert getattr(load_config(), field) == expected


def test_bad_int_does_not_crash_at_import_or_load(clean_env):
    """The old import-time bare int() crash-looped the instance on a typo."""
    import pathlib

    from cloud_functions.creative_fanout import config as config_module

    # Nothing reads the environment at import any more.
    source = pathlib.Path(config_module.__file__).read_text()
    assert "int(os.environ" not in source

    clean_env.setenv("CRF_MAX_ROWS_PER_RUN", "three")
    clean_env.setenv("MAX_PROCESSING_ATTEMPTS", "")
    clean_env.setenv("REAP_STALE_PROCESSING_MINUTES", "soon")
    cfg = load_config()  # must not raise
    assert (cfg.CRF_MAX_ROWS_PER_RUN, cfg.MAX_PROCESSING_ATTEMPTS) == (3, 3)


# ------------------------------------------------------------
# Cross-checks
# ------------------------------------------------------------
def test_reaper_default_follows_the_worker_timeout(clean_env):
    clean_env.setenv("CRF_WORKER_TIMEOUT_SECS", "3600")
    assert load_config().REAP_STALE_PROCESSING_MINUTES == 60 + 15
    clean_env.setenv("CRF_WORKER_TIMEOUT_SECS", "1790")
    assert load_config().REAP_STALE_PROCESSING_MINUTES == 30 + 15
    assert default_reap_minutes(1800.0) == 45


def test_reaper_below_the_worker_timeout_falls_back(clean_env, caplog):
    clean_env.setenv("CRF_WORKER_TIMEOUT_SECS", "3600")
    clean_env.setenv("REAP_STALE_PROCESSING_MINUTES", "45")
    with caplog.at_level(logging.ERROR):
        assert load_config().REAP_STALE_PROCESSING_MINUTES == 75
    assert "REAP_STALE_PROCESSING_MINUTES=45" in caplog.text


def test_reaper_equal_to_the_worker_timeout_is_accepted(clean_env):
    clean_env.setenv("CRF_WORKER_TIMEOUT_SECS", "1800")
    clean_env.setenv("REAP_STALE_PROCESSING_MINUTES", "30")
    assert load_config().REAP_STALE_PROCESSING_MINUTES == 30


def test_reaper_above_the_worker_timeout_is_kept(clean_env):
    clean_env.setenv("REAP_STALE_PROCESSING_MINUTES", "90")
    assert load_config().REAP_STALE_PROCESSING_MINUTES == 90


def test_default_safety_covers_read_delete_and_bq(clean_env):
    cfg = load_config()
    assert cfg.BUDGET_SAFETY_SECS >= (
        cfg.SESSION_READ_TIMEOUT_SECS
        + cfg.SESSION_DELETE_TIMEOUT_SECS
        + cfg.BQ_QUERY_TIMEOUT_SECS
    )


def test_safety_below_the_waits_is_raised(clean_env, caplog):
    clean_env.setenv("CRF_BQ_QUERY_TIMEOUT_SECS", "200")
    clean_env.setenv("CRF_BUDGET_SAFETY_SECS", "150")
    with caplog.at_level(logging.ERROR):
        cfg = load_config()
    assert cfg.BUDGET_SAFETY_SECS == 60 + 30 + 200
    assert cfg.BQ_QUERY_TIMEOUT_SECS == 200.0
    assert "CRF_BUDGET_SAFETY_SECS=150" in caplog.text


def test_impossible_safety_falls_back_to_defaults(clean_env, caplog):
    """Waits that can't fit in half the worker timeout: the default timeouts
    and margin, an ERROR, and no crash."""
    clean_env.setenv("CRF_WORKER_TIMEOUT_SECS", "300")
    clean_env.setenv("CRF_BQ_QUERY_TIMEOUT_SECS", "600")
    with caplog.at_level(logging.ERROR):
        cfg = load_config()
    assert (cfg.BQ_QUERY_TIMEOUT_SECS, cfg.BUDGET_SAFETY_SECS) == (60.0, 150.0)
    assert (cfg.SESSION_READ_TIMEOUT_SECS, cfg.SESSION_DELETE_TIMEOUT_SECS) == (
        60.0,
        30.0,
    )
    assert "using the default" in caplog.text


# ------------------------------------------------------------
# The accessor: test overrides don't leak
# ------------------------------------------------------------
def test_accessor_override_and_undo(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(config, "CRF_MAX_ROWS_PER_RUN", 9)
        assert config.CRF_MAX_ROWS_PER_RUN == 9
    assert config.CRF_MAX_ROWS_PER_RUN == load_config().CRF_MAX_ROWS_PER_RUN
    # The undo left no pin: the env still drives the value.
    monkeypatch.setenv("CRF_MAX_ROWS_PER_RUN", "5")
    assert config.CRF_MAX_ROWS_PER_RUN == 5


def test_accessor_rejects_unknown_settings():
    with pytest.raises(AttributeError):
        config.NOT_A_SETTING = 1  # type: ignore[attr-defined]


def test_every_field_reachable_through_the_accessor():
    for field in dataclasses.fields(CrfConfig):
        assert getattr(config, field.name) == getattr(load_config(), field.name)


def test_no_hardcoded_identifiers_in_cloud_function_source():
    """Guard against re-introducing the author's project identifiers."""
    import pathlib

    src = pathlib.Path(main.__file__).parent
    text = "".join(p.read_text() for p in src.glob("*.py"))
    # Built from fragments so a repo-wide grep for these doesn't match this file.
    needles = ("hybrid" + "-vertex", "9349" + "03580331", "Ima_" + "CloudRun_jr")
    for needle in needles:
        assert needle not in text


# ------------------------------------------------------------
# CREATIVE_AGENT_ENGINE_ID: full resource name or bare id -> bare id
# ------------------------------------------------------------
_ENGINE = "6183205068745998336"


@pytest.fixture
def engine_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "123456789")
    monkeypatch.setenv("GCP_REGION", "us-central1")
    monkeypatch.delenv("CREATIVE_AGENT_ENGINE_ID", raising=False)
    return monkeypatch


def test_engine_id_bare(engine_env):
    engine_env.setenv("CREATIVE_AGENT_ENGINE_ID", _ENGINE)
    assert config.CREATIVE_AGENT_ENGINE_ID == _ENGINE


@pytest.mark.parametrize("project", ["123456789", "my-proj"])
def test_engine_id_full_resource_name_normalised(engine_env, project):
    engine_env.setenv(
        "CREATIVE_AGENT_ENGINE_ID",
        f"projects/{project}/locations/us-central1/reasoningEngines/{_ENGINE}",
    )
    assert config.CREATIVE_AGENT_ENGINE_ID == _ENGINE


def test_engine_id_surrounding_whitespace_ignored(engine_env):
    engine_env.setenv("CREATIVE_AGENT_ENGINE_ID", f"  {_ENGINE}\n")
    assert config.CREATIVE_AGENT_ENGINE_ID == _ENGINE


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_engine_id_unset_or_blank_is_empty(engine_env, caplog, raw):
    if raw is not None:
        engine_env.setenv("CREATIVE_AGENT_ENGINE_ID", raw)
    with caplog.at_level(logging.ERROR):
        assert load_config().CREATIVE_AGENT_ENGINE_ID == ""
    assert "CREATIVE_AGENT_ENGINE_ID" not in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        f"projects/999/locations/us-central1/reasoningEngines/{_ENGINE}",
        f"projects/123456789/locations/europe-west4/reasoningEngines/{_ENGINE}",
        f"projects/123456789/locations/us-central1/agents/{_ENGINE}",
        "projects/123456789/locations/us-central1/reasoningEngines/a.b",
        "1/../../x",
        "a b",
        "x" * 129,
    ],
)
def test_engine_id_invalid_is_empty_with_one_error(engine_env, caplog, raw):
    engine_env.setenv("CREATIVE_AGENT_ENGINE_ID", raw)
    with caplog.at_level(logging.ERROR):
        assert load_config().CREATIVE_AGENT_ENGINE_ID == ""  # never raises
        assert load_config().CREATIVE_AGENT_ENGINE_ID == ""  # cached: logged once
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "CREATIVE_AGENT_ENGINE_ID" in errors[0].getMessage()


@pytest.mark.parametrize(
    "raw",
    [_ENGINE, f"projects/my-proj/locations/us-central1/reasoningEngines/{_ENGINE}"],
)
def test_engine_id_without_project_number_is_empty_with_one_error(
    engine_env, caplog, raw
):
    """The worker builds the Reasoning Engine path from the project number, so
    an engine without it is unusable: unset + one ERROR (rows stay QUEUED)."""
    engine_env.delenv("GOOGLE_CLOUD_PROJECT_NUMBER")
    engine_env.setenv("CREATIVE_AGENT_ENGINE_ID", raw)
    _build.cache_clear()  # another test may have cached this environment
    with caplog.at_level(logging.ERROR):
        assert load_config().CREATIVE_AGENT_ENGINE_ID == ""  # never raises
        assert load_config().CREATIVE_AGENT_ENGINE_ID == ""  # cached: logged once
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "GOOGLE_CLOUD_PROJECT_NUMBER" in errors[0].getMessage()


def test_engine_id_busts_the_config_cache(engine_env):
    from cloud_functions.creative_fanout import config as config_module

    assert "CREATIVE_AGENT_ENGINE_ID" in config_module._ENV_NAMES
    engine_env.setenv("CREATIVE_AGENT_ENGINE_ID", "111")
    assert config.CREATIVE_AGENT_ENGINE_ID == "111"
    engine_env.setenv("CREATIVE_AGENT_ENGINE_ID", "222")
    assert config.CREATIVE_AGENT_ENGINE_ID == "222"


# ------------------------------------------------------------
# Worker transport: Cloud Tasks only
# ------------------------------------------------------------
_TRANSPORT_ENV = (
    "CRF_TASKS_QUEUE",
    "CRF_TASKS_LOCATION",
    "CRF_WORKER_URL",
    "CRF_TASKS_INVOKER_SA",
    "CRF_TASK_DISPATCH_DEADLINE_SECS",
    "CRF_WORKER_TIMEOUT_SECS",
    "CREATIVE_AGENT_ENGINE_ID",  # a stray shell export would log its own ERROR
)
_URL = "https://creative-worker-crf-abc-uc.a.run.app"


@pytest.fixture
def transport_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "123456789")
    monkeypatch.setenv("GCP_REGION", "us-central1")
    for name in _TRANSPORT_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_transport_defaults(transport_env):
    cfg = load_config()
    assert cfg.TASKS_QUEUE == "creative-worker-queue"
    assert cfg.TASKS_LOCATION == "us-central1"
    assert cfg.WORKER_URL == ""
    assert cfg.TASKS_INVOKER_SA == "123456789-compute@developer.gserviceaccount.com"
    assert cfg.TASK_DISPATCH_DEADLINE_SECS == 1800.0


def test_transport_names_bust_the_config_cache():
    from cloud_functions.creative_fanout import config as config_module

    for name in _TRANSPORT_ENV:
        assert name in config_module._ENV_NAMES


def test_no_pubsub_transport_settings():
    """The Pub/Sub worker path is gone: no transport switch, no worker topic."""
    from cloud_functions.creative_fanout import config as config_module

    for name in ("CRF_WORKER_TRANSPORT", "CREATIVE_WORKER_TOPIC_NAME"):
        assert name not in config_module._ENV_NAMES
    names = {f.name for f in dataclasses.fields(CrfConfig)}
    assert not names & {"WORKER_TRANSPORT", "CREATIVE_WORKER_TOPIC_NAME"}
    assert not hasattr(config_module, "TRANSPORTS")


def test_tasks_settings(transport_env, caplog):
    transport_env.setenv("CRF_WORKER_URL", f"  {_URL} ")
    transport_env.setenv("CRF_TASKS_QUEUE", "q1")
    transport_env.setenv("CRF_TASKS_LOCATION", "europe-west1")
    transport_env.setenv("CRF_TASKS_INVOKER_SA", "inv@p.iam.gserviceaccount.com")
    with caplog.at_level(logging.ERROR):
        cfg = load_config()
    assert caplog.records == []
    assert cfg.WORKER_URL == _URL
    assert cfg.TASKS_QUEUE == "q1"
    assert cfg.TASKS_LOCATION == "europe-west1"
    assert cfg.TASKS_INVOKER_SA == "inv@p.iam.gserviceaccount.com"


def test_tasks_location_follows_the_region(transport_env):
    transport_env.setenv("GCP_REGION", "europe-west4")
    assert load_config().TASKS_LOCATION == "europe-west4"


@pytest.mark.parametrize("url", [None, "", "http://insecure", "not-a-url"])
def test_tasks_settings_are_not_checked_at_load(transport_env, caplog, url):
    """The worker loads this config too and never sets CRF_WORKER_URL: no ERROR
    here (``orchestrator.dispatch`` checks the settings before sending)."""
    if url is not None:
        transport_env.setenv("CRF_WORKER_URL", url)
    with caplog.at_level(logging.ERROR):
        assert load_config().WORKER_URL == (url or "")
    assert caplog.records == []


def test_no_invoker_without_sa_or_project_number(transport_env):
    """No CRF_TASKS_INVOKER_SA and no project number: no default compute SA."""
    transport_env.delenv("GOOGLE_CLOUD_PROJECT_NUMBER")
    assert load_config().TASKS_INVOKER_SA == ""


@pytest.mark.parametrize(
    "worker_timeout, raw, expected",
    [
        (None, None, 1800.0),  # default = the worker timeout
        ("900", None, 900.0),
        ("3600", None, 1800.0),  # Cloud Tasks' HTTP maximum
        (None, "600", 600.0),
        (None, "5", 15.0),
        (None, "99999", 1800.0),
        (None, "nope", 1800.0),
    ],
)
def test_dispatch_deadline(transport_env, worker_timeout, raw, expected):
    if worker_timeout is not None:
        transport_env.setenv("CRF_WORKER_TIMEOUT_SECS", worker_timeout)
    if raw is not None:
        transport_env.setenv("CRF_TASK_DISPATCH_DEADLINE_SECS", raw)
    assert load_config().TASK_DISPATCH_DEADLINE_SECS == expected


@pytest.mark.parametrize(
    "worker_timeout, raw",
    [("3600", None), ("1800", "900"), ("901", "900")],
)
def test_worker_timeout_above_the_dispatch_deadline_logs_one_error(
    transport_env, caplog, worker_timeout, raw
):
    """C10: a deadline shorter than the worker run frees the queue's dispatch
    slot while the run continues; logged once (cached), never raised."""
    transport_env.setenv("CRF_WORKER_TIMEOUT_SECS", worker_timeout)
    if raw is not None:
        transport_env.setenv("CRF_TASK_DISPATCH_DEADLINE_SECS", raw)
    _build.cache_clear()  # another test may have cached this environment
    with caplog.at_level(logging.ERROR):
        load_config()
        load_config()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "CRF_WORKER_TIMEOUT_SECS" in message
    assert "dispatch deadline" in message


@pytest.mark.parametrize(
    "worker_timeout, raw",
    [(None, None), ("900", None), ("900", "900"), ("900", "1800")],
)
def test_worker_timeout_within_the_dispatch_deadline_is_silent(
    transport_env, caplog, worker_timeout, raw
):
    if worker_timeout is not None:
        transport_env.setenv("CRF_WORKER_TIMEOUT_SECS", worker_timeout)
    if raw is not None:
        transport_env.setenv("CRF_TASK_DISPATCH_DEADLINE_SECS", raw)
    with caplog.at_level(logging.ERROR):
        load_config()
    assert caplog.records == []
