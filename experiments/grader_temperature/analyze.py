"""Analyse the re-judged / re-inspected creatives against the PRE-REGISTERED rule.

Reads ``data/selection.json`` (the human ratings), ``data/state/`` (to list each
session's creatives), ``results/judge/`` and ``results/qa/``. Pure and offline.

Creatives are matched like the calibration (``runserver.ratings.creative_index``
+ ``_find_eval``: visuals by concept name, copies by headline then
original_id). Failed judge calls (``"evaluation_failed"``) are excluded.

Per temperature:
- judge: pass flip rate (share of creatives with >= 2 valid repeats whose
  ``passed`` is not unanimous), gate flip rate (any non-advisory gate not
  unanimous; a gate missing from a repeat counts as not unanimous), per-gate
  flip counts, mean per-creative SD of the overall score, majority verdict
  (strict majority of valid repeats; a tie is no verdict), Cohen's kappa +
  agreement of the majority vs the human verdict, Spearman of the mean score vs
  the human 1-5 score;
- image QA: rule flip rate (failed-rule set not identical across repeats),
  kappa of the majority QA pass vs the human visual verdict, and as context the
  overlap of human fail reasons (product_not_visible / text_problem /
  unwanted_logo) with the majority QA rules.
Bootstrap 95% CIs of (candidate − baseline), resampling creatives (rating
pairs for kappa), are reported for context; the rule uses point estimates.

The rule (fixed before any live run; baseline = current production):
- Judge adopt 1.0 iff pass-flip(1.0) <= pass-flip(0.3) + 0.05 AND
  gate-flip(1.0) <= gate-flip(0.3) + 0.05 AND kappa(1.0) >= kappa(0.3) - 0.05.
- QA adopt 1.0 iff rule-flip(1.0) <= rule-flip(0) + 0.05 AND
  kappa(1.0) >= kappa(0) - 0.05.
Each check is PASS / FAIL / N/A (uncomputable on a side). Verdict per grader:
INCONCLUSIVE if any check is N/A, else ADOPT if all PASS, else KEEP;
INCONCLUSIVE keeps the current temperature.

Usage:
    PYTHONPATH="$PWD" uv run python -m experiments.grader_temperature.analyze
"""

from __future__ import annotations

import argparse
import csv
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import (
    EVALUATION_FAILED,
    JUDGE_DIR,
    QA_DIR,
    RESULTS_DIR,
    SELECTION_PATH,
    STATE_DIR,
    load_selection,
    load_state,
    parse_run_filename,
    parse_temps,
    temp_label,
)

MARGIN_FLIP = 0.05
MARGIN_KAPPA = 0.05
# Float tolerance for the threshold comparisons (0.15 - 0.1 != 0.05 exactly).
EPS = 1e-9
N_RESAMPLES = 10_000
SEED = 20261015

# Human fail reasons with a matching image-QA rule (context only).
REASON_RULES: dict[str, frozenset[str]] = {
    "product_not_visible": frozenset({"product not visible"}),
    "text_problem": frozenset(
        {"gibberish text", "in-image text not exact", "in-image text not legible"}
    ),
    "unwanted_logo": frozenset({"unrequested third-party logo"}),
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # PASS | FAIL | N/A
    detail: str


# --- loading -------------------------------------------------------------------


def load_judge_runs(root: Path = JUDGE_DIR) -> dict[str, dict[float, list[dict]]]:
    """``{sid: {temperature: [run, ...] by repeat}}`` from ``results/judge/``."""
    import json

    out: dict[str, dict[float, list[dict]]] = defaultdict(lambda: defaultdict(list))
    if not root.exists():
        return {}
    for path in sorted(root.glob("*/*.json")):
        parsed = parse_run_filename(path.name)
        if parsed is None:
            continue
        run = json.loads(path.read_text())
        run.setdefault("repeat", parsed[1])
        out[path.parent.name][parsed[0]].append(run)
    for by_t in out.values():
        for runs in by_t.values():
            runs.sort(key=lambda r: r["repeat"])
    return {sid: dict(by_t) for sid, by_t in out.items()}


def load_qa_runs(root: Path = QA_DIR) -> dict[str, dict[str, dict[float, list[dict]]]]:
    """``{sid: {concept_name: {temperature: [record, ...]}}}`` from ``results/qa/``."""
    import json

    out: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    if not root.exists():
        return {}
    for path in sorted(root.glob("*/*/*.json")):
        parsed = parse_run_filename(path.name)
        if parsed is None:
            continue
        rec = json.loads(path.read_text())
        out[rec["session_id"]][rec["concept_name"]][parsed[0]].append(rec)
    return {
        sid: {name: dict(by_t) for name, by_t in by_c.items()}
        for sid, by_c in out.items()
    }


# --- matching ------------------------------------------------------------------


def creative_evals(runs: Sequence[Mapping[str, Any]], info: Mapping[str, Any]) -> list:
    """The creative's valid evaluation in each repeat (unmatched / failed dropped)."""
    from runserver.ratings import _find_eval

    out = []
    for run in runs:
        ev = _find_eval(run, info)
        if ev is None:
            continue
        score = ev.get("score") or {}
        if EVALUATION_FAILED in (score.get("improvements") or []):
            continue
        out.append(ev)
    return out


# --- per-creative maths --------------------------------------------------------


def majority(values: Sequence[bool]) -> bool | None:
    """Strict majority of the values; None on a tie or no values."""
    yes = sum(1 for v in values if v)
    no = len(values) - yes
    if yes == no:
        return None
    return yes > no


def flipped(values: Sequence[Any]) -> bool | None:
    """Whether the values are not unanimous; None with fewer than 2 values."""
    if len(values) < 2:
        return None
    return len({_hashable(v) for v in values}) > 1


def _hashable(value: Any) -> Any:
    if isinstance(value, list | set | frozenset | tuple):
        return frozenset(value)
    return value


def flipped_gates(evals: Sequence[Mapping[str, Any]]) -> list[str] | None:
    """Non-advisory gates whose verdict is not unanimous (missing = a distinct value)."""
    if len(evals) < 2:
        return None
    maps: list[dict[str, bool]] = []
    advisory: set[str] = set()
    for ev in evals:
        gates = (ev.get("score") or {}).get("gates") or []
        maps.append({g["gate"]: bool(g.get("passed")) for g in gates})
        advisory |= {g["gate"] for g in gates if g.get("advisory")}
    names = sorted(set().union(*maps) - advisory)
    return [n for n in names if flipped([m.get(n) for m in maps])]


def judge_stats(evals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One creative's stability across repeats at one temperature."""
    passed = [bool(e["score"]["passed"]) for e in evals]
    scores = [float(e["score"]["overall_score"]) for e in evals]
    gates = flipped_gates(evals)
    return {
        "n_valid": len(evals),
        "pass_flip": flipped(passed),
        "gate_flip": None if gates is None else bool(gates),
        "flipped_gates": gates or [],
        "majority": majority(passed),
        "score_sd": statistics.stdev(scores) if len(scores) >= 2 else None,
        "mean_score": statistics.fmean(scores) if scores else None,
    }


def qa_stats(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One image's stability across repeats at one temperature."""
    rules = [list(r.get("failed_rules") or []) for r in records]
    counts: dict[str, int] = defaultdict(int)
    for rs in rules:
        for rule in set(rs):
            counts[rule] += 1
    return {
        "n_valid": len(rules),
        "rule_flip": flipped(rules),
        "majority_pass": majority([not rs for rs in rules]),
        "majority_rules": sorted(r for r, c in counts.items() if 2 * c > len(rules)),
    }


def rate(values: Iterable[bool | None]) -> float | None:
    vals = [bool(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def mean_or_none(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.fmean(vals) if vals else None


# --- aggregation ---------------------------------------------------------------


def _human(row: Mapping[str, Any]) -> bool | None:
    return {"pass": True, "fail": False}.get(str(row.get("verdict")))


def judge_table(
    states: Mapping[str, Mapping[str, Any]],
    runs: Mapping[str, Mapping[float, list[dict]]],
    temp: float,
) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(sid, creative_key): judge_stats}`` for every creative at ``temp``."""
    from runserver.ratings import creative_index

    out = {}
    for sid, state in states.items():
        temp_runs = runs.get(sid, {}).get(temp, [])
        if not temp_runs:
            continue
        for key, info in creative_index(state).items():
            out[(sid, key)] = judge_stats(creative_evals(temp_runs, info))
    return out


def qa_table(
    qa_runs: Mapping[str, Mapping[str, Mapping[float, list[dict]]]], temp: float
) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(sid, "visual:<name>"): qa_stats}`` at ``temp``."""
    out = {}
    for sid, by_c in qa_runs.items():
        for name, by_t in by_c.items():
            if by_t.get(temp):
                out[(sid, f"visual:{name}")] = qa_stats(by_t[temp])
    return out


def rated_pairs(
    ratings: Sequence[Mapping[str, Any]],
    table: Mapping[tuple[str, str], Mapping[str, Any]],
    field: str,
    *,
    kind: str | None = None,
) -> list[tuple[bool, bool]]:
    """(grader majority ``field``, human verdict) for every matched rating row."""
    pairs = []
    for row in ratings:
        if kind and row.get("kind") != kind:
            continue
        stats = table.get((str(row.get("session_id")), str(row.get("creative_key"))))
        human = _human(row)
        if stats is None or human is None or stats.get(field) is None:
            continue
        pairs.append((bool(stats[field]), human))
    return pairs


def score_pairs(
    ratings: Sequence[Mapping[str, Any]],
    table: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[tuple[float, float]]:
    pairs = []
    for row in ratings:
        stats = table.get((str(row.get("session_id")), str(row.get("creative_key"))))
        score = row.get("score")
        if stats is None or stats.get("mean_score") is None or score is None:
            continue
        pairs.append((float(stats["mean_score"]), float(score)))
    return pairs


def judge_summary(
    table: Mapping[tuple[str, str], Mapping[str, Any]],
    ratings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    from runserver.calibration import cohen_kappa, spearman

    gate_counts: dict[str, int] = defaultdict(int)
    for stats in table.values():
        for gate in stats["flipped_gates"]:
            gate_counts[gate] += 1
    pairs = rated_pairs(ratings, table, "majority")
    return {
        "n_creatives": len(table),
        "n_flip_eligible": sum(1 for s in table.values() if s["pass_flip"] is not None),
        "pass_flip": rate(s["pass_flip"] for s in table.values()),
        "gate_flip": rate(s["gate_flip"] for s in table.values()),
        "gate_flip_counts": dict(sorted(gate_counts.items())),
        "score_sd": mean_or_none(s["score_sd"] for s in table.values()),
        "kappa": cohen_kappa(pairs),
        "spearman": spearman(score_pairs(ratings, table)),
        "pairs": pairs,
    }


def qa_summary(
    table: Mapping[tuple[str, str], Mapping[str, Any]],
    ratings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    from runserver.calibration import cohen_kappa

    pairs = rated_pairs(ratings, table, "majority_pass", kind="visual")
    overlap: dict[str, list[int]] = {r: [0, 0] for r in REASON_RULES}
    for row in ratings:
        if row.get("kind") != "visual" or _human(row) is not False:
            continue
        stats = table.get((str(row.get("session_id")), str(row.get("creative_key"))))
        if stats is None:
            continue
        for reason in row.get("fail_reasons") or []:
            if reason in REASON_RULES:
                overlap[reason][1] += 1
                if REASON_RULES[reason] & set(stats["majority_rules"]):
                    overlap[reason][0] += 1
    return {
        "n_images": len(table),
        "rule_flip": rate(s["rule_flip"] for s in table.values()),
        "kappa": cohen_kappa(pairs),
        "reason_overlap": overlap,
        "pairs": pairs,
    }


# --- bootstrap -----------------------------------------------------------------


def _bootstrap_diff(cand: list[float], base: list[float]):
    from experiments.temperature_ab.analyze import bootstrap_diff

    # bootstrap_diff returns mean(first) - mean(second).
    return bootstrap_diff(cand, base, n_resamples=N_RESAMPLES, seed=SEED)


def _indicators(table: Mapping, field: str) -> list[float]:
    return [float(s[field]) for s in table.values() if s.get(field) is not None]


def kappa_diff_ci(
    cand_pairs: Sequence[tuple[bool, bool]],
    base_pairs: Sequence[tuple[bool, bool]],
    *,
    n_resamples: int = 2000,
    seed: int = SEED,
) -> tuple[float, float] | None:
    """95% percentile-bootstrap CI of kappa(cand) − kappa(base), each side resampled."""
    from runserver.calibration import cohen_kappa

    if not cand_pairs or not base_pairs:
        return None
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_resamples):
        kc = cohen_kappa(rng.choices(list(cand_pairs), k=len(cand_pairs)))["kappa"]
        kb = cohen_kappa(rng.choices(list(base_pairs), k=len(base_pairs)))["kappa"]
        if kc is not None and kb is not None:
            diffs.append(kc - kb)
    if len(diffs) < n_resamples // 2:
        return None
    diffs.sort()
    return diffs[int(0.025 * (len(diffs) - 1))], diffs[
        int(round(0.975 * (len(diffs) - 1)))
    ]


# --- the rule ------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def check_not_higher(
    name: str, cand: float | None, base: float | None, margin: float
) -> CheckResult:
    """PASS iff ``cand <= base + margin`` (N/A when either side is missing)."""
    detail = f"candidate {_fmt(cand)} vs baseline {_fmt(base)} + {margin}"
    if cand is None or base is None:
        return CheckResult(name, "N/A", detail)
    return CheckResult(name, "PASS" if cand <= base + margin + EPS else "FAIL", detail)


def check_not_lower(
    name: str, cand: float | None, base: float | None, margin: float
) -> CheckResult:
    """PASS iff ``cand >= base - margin`` (N/A when either side is missing)."""
    detail = f"candidate {_fmt(cand)} vs baseline {_fmt(base)} - {margin}"
    if cand is None or base is None:
        return CheckResult(name, "N/A", detail)
    return CheckResult(name, "PASS" if cand >= base - margin - EPS else "FAIL", detail)


def judge_rule(cand: Mapping[str, Any], base: Mapping[str, Any]) -> list[CheckResult]:
    return [
        check_not_higher(
            "judge_pass_flip", cand.get("pass_flip"), base.get("pass_flip"), MARGIN_FLIP
        ),
        check_not_higher(
            "judge_gate_flip", cand.get("gate_flip"), base.get("gate_flip"), MARGIN_FLIP
        ),
        check_not_lower(
            "judge_kappa",
            (cand.get("kappa") or {}).get("kappa"),
            (base.get("kappa") or {}).get("kappa"),
            MARGIN_KAPPA,
        ),
    ]


def qa_rule(cand: Mapping[str, Any], base: Mapping[str, Any]) -> list[CheckResult]:
    return [
        check_not_higher(
            "qa_rule_flip", cand.get("rule_flip"), base.get("rule_flip"), MARGIN_FLIP
        ),
        check_not_lower(
            "qa_kappa",
            (cand.get("kappa") or {}).get("kappa"),
            (base.get("kappa") or {}).get("kappa"),
            MARGIN_KAPPA,
        ),
    ]


def verdict(checks: Sequence[CheckResult]) -> str:
    """INCONCLUSIVE if any check is N/A, else ADOPT if all pass, else KEEP."""
    if not checks or any(c.status == "N/A" for c in checks):
        return "INCONCLUSIVE"
    return "ADOPT" if all(c.status == "PASS" for c in checks) else "KEEP"


def decision(grader: str, v: str, base: float, cand: float) -> str:
    if v == "ADOPT":
        return f"{grader}: ADOPT temperature {temp_label(cand)}"
    reason = "inconclusive, " if v == "INCONCLUSIVE" else ""
    return f"{grader}: {v} — {reason}keep current temperature {temp_label(base)}"


# --- outputs -------------------------------------------------------------------


def _ci(result) -> str:
    if result is None:
        return "n/a"
    if isinstance(result, tuple):
        return f"[{result[0]:+.3f}, {result[1]:+.3f}]"
    return f"{result.diff:+.3f} [{result.lo:+.3f}, {result.hi:+.3f}]"


def _kappa_text(k: Mapping[str, Any]) -> str:
    if k.get("kappa") is None:
        return f"n/a ({k.get('reason')}, n={k.get('n')})"
    return f"{k['kappa']:.3f} (agreement {k['agreement']:.0%}, n={k['n']})"


def _rho_text(s: Mapping[str, Any]) -> str:
    if s.get("rho") is None:
        return f"n/a ({s.get('reason')}, n={s.get('n')})"
    return f"{s['rho']:.3f} (n={s['n']})"


def build_records(
    judge_tables: Mapping[float, Mapping], qa_tables: Mapping[float, Mapping]
) -> list[dict[str, Any]]:
    rows = []
    for grader, tables in (("judge", judge_tables), ("qa", qa_tables)):
        for t, table in tables.items():
            for (sid, key), stats in sorted(table.items()):
                row = {
                    "grader": grader,
                    "temperature": t,
                    "session_id": sid,
                    "creative_key": key,
                }
                for k, v in stats.items():
                    row[k] = ";".join(v) if isinstance(v, list) else v
                rows.append(row)
    return rows


def write_csv(rows: Sequence[Mapping[str, Any]], path: Path) -> None:
    fields: list[str] = []
    for row in rows:
        fields += [k for k in row if k not in fields]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def render_summary(
    jt: tuple[float, float],
    qt: tuple[float, float],
    judge: Mapping[float, Mapping[str, Any]],
    qa: Mapping[float, Mapping[str, Any]],
    cis: Mapping[str, Any],
    judge_checks: Sequence[CheckResult],
    qa_checks: Sequence[CheckResult],
) -> str:
    jb, jc = jt
    qb, qc = qt
    lines = ["# Grader temperature harness — summary", "", "## Judge", ""]
    lines += [
        f"| metric | t={temp_label(jb)} | t={temp_label(jc)} | diff (cand − base) 95% CI |",
        "|---|---|---|---|",
        f"| creatives (flip-eligible) | {judge[jb]['n_creatives']} ({judge[jb]['n_flip_eligible']}) "
        f"| {judge[jc]['n_creatives']} ({judge[jc]['n_flip_eligible']}) | |",
        f"| pass flip rate | {_fmt(judge[jb]['pass_flip'])} | {_fmt(judge[jc]['pass_flip'])} | {_ci(cis['pass_flip'])} |",
        f"| gate flip rate | {_fmt(judge[jb]['gate_flip'])} | {_fmt(judge[jc]['gate_flip'])} | {_ci(cis['gate_flip'])} |",
        f"| mean score SD | {_fmt(judge[jb]['score_sd'])} | {_fmt(judge[jc]['score_sd'])} | {_ci(cis['score_sd'])} |",
        f"| kappa vs human | {_kappa_text(judge[jb]['kappa'])} | {_kappa_text(judge[jc]['kappa'])} | {_ci(cis['judge_kappa'])} |",
        f"| Spearman vs human 1-5 | {_rho_text(judge[jb]['spearman'])} | {_rho_text(judge[jc]['spearman'])} | |",
        "",
        "Per-gate flip counts: "
        + "; ".join(
            f"t={temp_label(t)}: {judge[t]['gate_flip_counts'] or '{}'}"
            for t in (jb, jc)
        ),
        "",
        "## Image QA",
        "",
        f"| metric | t={temp_label(qb)} | t={temp_label(qc)} | diff (cand − base) 95% CI |",
        "|---|---|---|---|",
        f"| images | {qa[qb]['n_images']} | {qa[qc]['n_images']} | |",
        f"| rule flip rate | {_fmt(qa[qb]['rule_flip'])} | {_fmt(qa[qc]['rule_flip'])} | {_ci(cis['rule_flip'])} |",
        f"| QA-pass kappa vs human visual | {_kappa_text(qa[qb]['kappa'])} | {_kappa_text(qa[qc]['kappa'])} | {_ci(cis['qa_kappa'])} |",
        "",
        "Human fail reason ↔ majority QA rule overlap (context): "
        + "; ".join(
            f"t={temp_label(t)}: "
            + ", ".join(f"{r} {a}/{b}" for r, (a, b) in qa[t]["reason_overlap"].items())
            for t in (qb, qc)
        ),
        "",
        "## Pre-registered rule",
        "",
        "| check | status | detail |",
        "|---|---|---|",
    ]
    lines += [
        f"| {c.name} | {c.status} | {c.detail} |" for c in [*judge_checks, *qa_checks]
    ]
    lines += [
        "",
        f"- {decision('Judge', verdict(judge_checks), jb, jc)}",
        f"- {decision('Image QA', verdict(qa_checks), qb, qc)}",
        "",
    ]
    return "\n".join(lines)


def analyze(
    selection: Mapping[str, Any],
    states: Mapping[str, Mapping[str, Any]],
    judge_runs: Mapping[str, Mapping[float, list[dict]]],
    qa_runs: Mapping[str, Mapping[str, Mapping[float, list[dict]]]],
    judge_temps: tuple[float, float],
    qa_temps: tuple[float, float],
) -> dict[str, Any]:
    """Everything the summary needs (pure)."""
    ratings = selection.get("ratings") or []
    jtables = {t: judge_table(states, judge_runs, t) for t in judge_temps}
    qtables = {t: qa_table(qa_runs, t) for t in qa_temps}
    judge = {t: judge_summary(jtables[t], ratings) for t in judge_temps}
    qa = {t: qa_summary(qtables[t], ratings) for t in qa_temps}
    jb, jc = judge_temps
    qb, qc = qa_temps

    def diff(cand: Mapping, base: Mapping, field: str) -> Any:
        return _bootstrap_diff(_indicators(cand, field), _indicators(base, field))

    cis = {
        "pass_flip": diff(jtables[jc], jtables[jb], "pass_flip"),
        "gate_flip": diff(jtables[jc], jtables[jb], "gate_flip"),
        "score_sd": diff(jtables[jc], jtables[jb], "score_sd"),
        "judge_kappa": kappa_diff_ci(judge[jc]["pairs"], judge[jb]["pairs"]),
        "rule_flip": diff(qtables[qc], qtables[qb], "rule_flip"),
        "qa_kappa": kappa_diff_ci(qa[qc]["pairs"], qa[qb]["pairs"]),
    }
    judge_checks = judge_rule(judge[jc], judge[jb])
    qa_checks = qa_rule(qa[qc], qa[qb])
    return {
        "judge": judge,
        "qa": qa,
        "cis": cis,
        "judge_checks": judge_checks,
        "qa_checks": qa_checks,
        "judge_verdict": verdict(judge_checks),
        "qa_verdict": verdict(qa_checks),
        "records": build_records(jtables, qtables),
    }


def _two(text: str) -> tuple[float, float]:
    temps = parse_temps(text)
    if len(temps) != 2:
        raise SystemExit(
            f"need exactly two temperatures (baseline,candidate): {text!r}"
        )
    return temps[0], temps[1]


def main(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--judge-temps", default="0.3,1.0", help="baseline,candidate")
    parser.add_argument("--qa-temps", default="0.0,1.0", help="baseline,candidate")
    parser.add_argument("--selection", type=Path, default=SELECTION_PATH)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--judge-dir", type=Path, default=JUDGE_DIR)
    parser.add_argument("--qa-dir", type=Path, default=QA_DIR)
    parser.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    jt, qt = _two(args.judge_temps), _two(args.qa_temps)
    selection = load_selection(args.selection)
    states = {
        sid: state
        for sid in selection["session_ids"]
        if (state := load_state(sid, args.state_dir)) is not None
    }
    result = analyze(
        selection,
        states,
        load_judge_runs(args.judge_dir),
        load_qa_runs(args.qa_dir),
        jt,
        qt,
    )
    summary = render_summary(
        jt,
        qt,
        result["judge"],
        result["qa"],
        result["cis"],
        result["judge_checks"],
        result["qa_checks"],
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "summary.md").write_text(summary)
    write_csv(result["records"], args.out_dir / "records.csv")
    for c in [*result["judge_checks"], *result["qa_checks"]]:
        print(f"{c.status:4}  {c.name}: {c.detail}")
    print(decision("Judge", result["judge_verdict"], *jt))
    print(decision("Image QA", result["qa_verdict"], *qt))
    print(f"Wrote {args.out_dir / 'summary.md'} and records.csv")
    return result


if __name__ == "__main__":
    main()
