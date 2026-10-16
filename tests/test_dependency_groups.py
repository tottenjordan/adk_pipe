"""Guards for the slim runtime dependency set (pyproject.toml / requirements.txt).

Everything in ``[project] dependencies`` ships to Agent Engine (requirements.txt is
its install list) and the api image (``uv sync --no-dev``), so eval, experiments
and Cloud-Function-only packages live in dependency groups instead.
"""

import importlib.metadata
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())

# Never imported by runtime code (agent packages, runserver, deployment/async_app).
NOT_RUNTIME = {
    "cloudevents",
    "db-dtypes",
    "functions-framework",
    "google-cloud-pubsub",
    "kaleido",
    "litellm",
    "matplotlib",
    "nltk",
    "pandas",
    "plotly",
    "pyarrow",
    "scikit-learn",
    "scipy",
    "tabulate",
}


def _group(name: str) -> list[Requirement]:
    return [
        Requirement(r)
        for r in PYPROJECT["dependency-groups"][name]
        if isinstance(r, str)
    ]


def test_runtime_dependencies_exclude_eval_and_experiment_packages():
    reqs = [Requirement(r) for r in PYPROJECT["project"]["dependencies"]]
    names = {canonicalize_name(r.name) for r in reqs}
    assert not names & NOT_RUNTIME
    adk = next(r for r in reqs if r.name == "google-adk")
    assert "eval" not in adk.extras
    aiplatform = next(r for r in reqs if r.name == "google-cloud-aiplatform")
    assert "evaluation" not in aiplatform.extras


def test_requirements_txt_has_no_eval_or_experiment_packages():
    pins = {
        canonicalize_name(line.split("==")[0])
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if "==" in line and not line.startswith((" ", "#"))
    }
    assert not pins & NOT_RUNTIME
    assert "google-cloud-aiplatform" in pins


def test_requirements_txt_has_no_local_project_lines():
    """The project is an editable install (`[tool.uv] package = true`), so a plain
    `uv export` starts with `-e .`; Agent Engine cannot install that. The export
    must use --no-emit-project, and its header must record that command."""
    text = (ROOT / "requirements.txt").read_text()
    local = [
        line
        for line in text.splitlines()
        if line.lstrip().startswith("-e") or "file:" in line
    ]
    assert not local, f"requirements.txt has local-project lines: {local}"
    assert "--no-emit-project" in text.splitlines()[1]


def test_eval_group_mirrors_google_adk_eval_extra():
    """The eval group lists google-adk's `eval` extra itself (that extra caps
    google-cloud-aiplatform <2); a new package in the extra must be added here."""
    extra = {
        canonicalize_name(Requirement(r).name)
        for r in importlib.metadata.requires("google-adk") or []
        if Requirement(r).marker is not None
        and Requirement(r).marker.evaluate({"extra": "eval"})
        and not Requirement(r).marker.evaluate({"extra": ""})
    }
    group = {canonicalize_name(r.name): r for r in _group("eval")}
    assert extra
    assert extra <= set(group)
    assert "evaluation" in group["google-cloud-aiplatform"].extras


def test_dev_group_includes_eval_and_experiments():
    includes = {
        r["include-group"]
        for r in PYPROJECT["dependency-groups"]["dev"]
        if isinstance(r, dict)
    }
    assert {"eval", "experiments"} <= includes
