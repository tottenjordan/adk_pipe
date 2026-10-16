"""Guards for the repo-root Makefile (developer shortcuts).

Every target must be documented (a ``## description`` on its rule line, which
``make help`` prints) and listed in ``.PHONY`` (none of them builds a file, so a
stray file named like a target must never make it a no-op). ``make -n`` (dry
run) must succeed for every target that needs no required variable, so a typo in
a recipe fails here rather than on someone's laptop.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"

# Targets that refuse to run (even as a dry run) without these variables.
REQUIRED_VARS = {
    "deploy-engines": {
        "CREATIVE_VERSION": "v1",
        "INTERACTIVE_VERSION": "v1",
        "SCOUT_VERSION": "v1",
    },
}

_RULE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)\s*:(?![=:])(.*)$")


def _rules() -> dict[str, str]:
    """Target name -> the rest of its rule line (prerequisites + ``## doc``)."""
    rules: dict[str, str] = {}
    for line in MAKEFILE.read_text().splitlines():
        m = _RULE.match(line)
        if m and m.group(1) != "PHONY":
            rules[m.group(1)] = m.group(2)
    return rules


def _phony() -> set[str]:
    text = MAKEFILE.read_text().replace("\\\n", " ")
    names: set[str] = set()
    for m in re.finditer(r"^\.PHONY\s*:(.*)$", text, re.MULTILINE):
        names.update(m.group(1).split())
    return names


TARGETS = sorted(_rules())


def test_makefile_has_the_expected_targets():
    expected = {
        "help",
        "sync",
        "fmt",
        "lint",
        "test",
        "test-fast",
        "test-crf",
        "frontend-test",
        "frontend-dev",
        "dev-backend",
        "eval-scout",
        "eval-creative",
        "deploy-engines",
        "list-engines",
        "bandit-image",
    }
    assert expected <= set(TARGETS)


def test_help_is_the_default_goal():
    text = MAKEFILE.read_text()
    assert re.search(r"^\.DEFAULT_GOAL\s*:=\s*help\s*$", text, re.MULTILINE)


@pytest.mark.parametrize("target", TARGETS)
def test_every_target_is_documented(target):
    doc = _rules()[target].partition("##")[2].strip()
    assert doc, f"{target}: add a '## description' to its rule line"


@pytest.mark.parametrize("target", TARGETS)
def test_every_target_is_phony(target):
    assert target in _phony(), f"{target}: add it to .PHONY"


def _make(*args: str, extra_env: dict[str, str] | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MAKE")}
    for var in ("CREATIVE_VERSION", "INTERACTIVE_VERSION", "SCOUT_VERSION"):
        env.pop(var, None)
    env.update(extra_env or {})
    return subprocess.run(
        ["make", "--no-print-directory", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


needs_make = pytest.mark.skipif(
    shutil.which("make") is None, reason="make not installed"
)


@needs_make
@pytest.mark.parametrize("target", TARGETS)
def test_dry_run_succeeds(target):
    variables = [f"{k}={v}" for k, v in REQUIRED_VARS.get(target, {}).items()]
    result = _make("-n", target, *variables)
    assert result.returncode == 0, result.stderr


@needs_make
@pytest.mark.parametrize("target", sorted(REQUIRED_VARS))
def test_required_variables_fail_clearly(target):
    result = _make("-n", target)
    assert result.returncode != 0
    for var in REQUIRED_VARS[target]:
        assert var in result.stderr


@needs_make
def test_help_lists_every_target():
    result = _make("help")
    assert result.returncode == 0, result.stderr
    for target in TARGETS:
        assert re.search(rf"^\s*{re.escape(target)}\s", result.stdout, re.MULTILINE), (
            target
        )
