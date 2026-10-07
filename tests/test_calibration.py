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


def _row(kind, verdict, judge_passed, score=None, overall=None, gates=None, sid="s1"):
    return {
        "kind": kind,
        "verdict": verdict,
        "judge_passed": judge_passed,
        "judge_gates_passed": gates,
        "judge_overall": overall,
        "score": score,
        "session_id": sid,
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
    block = cal.calibration_report(rows)["overall"]
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


def test_calibration_script_reads_a_csv_export(tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "eval_calibration.py"
    spec = importlib.util.spec_from_file_location("eval_calibration", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    path = tmp_path / "ratings.csv"
    path.write_text(
        "session_id,user_id,kind,verdict,score,judge_overall,judge_passed,judge_gates_passed\n"
        "s1,a@x.com,visual,pass,5,0.9,true,\n"
        "s1,a@x.com,visual,fail,1,0.3,false,\n"
        "s2,B@x.com,ad_copy,pass,,0.8,true,true\n"
    )
    assert mod.main(["--csv", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Ratings: 3 across 2 runs" in out
    assert mod.main(["--csv", str(path), "--user", "b@x.com", "--json"]) == 0
    report = __import__("json").loads(capsys.readouterr().out)
    assert report["n"] == 1 and report["by_kind"]["ad_copy"]["n"] == 1
