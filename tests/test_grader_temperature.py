"""Offline grader temperature harness (``experiments/grader_temperature/``).

Everything runs on fakes: no BigQuery, session service, GCS or model calls.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from creative_eval import agent as ev_agent
from creative_eval.schemas import (
    AdCopyEvaluation,
    CreativeScore,
    GateResult,
    VisualConceptEvaluation,
)
from experiments.grader_temperature import analyze, fetch, reinspect, rejudge
from experiments.grader_temperature import select as sel
from experiments.grader_temperature.common import (
    concept_slug,
    parse_run_filename,
    plan_order,
    run_filename,
    write_json,
)
from tests._fakes import FakeToolContext

# --- fixtures ------------------------------------------------------------------

PERSON_URI = "gs://bucket/person-refs/me-abc/photo.jpg"


def _state() -> dict[str, Any]:
    return {
        "brand": "Acme",
        "target_product": "Widget",
        "target_audience": "Makers",
        "key_selling_points": "Fast",
        "target_search_trends": "Marathon",
        # Stored as JSON, like some sessions do.
        "ad_copy_critique": json.dumps(
            {
                "ad_copies": [
                    {"original_id": 1, "headline": "Run fast", "tone_style": "Bold"},
                    {"original_id": 2, "headline": "Go far", "tone_style": "Calm"},
                ]
            }
        ),
        "final_visual_concepts": {
            "visual_concepts": [
                {"ad_copy_id": 1, "concept_name": "Finish Line", "trend_motif": "tape"},
                {
                    "ad_copy_id": 2,
                    "concept_name": "Cast Hero",
                    "trend_motif": "medal",
                    "casts_person_reference": True,
                },
                {"ad_copy_id": 2, "concept_name": "Unrendered"},
            ]
        },
        "creative_brief": {"proposition": "Fast widgets"},
        "generated_images": {
            "Finish Line": {"gcs_uri": "gs://bucket/out/finish.png"},
            "Cast Hero": {"gcs_uri": "gs://bucket/out/hero.jpg", "cast": True},
        },
        "person_reference": {"uri": PERSON_URI, "consent_id": "c-1"},
        "trend_situation": {"phase": "evergreen", "timely_ok": False},
        "sources": {"src-1": {"url": "https://example.com"}},
        "safer_angle_active": "yes",
        "safer_angle_sensitive_reason": "tragedy",
        "rating_strictness": ["weak_cta", "bogus", "product_not_visible"],
        "reference_images": [{"uri": "gs://bucket/logo.png", "role": "logo"}],
    }


def _score(passed: bool, overall: float = 0.8, *, failed=False, gates=()) -> dict:
    return {
        "overall_score": overall,
        "passed": passed,
        "improvements": ["evaluation_failed"] if failed else [],
        "gates": [
            {"gate": g, "passed": p, "advisory": a, "note": ""} for g, p, a in gates
        ],
    }


# --- selection -------------------------------------------------------------------


def _rows(sid: str, day: int, fails: int, passes: int = 2, kinds=("ad_copy", "visual")):
    rows = []
    for i in range(fails + passes):
        rows.append(
            {
                "rating_id": f"{sid}-{i}",
                "session_id": sid,
                "app_name": "creative_agent",
                "creative_key": f"copy:{i}",
                "kind": kinds[i % len(kinds)],
                "user_id": f"user-{sid}",
                "verdict": "fail" if i < fails else "pass",
                "score": 3,
                "created_at": f"2026-10-{day:02d}T10:00:00",
            }
        )
    return rows


def _pool() -> list[dict]:
    rows = []
    # 10 recent sessions with no fails, 4 older ones with 4 fails each.
    for d in range(10):
        rows += _rows(f"recent{d}", 20 + d, 0)
    for d in range(4):
        rows += _rows(f"old{d}", 1 + d, 4)
    rows += _rows("onekind", 30, 6, kinds=("visual",))  # not eligible
    return rows


def test_selection_stratifies_for_min_fails():
    chosen = sel.choose_sessions(_pool(), n=10, seed=1, min_fails=12)
    ids = chosen["session_ids"]
    assert len(ids) == 10
    assert "onekind" not in ids
    # Three old fail-bearing sessions reach 12 fails; the rest are the most recent.
    assert {"old3", "old2", "old1"} <= set(ids) and "old0" not in ids
    assert chosen["total_fails"] == 12 and chosen["min_fails_met"]
    assert [s for s in ids if s.startswith("recent")] == [
        f"recent{d}" for d in range(9, 2, -1)
    ]
    assert {r["session_id"] for r in chosen["ratings"]} == set(ids)
    assert chosen["sessions"][0]["user_id"] == "user-recent9"


def test_selection_is_deterministic_and_seeded_on_ties():
    rows = []
    for d in range(8):
        rows += _rows(f"s{d}", 5, 2)  # all tied on recency
    a = sel.choose_sessions(rows, n=3, seed=7, min_fails=0)
    assert a == sel.choose_sessions(rows, n=3, seed=7, min_fails=0)
    picks = {
        tuple(sel.choose_sessions(rows, n=3, seed=s, min_fails=0)["session_ids"])
        for s in range(10)
    }
    assert len(picks) > 1


def test_selection_exclude_and_unmet_fails():
    chosen = sel.choose_sessions(_pool(), n=10, seed=1, min_fails=40, exclude=["old0"])
    assert "old0" not in chosen["session_ids"]
    assert chosen["total_fails"] == 12 and not chosen["min_fails_met"]
    assert "NOT MET" in sel.describe(chosen)


def test_fetch_rows_is_parameterised_and_skips_notes():
    calls = {}

    class Row(dict):
        pass

    class FakeBQ:
        def query_and_wait(self, sql, job_config):
            calls["sql"] = sql
            calls["params"] = {p.name: p.value for p in job_config.query_parameters}
            return [Row(session_id="s", fail_reasons=("text_problem",))]

    rows = sel.fetch_rows(FakeBQ(), "proj.ds.creative_ratings")
    assert rows == [{"session_id": "s", "fail_reasons": ["text_problem"]}]
    assert "@app_name" in calls["sql"] and "note" not in calls["sql"]
    assert calls["params"]["app_name"] == "creative_agent"
    with pytest.raises(ValueError):
        sel.build_query("proj.ds.t`; DROP")


def test_select_main_writes_selection(tmp_path, capsys):
    class FakeBQ:
        def query_and_wait(self, sql, job_config):
            return [dict(r) for r in _pool()]

    out = tmp_path / "selection.json"
    sel.main(["--n", "4", "--min-fails", "4", "--out", str(out)], client=FakeBQ())
    data = json.loads(out.read_text())
    assert len(data["session_ids"]) == 4
    assert "Selected 4" in capsys.readouterr().out


# --- fetch ------------------------------------------------------------------------


def test_parse_session_uri_forms():
    assert fetch.parse_session_uri("agentengine://123", project="p") == {
        "project": "p",
        "location": "us-central1",
        "agent_engine_id": "123",
    }
    full = "agentengine://projects/n1/locations/us-east1/reasoningEngines/9"
    assert fetch.parse_session_uri(full) == {
        "project": "n1",
        "location": "us-east1",
        "agent_engine_id": "9",
    }
    for bad in ("memory://", "agentengine://", "agentengine://a/b"):
        with pytest.raises(ValueError):
            fetch.parse_session_uri(bad, project="p")


def test_fetch_caches_and_reports_missing(tmp_path):
    class FakeSessions:
        calls: list = []

        async def get_session(self, *, app_name, user_id, session_id):
            self.calls.append((app_name, user_id, session_id))
            if session_id == "gone":
                return None
            return SimpleNamespace(state={"brand": session_id})

    selection = {
        "sessions": [
            {"session_id": "a", "user_id": "u1"},
            {"session_id": "gone", "user_id": "u2"},
        ]
    }
    svc = FakeSessions()
    import asyncio

    report = asyncio.run(fetch.fetch_all(selection, svc, tmp_path))
    assert report == {"fetched": ["a"], "cached": [], "missing": ["gone"]}
    assert json.loads((tmp_path / "a.json").read_text()) == {"brand": "a"}
    assert svc.calls[0] == ("creative_agent", "u1", "a")
    again = asyncio.run(fetch.fetch_all(selection, svc, tmp_path))
    assert again["cached"] == ["a"] and len(svc.calls) == 3


# --- judge inputs ------------------------------------------------------------------


def _ad_eval(ad: dict, *, failed=False) -> AdCopyEvaluation:
    return AdCopyEvaluation(
        original_id=ad["original_id"],
        headline=ad["headline"],
        tone_style=ad.get("tone_style", ""),
        score=CreativeScore.model_validate(
            {
                **_score(not failed, 0.0 if failed else 0.8, failed=failed),
                "verdicts": [],
                "strengths": [],
            }
        ),
    )


def _vis_eval(vc: dict, *, failed=False) -> VisualConceptEvaluation:
    return VisualConceptEvaluation(
        ad_copy_id=vc["ad_copy_id"],
        concept_name=vc["concept_name"],
        score=CreativeScore(
            overall_score=0.8,
            passed=not failed,
            verdicts=[],
            strengths=[],
            improvements=["evaluation_failed"] if failed else [],
            gates=[GateResult(gate="product_visible", passed=True)],
        ),
    )


def test_judge_inputs_match_the_agents_full_evaluation(monkeypatch):
    captured = {}

    def fake(ad_copies, visual_concepts, campaign_context, config, **kwargs):
        captured.update(
            ad_copies=ad_copies,
            visual_concepts=visual_concepts,
            campaign_context=campaign_context,
            **kwargs,
        )
        return [_ad_eval(a) for a in ad_copies], [_vis_eval(v) for v in visual_concepts]

    monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", fake)
    state = _state()
    ev_agent.evaluate_all_creatives(FakeToolContext(state))
    ours = rejudge.judge_inputs(state)
    assert ours == captured
    assert ours["safer_angle"] is True and ours["sensitive_reason"]
    assert len(ours["ad_copies"]) == 2 and ours["paired_ad_copies"] == ours["ad_copies"]


# --- rejudge -----------------------------------------------------------------------


def test_plan_order_interleaves_counterbalanced():
    order = plan_order(["a", "b"], [0.3, 1.0], 2)
    assert order == [
        ("a", 0.3, 0),
        ("a", 1.0, 0),
        ("a", 1.0, 1),
        ("a", 0.3, 1),
        ("b", 1.0, 0),
        ("b", 0.3, 0),
        ("b", 0.3, 1),
        ("b", 1.0, 1),
    ]


def test_run_filenames_round_trip():
    assert run_filename(1, 2) == "1.0-r2.json"
    assert parse_run_filename("0.3-r0.json") == (0.3, 0)
    assert parse_run_filename("notes.json") is None
    assert concept_slug("A/B") != concept_slug("A B")


class FakeEvaluate:
    def __init__(self, fail_temp=None, raise_on=None):
        self.temps: list[float] = []
        self.clients: list[Any] = []
        self.fail_temp = fail_temp
        self.raise_on = raise_on

    def __call__(
        self, ad_copies, visual_concepts, campaign_context, config, *, client, **kw
    ):
        t = config.judge_temperature
        self.temps.append(t)
        self.clients.append(client)
        if t == self.raise_on:
            raise RuntimeError("quota")
        failed = t == self.fail_temp
        return (
            [_ad_eval(a, failed=failed and a["original_id"] == 2) for a in ad_copies],
            [_vis_eval(v) for v in visual_concepts],
        )


def _client_factory(opened: list):
    @contextlib.contextmanager
    def factory(config):
        client = object()
        opened.append(client)
        yield client

    return factory


def _config(t):
    return SimpleNamespace(judge_temperature=t, eval_model="judge-x")


def test_rejudge_writes_interleaved_resumable_and_records_failures(tmp_path):
    state_dir, out = tmp_path / "state", tmp_path / "judge"
    write_json(state_dir / "s1.json", _state())
    fake, opened = FakeEvaluate(fail_temp=1.0), []
    report = rejudge.run(
        ["s1", "nostate"],
        [0.3, 1.0],
        2,
        state_dir=state_dir,
        out_dir=out,
        evaluate=fake,
        client_factory=_client_factory(opened),
        config_factory=_config,
    )
    assert fake.temps == [0.3, 1.0, 1.0, 0.3]
    # One client per batch, passed through to the judge.
    assert fake.clients == opened and len(set(map(id, opened))) == 4
    assert len(report["written"]) == 4 and report["no_state"] == ["nostate"]
    rec = json.loads((out / "s1" / "1.0-r1.json").read_text())
    assert (
        rec["session_id"] == "s1" and rec["repeat"] == 1 and rec["temperature"] == 1.0
    )
    assert rec["failed"] == [
        {"kind": "ad_copy", "original_id": 2, "headline": "Go far"}
    ]
    assert len(rec["visual_concept_evaluations"]) == 3
    assert json.loads((out / "s1" / "0.3-r0.json").read_text())["failed"] == []

    again = FakeEvaluate()
    report = rejudge.run(
        ["s1"],
        [0.3, 1.0],
        2,
        state_dir=state_dir,
        out_dir=out,
        evaluate=again,
        client_factory=_client_factory([]),
        config_factory=_config,
    )
    assert again.temps == [] and len(report["skipped"]) == 4


def test_rejudge_batch_error_writes_nothing(tmp_path):
    state_dir, out = tmp_path / "state", tmp_path / "judge"
    write_json(state_dir / "s1.json", _state())
    report = rejudge.run(
        ["s1"],
        [0.3, 1.0],
        1,
        state_dir=state_dir,
        out_dir=out,
        evaluate=FakeEvaluate(raise_on=1.0),
        client_factory=_client_factory([]),
        config_factory=_config,
    )
    assert report["errors"] == [{"session_id": "s1", "temperature": 1.0, "repeat": 0}]
    assert not (out / "s1" / "1.0-r0.json").exists()
    assert (out / "s1" / "0.3-r0.json").exists()


def test_make_eval_config_sets_the_judge_temperature():
    config = rejudge.make_eval_config(1.0)
    assert config.judge_temperature == 1.0
    assert config.max_eval_workers == 2 and config.max_visual_wave_workers == 2


# --- reinspect ----------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name, self.size = store, name, None

    def reload(self):
        if self.name not in self.store.objects:
            raise FileNotFoundError(self.name)
        self.size = len(self.store.objects[self.name])

    def download_as_bytes(self):
        self.store.downloads.append(self.name)
        return self.store.objects[self.name]


class FakeGCS:
    def __init__(self, objects):
        self.objects, self.downloads = objects, []

    def bucket(self, name):
        return SimpleNamespace(blob=lambda obj: FakeBlob(self, f"{name}/{obj}"))


_OBJECTS = {
    "bucket/out/finish.png": b"png-bytes",
    "bucket/out/hero.jpg": b"jpg-bytes",
    "bucket/person-refs/me-abc/photo.jpg": b"person",
}


class FakeInspect:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(
        self,
        image_bytes,
        mime,
        concept,
        *,
        brand,
        target_product,
        client,
        model,
        has_logo_reference=False,
        strictness=(),
        person_image=None,
        original_image=None,
        safer_angle_active=False,
        temperature=0.0,
    ):
        self.calls.append(
            dict(
                image=image_bytes,
                mime=mime,
                concept=concept["concept_name"],
                brand=brand,
                target_product=target_product,
                client=client,
                model=model,
                logo=has_logo_reference,
                strictness=strictness,
                person_image=person_image,
                safer=safer_angle_active,
                t=temperature,
            )
        )
        return SimpleNamespace(model_dump=lambda: {"t": temperature})


def _failed_rules(result, concept, *, target_product):
    return ["gibberish text"] if concept["concept_name"] == "Cast Hero" else []


def _reinspect(tmp_path, objects, inspect_fn):
    gcs = FakeGCS(dict(objects))
    report = reinspect.run(
        ["s1"],
        [0.0, 1.0],
        2,
        state_dir=tmp_path / "state",
        img_dir=tmp_path / "img",
        out_dir=tmp_path / "qa",
        download=reinspect.Downloader(gcs),
        inspect_fn=inspect_fn,
        failed_rules=_failed_rules,
        qa_client="qa-client",
        model="qa-model",
    )
    return report, gcs


def test_reinspect_builds_production_inputs_and_resumes(tmp_path):
    write_json(tmp_path / "state" / "s1.json", _state())
    fake = FakeInspect()
    report, gcs = _reinspect(tmp_path, _OBJECTS, fake)
    assert len(fake.calls) == 8 and len(report["written"]) == 8
    by = {(c["concept"], c["t"]) for c in fake.calls}
    assert by == {(n, t) for n in ("Finish Line", "Cast Hero") for t in (0.0, 1.0)}
    first = fake.calls[0]
    assert first["brand"] == "Acme" and first["target_product"] == "Widget"
    assert first["strictness"] == ("product_not_visible", "weak_cta")
    assert first["safer"] is True and first["logo"] is True
    assert first["client"] == "qa-client" and first["model"] == "qa-model"
    for c in fake.calls:
        if c["concept"] == "Cast Hero":
            assert c["person_image"] == (b"person", "image/jpeg")
            assert c["image"] == b"jpg-bytes" and c["mime"] == "image/jpeg"
        else:
            assert c["person_image"] is None and c["mime"] == "image/png"
    # Interleaved per repeat: 0.0,1.0 then 1.0,0.0.
    assert [c["t"] for c in fake.calls if c["concept"] == "Finish Line"] == [
        0.0,
        1.0,
        1.0,
        0.0,
    ]
    # Each image downloaded once (plus the person photo once).
    assert sorted(gcs.downloads) == sorted(_OBJECTS)
    rec = json.loads(
        (tmp_path / "qa" / "s1" / concept_slug("Cast Hero") / "1.0-r0.json").read_text()
    )
    assert rec["failed_rules"] == ["gibberish text"] and rec["cast"] is True
    assert PERSON_URI not in json.dumps(rec)

    again = FakeInspect()
    report, gcs = _reinspect(tmp_path, _OBJECTS, again)
    assert again.calls == [] and len(report["skipped"]) == 8
    assert gcs.downloads == ["bucket/person-refs/me-abc/photo.jpg"]


def test_reinspect_skips_cast_concept_without_person_photo(tmp_path):
    write_json(tmp_path / "state" / "s1.json", _state())
    objects = {k: v for k, v in _OBJECTS.items() if "person-refs" not in k}
    fake = FakeInspect()
    report, _ = _reinspect(tmp_path, objects, fake)
    assert {c["concept"] for c in fake.calls} == {"Finish Line"}
    assert report["notes"] == [
        {
            "session_id": "s1",
            "concept_name": "Cast Hero",
            "note": "person photo unreadable; skipped",
        }
    ]


def test_reinspect_caps_image_size():
    gcs = FakeGCS({"b/big.png": b"x" * 11})
    with pytest.raises(ValueError):
        reinspect.Downloader(gcs)("gs://b/big.png", max_bytes=10)
    assert gcs.downloads == []


# --- analyze: maths ------------------------------------------------------------------


def test_flip_and_majority_definitions():
    assert analyze.flipped([True, True, True]) is False
    assert analyze.flipped([True, False, True]) is True
    assert analyze.flipped([True]) is None
    assert analyze.flipped([["a", "b"], ["b", "a"]]) is False
    assert analyze.flipped([["a"], []]) is True
    assert analyze.majority([True, True, False]) is True
    assert analyze.majority([False, False, True]) is False
    assert analyze.majority([True, False]) is None
    assert analyze.majority([]) is None


def test_flipped_gates_ignores_advisory_and_counts_missing():
    evals = [
        {
            "score": _score(
                True,
                gates=[("a", True, False), ("cue", True, True), ("b", True, False)],
            )
        },
        {"score": _score(True, gates=[("a", True, False), ("cue", False, True)])},
        {
            "score": _score(
                True,
                gates=[("a", True, False), ("cue", True, True), ("b", True, False)],
            )
        },
    ]
    assert analyze.flipped_gates(evals) == ["b"]
    stats = analyze.judge_stats(evals)
    assert stats["gate_flip"] is True and stats["pass_flip"] is False
    assert analyze.flipped_gates(evals[:1]) is None


def test_judge_stats_scores():
    evals = [
        {"score": _score(p, s)} for p, s in ((True, 0.8), (False, 0.6), (True, 0.7))
    ]
    stats = analyze.judge_stats(evals)
    assert stats["pass_flip"] is True and stats["majority"] is True
    assert stats["mean_score"] == pytest.approx(0.7)
    assert stats["score_sd"] == pytest.approx(0.1)


def test_qa_stats():
    recs = [{"failed_rules": r} for r in (["x", "y"], ["y", "x"], ["y"])]
    stats = analyze.qa_stats(recs)
    assert stats["rule_flip"] is True and stats["majority_pass"] is False
    assert stats["majority_rules"] == ["x", "y"]
    stable = analyze.qa_stats([{"failed_rules": []}] * 3)
    assert stable["rule_flip"] is False and stable["majority_pass"] is True


def test_rating_creative_matching_by_headline_then_id_and_failed_excluded():
    from runserver.ratings import creative_index

    index = creative_index(_state())
    assert set(index) == {
        "copy:1",
        "copy:2",
        "visual:Finish Line",
        "visual:Cast Hero",
        "visual:Unrendered",
    }
    # The judge echoed original_id 1 for both copies: headline decides.
    run = {
        "ad_copy_evaluations": [
            {"original_id": 1, "headline": "Run fast", "score": _score(True)},
            {"original_id": 1, "headline": "Go far", "score": _score(False)},
        ],
        "visual_concept_evaluations": [
            {"concept_name": "Finish Line", "score": _score(True)},
            {"concept_name": "Cast Hero", "score": _score(False, 0.0, failed=True)},
        ],
    }
    assert analyze.creative_evals([run], index["copy:2"])[0]["score"]["passed"] is False
    assert len(analyze.creative_evals([run], index["visual:Finish Line"])) == 1
    assert analyze.creative_evals([run], index["visual:Cast Hero"]) == []


# --- analyze: rule -------------------------------------------------------------------


def _judge(pass_flip, gate_flip, kappa):
    return {"pass_flip": pass_flip, "gate_flip": gate_flip, "kappa": {"kappa": kappa}}


def _statuses(checks):
    return [c.status for c in checks]


def test_judge_rule_boundaries():
    base = _judge(0.10, 0.20, 0.50)
    ok = analyze.judge_rule(_judge(0.15, 0.25, 0.45), base)
    assert _statuses(ok) == ["PASS", "PASS", "PASS"] and analyze.verdict(ok) == "ADOPT"
    worse = analyze.judge_rule(_judge(0.151, 0.251, 0.449), base)
    assert (
        _statuses(worse) == ["FAIL", "FAIL", "FAIL"]
        and analyze.verdict(worse) == "KEEP"
    )
    better = analyze.judge_rule(_judge(0.0, 0.0, 0.9), base)
    assert analyze.verdict(better) == "ADOPT"
    assert _statuses(analyze.judge_rule(_judge(0.16, 0.2, 0.5), base)) == [
        "FAIL",
        "PASS",
        "PASS",
    ]
    assert _statuses(analyze.judge_rule(_judge(0.1, 0.26, 0.5), base)) == [
        "PASS",
        "FAIL",
        "PASS",
    ]
    assert _statuses(analyze.judge_rule(_judge(0.1, 0.2, 0.44), base)) == [
        "PASS",
        "PASS",
        "FAIL",
    ]


def test_judge_rule_uncomputable_is_inconclusive():
    base = _judge(0.10, 0.20, 0.50)
    checks = analyze.judge_rule(_judge(0.0, 0.0, None), base)
    assert _statuses(checks) == ["PASS", "PASS", "N/A"]
    assert analyze.verdict(checks) == "INCONCLUSIVE"
    # N/A wins over a FAIL: still inconclusive, still keep current.
    checks = analyze.judge_rule(_judge(0.9, None, 0.5), base)
    assert analyze.verdict(checks) == "INCONCLUSIVE"
    assert "keep current temperature 0.3" in analyze.decision(
        "Judge", "INCONCLUSIVE", 0.3, 1.0
    )
    assert analyze.verdict([]) == "INCONCLUSIVE"


def test_qa_rule_boundaries():
    base = {"rule_flip": 0.20, "kappa": {"kappa": 0.30}}
    ok = analyze.qa_rule({"rule_flip": 0.25, "kappa": {"kappa": 0.25}}, base)
    assert analyze.verdict(ok) == "ADOPT"
    flip = analyze.qa_rule({"rule_flip": 0.26, "kappa": {"kappa": 0.30}}, base)
    assert _statuses(flip) == ["FAIL", "PASS"] and analyze.verdict(flip) == "KEEP"
    kap = analyze.qa_rule({"rule_flip": 0.2, "kappa": {"kappa": 0.24}}, base)
    assert _statuses(kap) == ["PASS", "FAIL"]
    na = analyze.qa_rule({"rule_flip": 0.2, "kappa": {"kappa": None}}, base)
    assert analyze.verdict(na) == "INCONCLUSIVE"


# --- analyze: end to end -------------------------------------------------------------


def _write_judge(out: Path, t: float, k: int, passes: dict[str, bool]) -> None:
    write_json(
        out / "s1" / run_filename(t, k),
        {
            "session_id": "s1",
            "temperature": t,
            "repeat": k,
            "ad_copy_evaluations": [
                {
                    "original_id": 1,
                    "headline": "Run fast",
                    "score": _score(passes["copy:1"], 0.8),
                },
                {
                    "original_id": 2,
                    "headline": "Go far",
                    "score": _score(passes["copy:2"], 0.4),
                },
            ],
            "visual_concept_evaluations": [
                {
                    "concept_name": "Finish Line",
                    "score": _score(passes["visual:Finish Line"], 0.9),
                },
                {
                    "concept_name": "Cast Hero",
                    "score": _score(passes["visual:Cast Hero"], 0.3),
                },
            ],
            "failed": [],
        },
    )


def test_analyze_main_end_to_end(tmp_path, capsys):
    data = tmp_path / "data"
    write_json(data / "state" / "s1.json", _state())
    ratings = [
        {
            "session_id": "s1",
            "creative_key": "copy:1",
            "kind": "ad_copy",
            "verdict": "pass",
            "score": 5,
        },
        {
            "session_id": "s1",
            "creative_key": "copy:2",
            "kind": "ad_copy",
            "verdict": "fail",
            "score": 2,
        },
        {
            "session_id": "s1",
            "creative_key": "visual:Finish Line",
            "kind": "visual",
            "verdict": "pass",
            "score": 4,
        },
        {
            "session_id": "s1",
            "creative_key": "visual:Cast Hero",
            "kind": "visual",
            "verdict": "fail",
            "score": 1,
            "fail_reasons": ["text_problem", "unwanted_logo"],
        },
        # A second rater on copy:2 (each rating row is its own pair).
        {
            "session_id": "s1",
            "creative_key": "copy:2",
            "kind": "ad_copy",
            "verdict": "fail",
            "score": 3,
        },
    ]
    write_json(data / "selection.json", {"session_ids": ["s1"], "ratings": ratings})
    truth = {
        "copy:1": True,
        "copy:2": False,
        "visual:Finish Line": True,
        "visual:Cast Hero": False,
    }
    judge = tmp_path / "judge"
    for k in range(3):
        _write_judge(judge, 0.3, k, truth)
        # t=1.0 flips copy:1 in one repeat.
        _write_judge(judge, 1.0, k, {**truth, "copy:1": k != 1})
    qa = tmp_path / "qa"
    for t in (0.0, 1.0):
        for k in range(3):
            for name, rules in (("Finish Line", []), ("Cast Hero", ["gibberish text"])):
                write_json(
                    qa / "s1" / concept_slug(name) / run_filename(t, k),
                    {
                        "session_id": "s1",
                        "concept_name": name,
                        "temperature": t,
                        "repeat": k,
                        "failed_rules": rules,
                    },
                )
    out = tmp_path / "results"
    result = analyze.main(
        [
            "--selection",
            str(data / "selection.json"),
            "--state-dir",
            str(data / "state"),
            "--judge-dir",
            str(judge),
            "--qa-dir",
            str(qa),
            "--out-dir",
            str(out),
        ]
    )
    j = result["judge"]
    assert j[0.3]["pass_flip"] == 0.0 and j[1.0]["pass_flip"] == pytest.approx(0.25)
    assert j[0.3]["kappa"]["kappa"] == pytest.approx(1.0)
    assert j[0.3]["kappa"]["n"] == 5 and j[0.3]["spearman"]["n"] == 5
    assert 0.5 < j[0.3]["spearman"]["rho"] < 1.0
    # pass flip 0.25 > 0.0 + 0.05 → FAIL → KEEP.
    assert _statuses(result["judge_checks"]) == ["FAIL", "PASS", "PASS"]
    assert result["judge_verdict"] == "KEEP"
    q = result["qa"]
    assert q[0.0]["rule_flip"] == 0.0 and q[0.0]["kappa"]["kappa"] == pytest.approx(1.0)
    assert q[1.0]["reason_overlap"]["text_problem"] == [1, 1]
    assert q[1.0]["reason_overlap"]["unwanted_logo"] == [0, 1]
    assert result["qa_verdict"] == "ADOPT"
    summary = (out / "summary.md").read_text()
    assert "Judge: KEEP" in summary and "Image QA: ADOPT temperature 1.0" in summary
    assert (out / "records.csv").read_text().startswith("grader,temperature")
    printed = capsys.readouterr().out
    assert "FAIL  judge_pass_flip" in printed


def test_reinspect_call_matches_the_real_inspect_image_signature(tmp_path):
    """The kwargs reinspect passes bind to production's ``inspect_image``."""
    import inspect

    from creative_agent import image_qa

    write_json(tmp_path / "state" / "s1.json", _state())
    fake = FakeInspect()
    _reinspect(tmp_path, _OBJECTS, fake)
    c = fake.calls[0]
    inspect.signature(image_qa.inspect_image).bind(
        c["image"],
        c["mime"],
        {"concept_name": c["concept"]},
        brand=c["brand"],
        target_product=c["target_product"],
        client=c["client"],
        model=c["model"],
        has_logo_reference=c["logo"],
        strictness=c["strictness"],
        person_image=c["person_image"],
        safer_angle_active=c["safer"],
        temperature=c["t"],
    )
