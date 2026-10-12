"""Guards for the trend-trawler retention policies in ``deployment/cleanup/``.

The Artifact Registry repo ``cloud-run-source-deploy`` and the Cloud Run source bucket are
shared with other projects in the GCP project, so every rule must be scoped to
trend-trawler's own packages / prefixes, delete only untagged registry versions and never
touch the bucket prefixes live Agent Engines, bandit checkpoints, run folders or frozen
shares depend on.
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

CLEANUP_DIR = Path(__file__).resolve().parent.parent / "deployment" / "cleanup"

TT_PACKAGES = {
    "trend-trawler-api",
    "trend-trawler-web",
    "trend-trawler-share",
    "creative-trawler-crf",
    "creative-worker-crf",
    "creative-fanout-crf",
    "trend-trawler-bandit",
}
# Registry-only packages (built by Cloud Build, no Cloud Run source upload).
IMAGE_ONLY_PACKAGES = {"trend-trawler-bandit", "creative-fanout-crf"}
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


def test_cloud_run_repo_rules_cover_every_cloud_run_package():
    """Both rules (delete untagged + keep newest) name every trend-trawler package
    in cloud-run-source-deploy, incl. the shared CRF image ``creative-fanout-crf``."""
    expected = TT_PACKAGES - {"trend-trawler-bandit"}
    for rule in _load("ar-cloud-run-source-deploy.json"):
        spec = rule.get("condition") or rule.get("mostRecentVersions")
        assert set(spec["packageNamePrefixes"]) == expected, rule["name"]


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
    assert prefixes == {f"services/{p}/" for p in TT_PACKAGES - IMAGE_ONLY_PACKAGES}


# --- the read-only preview helpers behind apply.sh ---------------------------------

NOW = datetime(2026, 10, 12, tzinfo=UTC)


def _retention():
    spec = importlib.util.spec_from_file_location(
        "tt_retention", CLEANUP_DIR / "retention.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _version(digest: str, days_old: int) -> dict:
    created = (NOW - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    return {"version": digest, "tags": [], "createTime": created}


def test_ar_candidates_keep_newest_and_tagged():
    r = _retention()
    policy = _load("ar-cpr.json")
    versions = [_version(f"sha256:{i:02d}", days_old=i) for i in range(20)]
    versions[19]["tags"] = ["buildcache"]
    candidates, kept = r.ar_candidates(policy, "trend-trawler-bandit", versions, NOW)
    # Newest 5 kept, untagged > 14 days deleted (day 14 is a hair younger: the
    # fractional createTime), the tagged one never.
    assert {v["version"] for v in candidates} == {
        f"sha256:{i:02d}" for i in range(15, 19)
    }
    assert kept == 16
    # Packages outside the prefixes are untouched.
    assert r.ar_candidates(policy, "cpr-predict-proba", versions, NOW)[0] == []


def test_matching_packages_only_trend_trawler():
    r = _retention()
    pkgs = ["omnimash", "trend-trawler-api", "creative-worker-crf", "nova-web"]
    assert r.matching_packages(_load("ar-cloud-run-source-deploy.json"), pkgs) == [
        "trend-trawler-api",
        "creative-worker-crf",
    ]


def test_gcs_older_than_parses_ls_long():
    r = _retention()
    text = (
        "  100  2026-01-01T00:00:00Z  gs://b/services/trend-trawler-api/a.zip\n"
        "  200  2026-10-11T00:00:00Z  gs://b/services/trend-trawler-api/b.zip\n"
        "TOTAL: 2 objects, 300 bytes (300B)\n"
    )
    assert r.gcs_older_than(text, 30, NOW) == (1, 100, 2)


def test_policy_comparison_handles_api_shape():
    r = _retention()
    wanted = r.normalise_ar(_load("ar-cpr.json"))
    described = {
        "tt-delete-untagged-older-than-14d": {
            "id": "tt-delete-untagged-older-than-14d",
            "action": "DELETE",
            "condition": {
                "tagState": "UNTAGGED",
                "olderThan": "1209600s",
                "packageNamePrefixes": ["trend-trawler-bandit"],
            },
        },
        "tt-keep-newest-5": {
            "id": "tt-keep-newest-5",
            "action": "KEEP",
            "mostRecentVersions": {
                "packageNamePrefixes": ["trend-trawler-bandit"],
                "keepCount": 5,
            },
        },
    }
    assert r.compare(wanted, r.normalise_ar(described)) == "same"
    assert r.compare(wanted, r.normalise_ar(None)) == "empty"
    described["tt-keep-newest-5"]["mostRecentVersions"]["keepCount"] = 3
    assert r.compare(wanted, r.normalise_ar(described)) == "differs"
    lifecycle = r.normalise_lifecycle(_load("gcs-run-sources.json"))
    assert r.compare(lifecycle, lifecycle) == "same"
    assert r.compare(lifecycle, r.normalise_lifecycle(None)) == "empty"


def _described(policy: list[dict]) -> dict:
    """A policy file -> the ``describe`` ``cleanupPolicies`` map shape."""
    return {rule["name"]: {"id": rule["name"], **rule} for rule in policy}


def test_ar_verdict_no_current_policies_applies():
    r = _retention()
    wanted = r.normalise_ar(_load("ar-cpr.json"))
    assert r.ar_verdict(wanted, r.normalise_ar(None)) == "empty"
    assert r.ar_verdict(wanted, r.normalise_ar({})) == "empty"


def test_ar_verdict_identical_is_same():
    r = _retention()
    policy = _load("ar-cpr.json")
    wanted = r.normalise_ar(policy)
    assert r.ar_verdict(wanted, r.normalise_ar(_described(policy))) == "same"


def test_ar_verdict_all_owned_but_different_is_ours_with_diff():
    r = _retention()
    policy = _load("ar-cloud-run-source-deploy.json")
    wanted = r.normalise_ar(policy)
    # Today's live state: our own rules before a package was added to the file.
    old = json.loads(json.dumps(policy))
    for rule in old:
        cond = rule.get("condition") or rule["mostRecentVersions"]
        cond["packageNamePrefixes"].remove("creative-fanout-crf")
    current = r.normalise_ar(_described(old))
    assert r.ar_verdict(wanted, current) == "ours"
    diff = r.ar_diff(wanted, current)
    assert any(line.startswith("- ") for line in diff)
    assert any(line.startswith("+ ") and "creative-fanout-crf" in line for line in diff)
    assert r.ar_diff(wanted, wanted) == []


def test_ar_verdict_foreign_policy_refuses():
    r = _retention()
    policy = _load("ar-cpr.json")
    wanted = r.normalise_ar(policy)
    foreign = {
        "id": "omnimash-keep-3",
        "action": "KEEP",
        "mostRecentVersions": {"packageNamePrefixes": ["omnimash"], "keepCount": 3},
    }
    # Alongside ours (identical) or alone: any non-tt- name refuses.
    described = {**_described(policy), "omnimash-keep-3": foreign}
    assert r.ar_verdict(wanted, r.normalise_ar(described)) == "differs"
    alone = {"omnimash-keep-3": foreign}
    assert r.ar_verdict(wanted, r.normalise_ar(alone)) == "differs"
