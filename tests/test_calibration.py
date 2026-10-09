"""runserver/calibration.py: Cohen's kappa, Spearman, report slicing (pure)."""

from __future__ import annotations

import math

import pytest

from runserver import calibration as cal


def _pairs(tt: int, tf: int, ft: int, ff: int) -> list[tuple[bool, bool]]:
    return (
        [(True, True)] * tt
        + [(True, False)] * tf
        + [(False, True)] * ft
        + [(False, False)] * ff
    )


def test_kappa_textbook_value():
    # po = 0.7; pj = 0.5, ph = 0.6 -> pe = 0.5 -> kappa = 0.4
    out = cal.cohen_kappa(_pairs(20, 5, 10, 15))
    assert out["n"] == 50
    assert out["agreement"] == pytest.approx(0.7)
    assert out["kappa"] == pytest.approx(0.4)
    assert out["reason"] is None


def test_kappa_perfect_and_inverse():
    assert cal.cohen_kappa(_pairs(5, 0, 0, 5))["kappa"] == pytest.approx(1.0)
    assert cal.cohen_kappa(_pairs(0, 5, 5, 0))["kappa"] == pytest.approx(-1.0)


@pytest.mark.parametrize(
    ("pairs", "reason"),
    [
        ([], "no_pairs"),
        (_pairs(10, 0, 0, 0), "single_class"),  # everyone passes everything
        (_pairs(6, 4, 0, 0), "judge_single_class"),  # judge always passes
        (_pairs(6, 0, 4, 0), "human_single_class"),
    ],
)
def test_kappa_degenerate_cases_have_reasons(pairs, reason):
    out = cal.cohen_kappa(pairs)
    assert out["kappa"] is None and out["reason"] == reason
    if pairs:
        assert out["agreement"] is not None


def test_spearman_values():
    assert cal.spearman([(i, i * 2) for i in range(5)])["rho"] == pytest.approx(1.0)
    assert cal.spearman([(i, -i) for i in range(6)])["rho"] == pytest.approx(-1.0)
    # ties get average ranks: y ranks [1, 2, 3.5, 5, 3.5] -> 8 / sqrt(95)
    out = cal.spearman(list(zip([1, 2, 3, 4, 5], [5, 6, 7, 8, 7], strict=True)))
    assert out["rho"] == pytest.approx(8 / math.sqrt(95))


def test_spearman_degenerate():
    assert cal.spearman([(0.1, 1)] * 4) == {
        "n": 4,
        "rho": None,
        "reason": "too_few_pairs",
    }
    assert cal.spearman([(0.5, i) for i in range(5)])["reason"] == "constant_values"


def _row(
    kind,
    verdict,
    judge_passed,
    score=None,
    overall=None,
    gates=None,
    sid="s1",
    version=cal.JUDGE_VERSION,
    learned=False,
):
    return {
        "kind": kind,
        "verdict": verdict,
        "judge_passed": judge_passed,
        "judge_gates_passed": gates,
        "judge_overall": overall,
        "score": score,
        "session_id": sid,
        "judge_version": version,
        "learning_used": learned,
    }


def test_report_slices_by_kind_and_skips_unpaired():
    rows = [
        _row("visual", "pass", True, 5, 0.9, sid="a"),
        _row("visual", "fail", False, 1, 0.4, sid="b"),
        _row("visual", "pass", False, 4, 0.6, sid="b"),
        _row("visual", "fail", True, 2, 0.75, sid="c"),
        _row("visual", "pass", None, 3, None, sid="c"),  # no judge data
        _row("ad_copy", "pass", True, gates=True),
        _row("ad_copy", "fail", True, gates=False),
    ]
    rep = cal.calibration_report(rows)
    assert rep["n"] == 7 and rep["sessions"] == 4
    assert rep["ready_min_ratings"] == cal.READY_MIN_RATINGS
    vis = rep["by_kind"]["visual"]
    assert vis["n"] == 5
    assert vis["judge_passed"]["n"] == 4
    assert vis["judge_passed"]["kappa"] == pytest.approx(0.0)
    assert vis["judge_gates_passed"]["reason"] == "no_pairs"
    assert vis["score_spearman"]["reason"] == "too_few_pairs"  # 4 paired scores
    ad = rep["by_kind"]["ad_copy"]
    assert ad["judge_passed"]["reason"] == "judge_single_class"
    assert ad["judge_gates_passed"]["kappa"] == pytest.approx(1.0)
    assert rep["overall"]["judge_passed"]["n"] == 6


def test_report_accepts_csv_strings():
    rows = [
        {"kind": "visual", "verdict": "pass", "judge_passed": "true", "score": "5",
         "judge_overall": "0.9", "judge_gates_passed": ""},
        {"kind": "visual", "verdict": "fail", "judge_passed": "false", "score": "",
         "judge_overall": "0.3", "judge_gates_passed": "FALSE"},
        {"kind": "visual", "verdict": "maybe", "judge_passed": "true"},
    ]  # fmt: skip
    block = cal.calibration_report(rows, judge_version=None)["overall"]
    assert block["n"] == 2  # the bad verdict is ignored
    assert block["judge_passed"]["kappa"] == pytest.approx(1.0)
    assert block["judge_gates_passed"]["n"] == 1


def test_format_report_mentions_every_slice():
    text = cal.format_report(cal.calibration_report([_row("visual", "pass", True)]))
    assert "Ratings: 1 across 1 runs" in text
    for name in ("[all]", "[visual]", "[ad_copy]"):
        assert name in text
    assert "both judge and humans gave a single verdict" in text
    assert "fewer than 5 paired scores" in text


def test_current_judge_version_comes_from_creative_eval():
    from creative_eval import JUDGE_VERSION

    assert cal.JUDGE_VERSION == JUDGE_VERSION


def test_report_keeps_only_the_current_judge_version_by_default():
    rows = [
        _row("visual", "pass", True, sid="a"),
        _row("visual", "fail", False, sid="a"),
        _row("visual", "fail", True, sid="b", version="2026-10-01"),  # older judge
        _row("ad_copy", "pass", True, sid="c", version=""),  # pre-versioning
        _row("ad_copy", "pass", True, sid="c", version=None),  # NULL column
        _row("visual", "pass", None, sid="d", version=""),  # unpaired: not counted
    ]
    rep = cal.calibration_report(rows)
    assert rep["judge_version"] == cal.JUDGE_VERSION
    assert rep["n"] == 2 and rep["sessions"] == 1
    assert rep["overall"]["judge_passed"]["kappa"] == pytest.approx(1.0)
    assert rep["by_kind"]["ad_copy"]["n"] == 0
    assert rep["excluded_other_versions"] == 3
    # an explicit version, or None = every version
    old = cal.calibration_report(rows, judge_version="2026-10-01")
    assert old["n"] == 1 and old["excluded_other_versions"] == 4
    every = cal.calibration_report(rows, judge_version=None)
    assert every["judge_version"] is None
    assert every["n"] == 6 and every["excluded_other_versions"] == 0


def test_report_splits_the_current_version_by_learning():
    rows = [
        _row("visual", "pass", True, learned=True),
        _row("visual", "fail", False, learned=True),
        _row("visual", "fail", True, learned=True),
        _row("visual", "pass", True),
        _row("ad_copy", "fail", True, learned=None),  # unknown -> not learned
        _row("visual", "pass", True, learned=True, version="2026-10-01"),
    ]
    rep = cal.calibration_report(rows)
    learned, not_learned = (
        rep["by_learning"]["learned"],
        rep["by_learning"]["not_learned"],
    )
    assert learned["n"] == 3 and learned["judge_passed"]["n"] == 3
    assert not_learned["n"] == 2 and not_learned["judge_passed"]["n"] == 2
    # same metric block shape as overall
    assert set(learned) == set(rep["overall"]) == set(not_learned)
    # CSV strings parse too
    csv_row = {**_row("visual", "pass", True), "learning_used": "true"}
    assert cal.calibration_report([csv_row])["by_learning"]["learned"]["n"] == 1


def test_format_report_prints_version_and_learning_split():
    rows = [
        _row("visual", "pass", True, learned=True),
        _row("visual", "pass", True, version="old"),
    ]
    text = cal.format_report(cal.calibration_report(rows))
    assert f"Judge version: {cal.JUDGE_VERSION}" in text
    assert "1 paired ratings from other judge versions not counted" in text
    assert "[learned]" in text and "[not learned]" in text
    text = cal.format_report(cal.calibration_report(rows, judge_version=None))
    assert "Judge version: all" in text and "not counted" not in text


def test_calibration_script_reads_a_csv_export(tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "eval_calibration.py"
    spec = importlib.util.spec_from_file_location("eval_calibration", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    path = tmp_path / "ratings.csv"
    v = cal.JUDGE_VERSION
    path.write_text(
        "session_id,user_id,kind,verdict,score,judge_overall,judge_passed,judge_gates_passed,judge_source,judge_version,learning_used\n"
        f"s1,a@x.com,visual,pass,5,0.9,true,,gcs,{v},false\n"
        f"s1,a@x.com,visual,fail,1,0.3,false,,gcs,{v},true\n"
        f"s2,B@x.com,ad_copy,pass,,0.8,true,true,gcs,{v},\n"
        f"s3,a@x.com,visual,fail,1,0.99,true,,state,{v},\n"
        f"s4,a@x.com,visual,pass,,,,,none,{v},\n"
        "s5,a@x.com,visual,fail,1,0.9,true,,gcs,2026-10-01,false\n"
    )
    assert mod.main(["--csv", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Ratings: 3 across 2 runs" in out  # state/none-sourced left out
    assert "excluded 2 ratings" in out
    assert "1 paired ratings from other judge versions not counted" in out
    assert "[learned] 1 ratings" in out and "[not learned] 2 ratings" in out
    assert mod.main(["--csv", str(path), "--include-all"]) == 0
    assert "Ratings: 5 across 4 runs" in capsys.readouterr().out
    assert mod.main(["--csv", str(path), "--judge-version", "all"]) == 0
    out = capsys.readouterr().out
    assert "Ratings: 4 across 3 runs" in out and "Judge version: all" in out
    assert mod.main(["--csv", str(path), "--judge-version", "2026-10-01"]) == 0
    assert "Ratings: 1 across 1 runs" in capsys.readouterr().out
    assert mod.main(["--csv", str(path), "--user", "b@x.com", "--json"]) == 0
    report = __import__("json").loads(capsys.readouterr().out)
    assert report["n"] == 1 and report["by_kind"]["ad_copy"]["n"] == 1
    assert report["excluded_untrusted"] == 0
