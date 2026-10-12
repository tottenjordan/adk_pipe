"""Guards for the trend-trawler retention policies in ``deployment/cleanup/``.

The Artifact Registry repo ``cloud-run-source-deploy`` and the Cloud Run source bucket are
shared with other projects in the GCP project, so every rule must be scoped to
trend-trawler's own packages / prefixes, delete only untagged registry versions and never
touch the bucket prefixes live Agent Engines, bandit checkpoints, run folders or frozen
shares depend on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

CLEANUP_DIR = Path(__file__).resolve().parent.parent / "deployment" / "cleanup"

TT_PACKAGES = {
    "trend-trawler-api",
    "trend-trawler-web",
    "trend-trawler-share",
    "creative-trawler-crf",
    "creative-worker-crf",
    "trend-trawler-bandit",
}
# Other projects' packages in the shared repos (not exhaustive; encodes the intent).
UNRELATED_PACKAGES = [
    "omnimash",
    "omnimash-api",
    "nova-web",
    "nova-api",
    "janus-api",
    "squeeze-the-juice",
    "cpr-predict-proba",
]
MIN_KEEP = {"ar-cloud-run-source-deploy.json": 10, "ar-cpr.json": 5}
MIN_DELETE_AGE_DAYS = 14

GCS_PREFIX_ALLOWLIST = {
    "person-refs/",
    "adk-pipe/cloudbuild/",
    "services/trend-trawler-api/",
    "services/trend-trawler-web/",
    "services/trend-trawler-share/",
    "services/creative-trawler-crf/",
    "services/creative-worker-crf/",
}
PROTECTED_GCS_PREFIXES = ("bandit/", "shares/", "test_imgs/", "adk-pipe/")


def _load(name: str):
    return json.loads((CLEANUP_DIR / name).read_text())


def _files(prefix: str) -> list[str]:
    return sorted(p.name for p in CLEANUP_DIR.glob(f"{prefix}*.json"))


AR_FILES = _files("ar-")
GCS_FILES = _files("gcs-")


def _days(duration: str) -> float:
    m = re.fullmatch(r"(\d+)([smhd])", duration)
    assert m, f"bad duration {duration!r}"
    n, unit = int(m.group(1)), m.group(2)
    return n / {"s": 86400, "m": 1440, "h": 24, "d": 1}[unit]


def test_expected_files_exist():
    assert AR_FILES == ["ar-cloud-run-source-deploy.json", "ar-cpr.json"]
    assert GCS_FILES == ["gcs-run-sources.json", "gcs-trend-trawler-deploy-ae.json"]


@pytest.mark.parametrize("name", AR_FILES)
def test_ar_rules_are_well_formed(name):
    rules = _load(name)
    assert isinstance(rules, list) and rules
    names = [r["name"] for r in rules]
    assert len(names) == len(set(names))
    for rule in rules:
        assert rule["action"]["type"] in {"Delete", "Keep"}
        assert set(rule) <= {"name", "action", "condition", "mostRecentVersions"}


@pytest.mark.parametrize("name", AR_FILES)
def test_ar_delete_rules_are_scoped_untagged_and_old(name):
    deletes = [r for r in _load(name) if r["action"]["type"] == "Delete"]
    assert deletes
    for rule in deletes:
        cond = rule["condition"]
        prefixes = cond.get("packageNamePrefixes")
        # An empty/missing prefix list would match every package in the shared repo.
        assert prefixes, f"{rule['name']}: packageNamePrefixes required"
        assert set(prefixes) <= TT_PACKAGES
        for prefix in prefixes:
            for other in UNRELATED_PACKAGES:
                assert not other.startswith(prefix), (prefix, other)
        assert cond["tagState"] == "untagged"
        assert _days(cond["olderThan"]) >= MIN_DELETE_AGE_DAYS
        assert not {"tagPrefixes", "versionNamePrefixes"} & set(cond)


@pytest.mark.parametrize("name", AR_FILES)
def test_ar_keep_most_recent_covers_deleted_packages(name):
    rules = _load(name)
    deleted = {
        p
        for r in rules
        if r["action"]["type"] == "Delete"
        for p in r["condition"]["packageNamePrefixes"]
    }
    keeps = [
        r["mostRecentVersions"]
        for r in rules
        if r["action"]["type"] == "Keep" and "mostRecentVersions" in r
    ]
    assert keeps
    kept: set[str] = set()
    for keep in keeps:
        assert keep["keepCount"] >= MIN_KEEP[name]
        assert set(keep["packageNamePrefixes"]) <= TT_PACKAGES
        kept |= set(keep["packageNamePrefixes"])
    assert deleted <= kept


@pytest.mark.parametrize("name", GCS_FILES)
def test_gcs_rules_are_prefix_scoped_and_skip_protected_data(name):
    rules = _load(name)["rule"]
    assert rules
    for rule in rules:
        assert rule["action"]["type"] == "Delete"
        prefixes = rule["condition"].get("matchesPrefix")
        assert prefixes, "every bucket rule needs matchesPrefix"
        assert rule["condition"]["age"] >= 30
        for prefix in prefixes:
            assert prefix in GCS_PREFIX_ALLOWLIST, prefix
            # A trailing slash keeps e.g. creative-trawler-crf/ off creative-trawler-crf-vx/.
            assert prefix.endswith("/")
            assert not re.match(r"^\d{4}_", prefix), "run folders are never expired"
            if prefix != "adk-pipe/cloudbuild/":
                assert not prefix.startswith(PROTECTED_GCS_PREFIXES), prefix


def test_person_refs_backstop_is_365_days():
    rules = _load("gcs-trend-trawler-deploy-ae.json")["rule"]
    ages = [
        r["condition"]["age"]
        for r in rules
        if "person-refs/" in r["condition"]["matchesPrefix"]
    ]
    assert ages == [365]


def test_run_sources_rule_covers_only_trend_trawler_services():
    rules = _load("gcs-run-sources.json")["rule"]
    prefixes = {p for r in rules for p in r["condition"]["matchesPrefix"]}
    assert prefixes == {
        f"services/{p}/" for p in TT_PACKAGES - {"trend-trawler-bandit"}
    }
