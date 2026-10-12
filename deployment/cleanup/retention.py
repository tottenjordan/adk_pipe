"""Read-only helpers behind ``deployment/cleanup/apply.sh`` (stdlib only).

Simulates the retention policies in this folder against listings fetched by the script
(``gcloud ... --format=json`` / ``gcloud storage ls -l``) and compares policy files with a
resource's current configuration. Nothing here talks to GCP.

Subcommands (all print to stdout):

- ``ar-packages POLICY PACKAGES_FILE``: the repo's packages (one per line) a Delete rule
  of POLICY matches.
- ``ar-preview POLICY PACKAGE VERSIONS_JSON LIVE_DIGESTS_FILE``: deletion candidates for one
  package; exits 3 when a live digest is a candidate.
- ``gcs-preview AGE_DAYS LS_FILE``: count/bytes of listed objects older than AGE_DAYS.
- ``ar-same POLICY DESCRIBE_JSON`` / ``gcs-same LIFECYCLE DESCRIBE_JSON``: prints
  ``same`` / ``empty`` / ``differs``.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
LIVE_DIGEST_EXIT = 3


def duration_seconds(value: str) -> int:
    """``"14d"`` / ``"1209600s"`` / ``"1209600.000s"`` -> seconds."""
    m = re.fullmatch(r"(\d+)(?:\.0+)?([smhd])", str(value).strip())
    if not m:
        raise ValueError(f"bad duration {value!r}")
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


def parse_time(value: str) -> datetime:
    """RFC 3339 (any fractional precision, ``Z`` or offset) -> aware datetime."""
    value = value.strip().replace("Z", "+00:00")
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _prefixed(package: str, prefixes: list[str]) -> bool:
    return any(package.startswith(p) for p in prefixes)


def matching_packages(policy: list[dict], packages: list[str]) -> list[str]:
    """Packages any Delete rule of ``policy`` would act on."""
    prefixes = [
        p
        for rule in policy
        if rule["action"]["type"] == "Delete"
        for p in rule["condition"].get("packageNamePrefixes", [])
    ]
    return [pkg for pkg in packages if _prefixed(pkg, prefixes)]


def ar_candidates(
    policy: list[dict], package: str, versions: list[dict], now: datetime
) -> tuple[list[dict], int]:
    """Versions of ``package`` the policy would delete, plus how many it would keep.

    Mirrors Artifact Registry semantics for the rule shapes used here: a version is
    deleted when it matches a Delete rule (``tagState`` + ``olderThan`` on createTime) and
    no Keep rule (``mostRecentVersions`` newest ``keepCount`` by createTime). Keep wins.
    Docker manifests referenced by a parent index are never deleted by AR; the listing
    can't tell, so the count may slightly overstate.
    """
    ordered = sorted(versions, key=lambda v: parse_time(v["createTime"]), reverse=True)
    protected: set[str] = set()
    for rule in policy:
        recent = rule.get("mostRecentVersions")
        if rule["action"]["type"] == "Keep" and recent:
            if _prefixed(package, recent.get("packageNamePrefixes", [])):
                protected |= {v["version"] for v in ordered[: recent["keepCount"]]}
    candidates = []
    for version in ordered:
        if version["version"] in protected:
            continue
        for rule in policy:
            cond = rule.get("condition", {})
            if rule["action"]["type"] != "Delete":
                continue
            if not _prefixed(package, cond.get("packageNamePrefixes", [])):
                continue
            state = cond.get("tagState", "any")
            tagged = bool(version.get("tags"))
            if (state == "untagged" and tagged) or (state == "tagged" and not tagged):
                continue
            age = now - parse_time(version["createTime"])
            if "olderThan" in cond and age < timedelta(
                seconds=duration_seconds(cond["olderThan"])
            ):
                continue
            candidates.append(version)
            break
    return candidates, len(ordered) - len(candidates)


def gcs_older_than(ls_text: str, age_days: int, now: datetime) -> tuple[int, int, int]:
    """(matching objects, their bytes, all listed objects) from ``gcloud storage ls -l``."""
    cutoff = now - timedelta(days=age_days)
    count = size = total = 0
    for line in ls_text.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[2].startswith("gs://") or not parts[0].isdigit():
            continue
        total += 1
        if parse_time(parts[1]) < cutoff:
            count += 1
            size += int(parts[0])
    return count, size, total


def _enum(value) -> str:
    if isinstance(value, dict):
        value = value.get("type", "")
    return str(value).lower()


def normalise_ar(policies) -> list[tuple]:
    """Policy file (list) or ``describe`` ``cleanupPolicies`` (map) -> comparable form."""
    if isinstance(policies, dict):
        policies = [{"name": k, **v} for k, v in policies.items()]
    out = []
    for rule in policies or []:
        cond = rule.get("condition") or {}
        recent = rule.get("mostRecentVersions") or {}
        out.append(
            (
                rule.get("name") or rule.get("id"),
                _enum(rule.get("action")),
                _enum(cond.get("tagState", "")),
                duration_seconds(cond["olderThan"]) if "olderThan" in cond else None,
                tuple(sorted(cond.get("packageNamePrefixes", []))),
                tuple(sorted(recent.get("packageNamePrefixes", []))),
                int(recent["keepCount"]) if "keepCount" in recent else None,
            )
        )
    return sorted(out, key=repr)


def normalise_lifecycle(config) -> list[str]:
    rules = (config or {}).get("rule") or []
    return sorted(json.dumps(r, sort_keys=True) for r in rules)


def compare(wanted, current) -> str:
    if not current:
        return "empty"
    return "same" if wanted == current else "differs"


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def main(argv: list[str]) -> int:
    cmd, args = argv[0], argv[1:]
    now = datetime.now(UTC)
    if cmd == "ar-packages":
        policy = json.loads(Path(args[0]).read_text())
        packages = Path(args[1]).read_text().split()
        print("\n".join(matching_packages(policy, packages)))
    elif cmd == "ar-preview":
        policy = json.loads(Path(args[0]).read_text())
        package = args[1]
        versions = json.loads(Path(args[2]).read_text() or "[]")
        live = set(Path(args[3]).read_text().split())
        candidates, kept = ar_candidates(policy, package, versions, now)
        size = sum(
            int(v.get("metadata", {}).get("imageSizeBytes", 0)) for v in candidates
        )
        print(
            f"  {package}: {len(versions)} versions -> delete {len(candidates)}"
            f" (~{_human(size)} manifest-reported), keep {kept}"
        )
        for v in candidates[:3]:
            print(f"    e.g. {v['version']}  created {v['createTime'][:10]}")
        hits = sorted(live & {v["version"] for v in candidates})
        for digest in hits:
            print(f"  !! LIVE DIGEST WOULD BE DELETED: {package}@{digest}")
        if hits:
            return LIVE_DIGEST_EXIT
    elif cmd == "gcs-preview":
        count, size, total = gcs_older_than(
            Path(args[1]).read_text(), int(args[0]), now
        )
        print(f"    {total} objects; older than {args[0]}d: {count} ({_human(size)})")
    elif cmd == "ar-same":
        wanted = normalise_ar(json.loads(Path(args[0]).read_text()))
        current = json.loads(Path(args[1]).read_text() or "{}") or {}
        print(compare(wanted, normalise_ar(current.get("cleanupPolicies"))))
    elif cmd == "gcs-same":
        wanted = normalise_lifecycle(json.loads(Path(args[0]).read_text()))
        current = json.loads(Path(args[1]).read_text() or "{}") or {}
        print(compare(wanted, normalise_lifecycle(current.get("lifecycle_config"))))
    else:
        print(f"unknown command {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
