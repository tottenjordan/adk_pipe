"""Env-driven CRF config (`cloud_functions/creative_fanout/config.py`).

The cloud functions must not hardcode a GCP project: the project is a required
env var read at *use* time (so importing stays credential-free), the project
number falls back to the project ID, and the worker's session user ID is
configurable with a neutral default.
"""

import importlib

import pytest

from cloud_functions.creative_fanout import config as config_module
from cloud_functions.creative_fanout import main


def _reload_config():
    """Re-evaluate the class-level `os.environ.get` defaults.

    NB: `main.config` keeps the ORIGINAL `AppConfig` instance after a reload
    (it was bound at import), so only assert on the returned instance here.
    Env-read properties (project / number) are still live on `main.config`.
    """
    return importlib.reload(config_module).config


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    importlib.reload(config_module)


def test_project_read_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    assert config_module.config.GOOGLE_CLOUD_PROJECT == "my-proj"


def test_unset_project_raises_at_use_time_not_import(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    cfg = _reload_config()  # import/reload itself must not raise
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT is not set"):
        _ = cfg.GOOGLE_CLOUD_PROJECT


def test_blank_project_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "   ")
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        _ = config_module.config.GOOGLE_CLOUD_PROJECT


def test_project_number_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "123456789")
    assert config_module.config.GOOGLE_CLOUD_PROJECT_NUMBER == "123456789"


def test_project_number_falls_back_to_project_id(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_NUMBER", raising=False)
    assert config_module.config.GOOGLE_CLOUD_PROJECT_NUMBER == "my-proj"


def test_project_number_without_project_raises(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_NUMBER", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        _ = config_module.config.GOOGLE_CLOUD_PROJECT_NUMBER


def test_worker_user_id_default(monkeypatch):
    monkeypatch.delenv("AGENT_WORKER_USER_ID", raising=False)
    assert _reload_config().AGENT_WORKER_USER_ID == "crf_worker"


def test_worker_user_id_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_WORKER_USER_ID", "batch_bot")
    assert _reload_config().AGENT_WORKER_USER_ID == "batch_bot"


def test_region_and_topic_defaults(monkeypatch):
    monkeypatch.delenv("GCP_REGION", raising=False)
    monkeypatch.delenv("CREATIVE_WORKER_TOPIC_NAME", raising=False)
    cfg = _reload_config()
    assert cfg.GCP_REGION == "us-central1"
    assert cfg.CREATIVE_WORKER_TOPIC_NAME == "creative-worker-queue-topic"


def test_worker_topic_path_resolved_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_NUMBER", "42")
    assert main._worker_topic_path() == (
        f"projects/42/topics/{main.config.CREATIVE_WORKER_TOPIC_NAME}"
    )


def test_no_hardcoded_identifiers_in_cloud_function_source():
    """Guard against re-introducing the author's project identifiers."""
    import pathlib

    src = pathlib.Path(main.__file__).parent
    text = "".join(p.read_text() for p in src.glob("*.py"))
    # Built from fragments so a repo-wide grep for these doesn't match this file.
    needles = ("hybrid" + "-vertex", "9349" + "03580331", "Ima_" + "CloudRun_jr")
    for needle in needles:
        assert needle not in text
