"""Tests for creative_eval's binary compliance gates (research F11).

Gates are the trustworthy part of the judge's verdict (binary compliance
against concrete rules); the 1-10 dimension scores stay advisory quality
signals. ``passed`` = ``overall_score >= threshold`` AND ``gates_passed``.
"""

import json
from unittest.mock import MagicMock

import pytest

from creative_eval import prompts as eval_prompts
from creative_eval.brief import (
    NO_BRIEF_BLOCK,
    angle_line,
    format_brief_for_judge,
    parse_brief,
)
from creative_eval.config import EvalConfig
from creative_eval.dimensions import (
    AD_COPY_GATES,
    ADVISORY_GATES,
    BRIEF_GATES,
    VISUAL_GATES,
)
from creative_eval.schemas import (
    AdCopyEvaluation,
    CreativeEvaluationReport,
    CreativeScore,
    EvalVerdict,
    GateResult,
    VisualConceptEvaluation,
)
from tests._fakes import FakeToolContext

OLD_REPORT = {
    "brand": "B",
    "target_product": "P",
    "target_search_trend": "t",
    "ad_copy_evaluations": [
        {
            "original_id": 1,
            "headline": "H",
            "tone_style": "T",
            "score": {
                "overall_score": 0.8,
                "passed": True,
                "verdicts": [],
                "strengths": [],
                "improvements": [],
            },
        }
    ],
    "visual_concept_evaluations": [],
    "summary": {
        "total_ad_copies": 1,
        "ad_copies_passed": 1,
        "avg_ad_copy_score": 0.8,
        "total_visual_concepts": 0,
        "visual_concepts_passed": 0,
        "avg_visual_score": 0.0,
        "overall_pass_rate": 1.0,
        "weakest_dimensions": [],
    },
}


def _verdicts(score: int, n: int = 6) -> list[EvalVerdict]:
    return [
        EvalVerdict(
            dimension=f"d{i}",
            score=score,
            verdict="pass" if score >= 7 else "fail",
            rationale="r",
        )
        for i in range(n)
    ]


# --- Task 7.1: schema --------------------------------------------------------


class TestSchema:
    def test_old_report_still_parses_with_defaults(self):
        report = CreativeEvaluationReport.model_validate(OLD_REPORT)
        score = report.ad_copy_evaluations[0].score
        assert score.gates == []
        assert score.gates_passed is True
        assert report.passing_threshold == 0.7
        assert report.brief_used is False
        assert report.summary.gates_pass_rate is None

    def test_gate_result_fields(self):
        gate = GateResult(gate="product_named", passed=False, note="no product")
        assert gate.model_dump() == {
            "gate": "product_named",
            "passed": False,
            "note": "no product",
            "advisory": False,
        }

    def test_visual_eval_image_judged_defaults_false(self):
        ev = VisualConceptEvaluation(
            ad_copy_id=1,
            concept_name="c",
            score=CreativeScore(
                overall_score=0.5,
                passed=False,
                verdicts=[],
                strengths=[],
                improvements=[],
            ),
        )
        assert ev.image_judged is False


class TestPassedRule:
    def test_high_score_with_failed_gate_does_not_pass(self):
        from creative_eval.evaluate import _score_from_verdicts

        gates = [
            GateResult(gate="product_named", passed=True),
            GateResult(gate="mandatories_met", passed=False, note="no CTA"),
        ]
        result = _score_from_verdicts(_verdicts(9), threshold=0.7, gates=gates)
        assert result.overall_score == 0.9
        assert result.gates_passed is False
        assert result.passed is False
        assert result.gates == gates

    def test_failed_advisory_gate_does_not_block(self):
        from creative_eval.evaluate import _score_from_verdicts

        gates = [
            GateResult(gate="product_visible", passed=True),
            GateResult(gate="brand_cue_present", passed=False, advisory=True),
        ]
        result = _score_from_verdicts(_verdicts(8), threshold=0.7, gates=gates)
        assert result.gates_passed is True
        assert result.passed is True

    def test_gates_pass_but_low_score_fails(self):
        from creative_eval.evaluate import _score_from_verdicts

        gates = [GateResult(gate="product_named", passed=True)]
        result = _score_from_verdicts(_verdicts(5), threshold=0.7, gates=gates)
        assert result.gates_passed is True
        assert result.passed is False

    def test_no_gates_keeps_score_only_rule(self):
        from creative_eval.evaluate import _score_from_verdicts

        result = _score_from_verdicts(_verdicts(8), threshold=0.7)
        assert result.gates == [] and result.gates_passed is True
        assert result.passed is True


def _ad_eval(passed: bool, gates_passed: bool) -> AdCopyEvaluation:
    return AdCopyEvaluation(
        original_id=1,
        headline="h",
        tone_style="t",
        score=CreativeScore(
            overall_score=0.8,
            passed=passed,
            verdicts=[],
            strengths=[],
            improvements=[],
            gates=[GateResult(gate="product_named", passed=gates_passed)],
            gates_passed=gates_passed,
        ),
    )


class TestSummaryGatesPassRate:
    def test_gates_pass_rate_counts_creatives_with_all_gates_passed(self):
        from creative_eval.evaluate import _build_summary

        summary = _build_summary(
            [_ad_eval(True, True), _ad_eval(False, False), _ad_eval(True, True)],
            [],
        )
        assert summary.gates_pass_rate == round(2 / 3, 3)

    def test_gates_pass_rate_none_without_creatives(self):
        from creative_eval.evaluate import _build_summary

        assert _build_summary([], []).gates_pass_rate is None

    def test_gates_pass_rate_excludes_failed_judge_calls(self):
        from creative_eval.evaluate import _build_summary, _failed_score

        failed = _ad_eval(False, False)
        failed.score = _failed_score()
        summary = _build_summary([_ad_eval(True, True), failed, failed], [])
        assert summary.gates_pass_rate == 1.0
        assert _build_summary([failed], []).gates_pass_rate is None

    def test_gates_pass_rate_definition_is_documented(self):
        from creative_eval.schemas import EvaluationSummary

        desc = EvaluationSummary.model_fields["gates_pass_rate"].description or ""
        assert "judged" in desc and "evaluation_failed" in desc


# --- Task 7.2: brief block, gates, prompts -------------------------------------

BRIEF = {
    "single_minded_proposition": "Rocket Skates make every chase {winnable}.",
    "reasons_to_believe": [
        {"claim": "Top speed 90 mph", "source_id": "src-1"},
        {"claim": "Lab-tested brakes", "source_id": "brief"},
    ],
    "brand": {
        "tone_of_voice": "wry, deadpan",
        "distinctive_assets": ["the red ACME crate", "ACME stencil logo"],
        "do_not": ["never mock the coyote"],
    },
    "trend_bridge": {
        "fit_score": 4,
        "fit_mode": "direct",
        "bridge": "Skate speed meets the roadrunner sprint meme.",
        "motifs": ["dust cloud"],
        "risks": [],
    },
    "mandatories": ["name Rocket Skates", "free shipping"],
    "avoid": ["anvils"],
    "angles": [
        {"angle_id": "A1", "name": "Finally fast", "tension": "t", "route": "Win."},
    ],
}
CAMPAIGN = {
    "brand": "Acme",
    "target_product": "Rocket Skates",
    "target_audience": "Coyotes",
    "key_selling_points": "fast",
    "target_search_trend": "roadrunner",
}
AD_COPY = {
    "original_id": 3,
    "angle_id": "A1",
    "headline": "Beep beep, who?",
    "body_text": "Rocket Skates.",
    "tone_style": "Deadpan",
    "trend_connection": "c",
    "audience_appeal_rationale": "a",
    "social_caption": "Zoom.",
    "call_to_action": "Order now",
    "detailed_performance_rationale": "p",
}
CONCEPT = {
    "ad_copy_id": 3,
    "concept_name": "Dust",
    "visual_style": "Watercolor",
    "aspect_ratio": "9:16",
    "trend": "roadrunner",
    "trend_reference": "r",
    "markets_product": "m",
    "audience_appeal": "a",
    "selection_rationale": "s",
    "headline": "Beep beep, who?",
    "social_caption": "Zoom.",
    "call_to_action": "Order now",
    "concept_summary": "sum",
    "image_generation_prompt": "Watercolor: Rocket Skates in a dust cloud.",
    "trend_motif": "dust cloud",
    "brand_cue": "the red ACME crate",
    "angle_id": "A1",
}


class TestBriefParsing:
    def test_parse_dict_json_and_garbage(self):
        assert parse_brief(BRIEF) is BRIEF
        assert parse_brief(json.dumps(BRIEF)) == BRIEF
        for bad in (None, "", "not json", "[]", {}, 3):
            assert parse_brief(bad) is None

    def test_block_carries_every_gate_input(self):
        block = format_brief_for_judge(BRIEF)
        for text in (
            "Rocket Skates make every chase {winnable}.",  # braces kept verbatim
            "- Top speed 90 mph",
            "- Lab-tested brakes",
            "- name Rocket Skates",
            "- free shipping",
            "- anvils",
            "- never mock the coyote",
            "fit 4/5 (direct)",
            "Skate speed meets the roadrunner sprint meme.",
            "wry, deadpan",
            "the red ACME crate; ACME stencil logo",
        ):
            assert text in block

    def test_block_without_brief_and_malformed_fields(self):
        assert format_brief_for_judge(None) == NO_BRIEF_BLOCK
        block = format_brief_for_judge({"mandatories": "x", "brand": "y"})
        assert "Single-minded proposition: (none)" in block
        assert "fit unknown (unknown)" in block

    def test_angle_line(self):
        assert angle_line(BRIEF, "A1") == "A1 — Finally fast: Win."
        assert angle_line(BRIEF, "A9") == "A9"
        assert angle_line(None, "") == "(none)"

    def test_brief_braces_survive_prompt_format(self):
        text = eval_prompts.AD_COPY_EVAL_USER.format(
            **CAMPAIGN,
            **AD_COPY,
            angle="A1",
            brief_block=format_brief_for_judge(BRIEF),
        )
        assert "chase {winnable}." in text


class TestPromptsListGates:
    def test_ad_copy_prompt_names_every_gate(self):
        for gate in AD_COPY_GATES:
            assert f"**{gate}**" in eval_prompts.AD_COPY_EVAL_USER

    def test_visual_prompt_names_every_gate(self):
        for gate in VISUAL_GATES:
            assert f"**{gate}**" in eval_prompts.VISUAL_CONCEPT_EVAL_USER

    def test_gate_sets(self):
        assert ADVISORY_GATES == {"brand_cue_present"}
        assert BRIEF_GATES <= set(AD_COPY_GATES) | set(VISUAL_GATES)
        assert "product_named" not in BRIEF_GATES


class TestNormalizeGates:
    def test_orders_drops_unknown_and_passes_missing(self):
        """Conservative: an omitted gate is 'not checked', never a failure."""
        from creative_eval.evaluate import normalize_gates

        raw = [
            GateResult(gate="avoid_respected", passed=True, note="  clean  "),
            GateResult(gate="made_up", passed=False),
            GateResult(gate=" Product_Named ", passed=True, note="named"),
            GateResult(gate="product_named", passed=False, note="dup ignored"),
        ]
        gates = normalize_gates(raw, AD_COPY_GATES, brief_used=True)
        assert [g.gate for g in gates] == list(AD_COPY_GATES)
        by = {g.gate: g for g in gates}
        assert by["product_named"].passed and by["product_named"].note == "named"
        assert by["avoid_respected"].note == "clean"
        assert by["mandatories_met"].passed
        assert by["mandatories_met"].note.startswith("not checked")

    @pytest.mark.parametrize("brief_used", [True, False])
    def test_judge_returning_no_gates_fails_the_creative(self, brief_used):
        """Zero expected gates reported = the judge skipped the checks: unverified."""
        from creative_eval.evaluate import (
            NO_GATES_NOTE,
            gates_passed,
            normalize_gates,
        )

        for raw in ([], [GateResult(gate="made_up", passed=True)]):
            gates = normalize_gates(raw, VISUAL_GATES, brief_used=brief_used)
            assert len(gates) == 1
            assert gates[0].passed is False and not gates[0].advisory
            assert gates[0].note == NO_GATES_NOTE == "judge returned no gates"
            assert not gates_passed(gates)

    @pytest.mark.parametrize(
        "spelling", ["Product Named", "product-named", "PRODUCT_NAMED"]
    )
    def test_lenient_name_matching(self, spelling):
        from creative_eval.evaluate import normalize_gates

        raw = [GateResult(gate=spelling, passed=False, note="missing")]
        by = {g.gate: g for g in normalize_gates(raw, AD_COPY_GATES, brief_used=True)}
        assert by["product_named"].passed is False

    def test_no_brief_passes_brief_gates_with_note(self):
        from creative_eval.evaluate import normalize_gates

        raw = [GateResult(gate=g, passed=False) for g in AD_COPY_GATES]
        by = {g.gate: g for g in normalize_gates(raw, AD_COPY_GATES, brief_used=False)}
        for name in BRIEF_GATES & set(AD_COPY_GATES):
            assert by[name].passed and by[name].note == "no brief"
        assert not by["product_named"].passed  # not brief-dependent

    def test_advisory_is_set_by_code(self):
        from creative_eval.evaluate import normalize_gates

        raw = [GateResult(gate=g, passed=False, advisory=True) for g in VISUAL_GATES]
        gates = normalize_gates(raw, VISUAL_GATES, brief_used=True)
        assert {g.gate for g in gates if g.advisory} == {"brand_cue_present"}


def _judge_json(kind: str, gates: dict[str, bool], score: int = 9) -> str:
    """A judge response; it (wrongly) claims passed/gates_passed True."""
    body = {
        "score": {
            "overall_score": 0.1,
            "passed": True,
            "gates_passed": True,
            "verdicts": [
                {"dimension": "d", "score": score, "verdict": "pass", "rationale": "r"}
            ],
            "strengths": ["s"],
            "improvements": [],
            "gates": [
                {"gate": g, "passed": p, "note": f"{g} note"} for g, p in gates.items()
            ],
        }
    }
    if kind == "ad":
        body |= {"original_id": 3, "headline": "Beep beep, who?", "tone_style": "D"}
    else:
        body |= {"ad_copy_id": 3, "concept_name": "Dust"}
    return json.dumps(body)


def _client(text: str) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=text)
    return client


def _prompt_text(client: MagicMock) -> str:
    contents = client.models.generate_content.call_args.kwargs["contents"]
    if isinstance(contents, str):
        return contents
    return "\n".join(getattr(p, "text", None) or "" for p in contents)


class TestEvaluateAdCopyGates:
    def test_prompt_has_brief_and_angle_and_gates_drive_passed(self):
        from creative_eval.evaluate import evaluate_ad_copy

        gates = dict.fromkeys(AD_COPY_GATES, True) | {"mandatories_met": False}
        client = _client(_judge_json("ad", gates))
        result = evaluate_ad_copy(
            AD_COPY, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        text = _prompt_text(client)
        assert "<CREATIVE_BRIEF>" in text and "- free shipping" in text
        assert "Creative Angle: A1 — Finally fast: Win." in text
        assert result.score.overall_score == 0.9
        assert result.score.gates_passed is False
        assert result.score.passed is False
        assert [g.gate for g in result.score.gates] == list(AD_COPY_GATES)

    def test_all_gates_pass(self):
        from creative_eval.evaluate import evaluate_ad_copy

        client = _client(_judge_json("ad", dict.fromkeys(AD_COPY_GATES, True)))
        result = evaluate_ad_copy(
            AD_COPY, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        assert result.score.gates_passed and result.score.passed

    def test_no_brief_says_so_and_skips_brief_gates(self):
        from creative_eval.evaluate import evaluate_ad_copy

        gates = dict.fromkeys(AD_COPY_GATES, False) | {"product_named": True}
        client = _client(_judge_json("ad", gates))
        result = evaluate_ad_copy(AD_COPY, CAMPAIGN, EvalConfig(), client=client)
        assert NO_BRIEF_BLOCK in _prompt_text(client)
        assert result.score.gates_passed and result.score.passed

    def test_judge_failure_is_unverified(self):
        from creative_eval.evaluate import evaluate_ad_copy

        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError("offline")
        result = evaluate_ad_copy(AD_COPY, CAMPAIGN, EvalConfig(), client=client)
        assert result.score.gates == [] and result.score.gates_passed is False
        assert result.score.improvements == ["evaluation_failed"]


class TestEvaluateVisualGates:
    def test_advisory_brand_cue_does_not_block(self):
        from creative_eval.evaluate import evaluate_visual_concept

        gates = dict.fromkeys(VISUAL_GATES, True) | {"brand_cue_present": False}
        client = _client(_judge_json("visual", gates))
        result = evaluate_visual_concept(
            CONCEPT, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        text = _prompt_text(client)
        assert "Trend Motif: dust cloud" in text
        assert "Brand Cue: the red ACME crate" in text
        assert result.score.gates_passed and result.score.passed
        cue = next(g for g in result.score.gates if g.gate == "brand_cue_present")
        assert cue.advisory and not cue.passed

    def test_failed_product_visible_blocks(self):
        from creative_eval.evaluate import evaluate_visual_concept

        gates = dict.fromkeys(VISUAL_GATES, True) | {"product_visible": False}
        client = _client(_judge_json("visual", gates))
        result = evaluate_visual_concept(
            CONCEPT, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        assert not result.score.gates_passed and not result.score.passed

    def test_concept_without_motif_or_cue_formats(self):
        from creative_eval.evaluate import evaluate_visual_concept

        concept = {
            k: v for k, v in CONCEPT.items() if k not in {"trend_motif", "brand_cue"}
        }
        client = _client(_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
        evaluate_visual_concept(concept, CAMPAIGN, EvalConfig(), client=client)
        assert "Brand Cue: (none)" in _prompt_text(client)


class TestAgentToolBrief:
    def _run(self, monkeypatch, extra_state):
        import creative_eval.agent as ev_agent

        seen = {}

        def fake_concurrent(ads, vis, ctx, config, client=None, *, brief=None, **kw):
            seen["brief"] = brief
            return [_ad_eval(False, False)], []

        monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", fake_concurrent)
        state = {
            "brand": "Acme",
            "target_product": "Rocket Skates",
            "target_search_trends": "roadrunner",
            "ad_copy_critique": {"ad_copies": [AD_COPY]},
            "final_visual_concepts": {"visual_concepts": []},
            **extra_state,
        }
        ctx = FakeToolContext(state)
        result = ev_agent.evaluate_all_creatives(ctx)
        return seen, result, ctx.state["creative_evaluation_report"]

    def test_brief_from_state_json_string(self, monkeypatch):
        seen, result, report = self._run(
            monkeypatch, {"creative_brief": json.dumps(BRIEF)}
        )
        assert seen["brief"] == BRIEF
        assert report["brief_used"] is True
        assert report["passing_threshold"] == 0.7
        assert result["failed_creatives"][0]["failed_gates"] == ["product_named"]

    def test_no_brief(self, monkeypatch):
        seen, _, report = self._run(monkeypatch, {})
        assert seen["brief"] is None and report["brief_used"] is False


def test_evaluate_creatives_records_brief_and_threshold(monkeypatch):
    import creative_eval.evaluate as ev

    monkeypatch.setattr(ev, "_get_client", lambda cfg: MagicMock())
    monkeypatch.setattr(ev, "evaluate_all_concurrently", lambda *a, **k: ([], []))
    report = ev.evaluate_creatives(
        CAMPAIGN, [], [], EvalConfig(passing_threshold=0.8), brief=BRIEF
    )
    assert report.brief_used is True and report.passing_threshold == 0.8


@pytest.mark.parametrize("advisory", [False, True])
def test_finalize_summary_names_failed_checks(advisory):
    from creative_agent.finalize import finalize_summary

    gate = {"gate": "product_named", "passed": False, "advisory": advisory}
    report = {
        "summary": {"total_ad_copies": 1, "overall_pass_rate": 0.0},
        "ad_copy_evaluations": [
            {
                "headline": "Zoom",
                "score": {"overall_score": 0.9, "passed": False, "gates": [gate]},
            }
        ],
    }
    out = finalize_summary({"creative_evaluation_report": report})
    assert "Did not pass: 'Zoom' (ad copy, 0.90" in out
    assert ("failed checks: Product named" in out) is not advisory


# --- Task 7.3: judge the pixels -------------------------------------------------

QA_FAILED = {
    "passed": False,
    "failures": ["product not visible", "gibberish text on the sign"],
}


def _contents(client: MagicMock):
    return client.models.generate_content.call_args.kwargs["contents"]


class TestJudgeThePixels:
    def test_rendered_image_is_attached_as_a_gcs_part(self):
        from creative_eval.evaluate import evaluate_visual_concept

        client = _client(_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
        image = {"gcs_uri": "gs://bkt/run/dust.png", "attempts": 1, "qa": None}
        result = evaluate_visual_concept(
            CONCEPT, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF, image=image
        )
        contents = _contents(client)
        assert isinstance(contents, list)
        file_parts = [p for p in contents if p.file_data is not None]
        assert len(file_parts) == 1
        assert file_parts[0].file_data.file_uri == "gs://bkt/run/dust.png"
        assert file_parts[0].file_data.mime_type == "image/png"
        text = _prompt_text(client)
        assert "judge the rendered image" in text.lower()
        assert "only to understand intent" in text
        assert result.image_judged is True

    @pytest.mark.parametrize(
        ("uri", "mime"),
        [
            ("gs://b/a.jpg", "image/jpeg"),
            ("gs://b/a.JPEG", "image/jpeg"),
            ("gs://b/a.webp", "image/webp"),
            ("gs://b/a", "image/png"),
        ],
    )
    def test_image_mime_from_extension(self, uri, mime):
        from creative_eval.evaluate import image_mime_type

        assert image_mime_type(uri) == mime

    def test_no_image_judges_the_prompt_and_says_so(self):
        from creative_eval.evaluate import evaluate_visual_concept

        for image in (None, {}, {"gcs_uri": ""}, {"gcs_uri": "https://x/y.png"}):
            client = _client(_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
            result = evaluate_visual_concept(
                CONCEPT, CAMPAIGN, EvalConfig(), client=client, image=image
            )
            contents = _contents(client)
            parts = contents if isinstance(contents, list) else []
            assert not [p for p in parts if p.file_data is not None]
            assert "No rendered image is available" in _prompt_text(client)
            assert result.image_judged is False

    def test_image_qa_is_a_hint_line(self):
        from creative_eval.evaluate import evaluate_visual_concept

        client = _client(_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
        image = {"gcs_uri": "gs://b/d.png", "qa": QA_FAILED}
        evaluate_visual_concept(
            CONCEPT, CAMPAIGN, EvalConfig(), client=client, image=image
        )
        text = _prompt_text(client)
        assert (
            "Automated image check: failed — product not visible; "
            "gibberish text on the sign" in text
        )
        assert "not ground truth" in text

    def test_qa_hint_text(self):
        from creative_eval.evaluate import image_qa_hint

        assert image_qa_hint({"passed": True, "failures": []}) == (
            "Automated image check: passed"
        )
        assert image_qa_hint({"passed": False}) == "Automated image check: failed"
        assert image_qa_hint(None) == "Automated image check: not run"

    def test_failed_judge_keeps_image_judged_false(self):
        from creative_eval.evaluate import evaluate_visual_concept

        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError("offline")
        result = evaluate_visual_concept(
            CONCEPT,
            CAMPAIGN,
            EvalConfig(),
            client=client,
            image={"gcs_uri": "gs://b/d.png"},
        )
        assert result.image_judged is False

    def test_concurrent_eval_routes_each_concepts_image(self):
        import creative_eval.evaluate as ev

        seen = {}

        def fake_vis(vc, ctx, config, client=None, brief=None, image=None):
            seen[vc["concept_name"]] = image
            return MagicMock()

        other = {**CONCEPT, "concept_name": "Other"}
        images = {"Dust": {"gcs_uri": "gs://b/d.png"}}
        orig = ev.evaluate_visual_concept
        ev.evaluate_visual_concept = fake_vis
        try:
            ev.evaluate_all_concurrently(
                [],
                [CONCEPT, other],
                CAMPAIGN,
                EvalConfig(),
                MagicMock(),
                generated_images=images,
            )
        finally:
            ev.evaluate_visual_concept = orig
        assert seen == {"Dust": {"gcs_uri": "gs://b/d.png"}, "Other": None}

    def test_agent_tool_passes_generated_images(self, monkeypatch):
        import creative_eval.agent as ev_agent

        seen = {}

        def fake_concurrent(ads, vis, ctx, config, client=None, **kw):
            seen.update(kw)
            return [], []

        monkeypatch.setattr(ev_agent, "evaluate_all_concurrently", fake_concurrent)
        images = {"Dust": {"gcs_uri": "gs://b/d.png"}}
        ctx = FakeToolContext(
            {
                **CAMPAIGN,
                "final_visual_concepts": {"visual_concepts": [CONCEPT]},
                "generated_images": images,
            }
        )
        ev_agent.evaluate_all_creatives(ctx)
        assert seen["generated_images"] == images

    def test_eval_config_has_no_unused_max_retries(self):
        assert not hasattr(EvalConfig(), "max_retries")


# --- conservative gate wording (false positives wrongly fail creatives) ------


class TestConservativeGateWording:
    def test_ad_copy_leniency_clauses(self):
        text = eval_prompts.AD_COPY_EVAL_USER
        assert "short form" in text  # 'SE CE24' for 'PRS SE CE24' counts
        assert "only an image can carry" in text  # visual mandatories ignored
        assert "paraphrase counts" in text
        assert "clear, literal violation" in text

    def test_visual_leniency_clauses(self):
        text = eval_prompts.VISUAL_CONCEPT_EVAL_USER
        assert "Stylised renderings count" in text
        assert "no physical form" in text
        assert "quotes that only describe the scene are not in-image text" in text
        assert "Case, line breaks and minor punctuation differences are fine" in text
        assert "clear, literal violation" in text

    def test_templates_have_no_stray_placeholders(self):
        import string

        fields = {
            f
            for t in (
                eval_prompts.AD_COPY_EVAL_USER,
                eval_prompts.VISUAL_CONCEPT_EVAL_USER,
            )
            for _, f, _, _ in string.Formatter().parse(t)
            if f
        }
        expected_extra = {
            "brief_block",
            "angle",
            "image_section",
            "trend_motif",
            "brand_cue",
        }
        assert expected_extra <= fields
        assert {f for f in fields if not f.isidentifier()} == set()
        assert [
            f
            for _, f, _, _ in string.Formatter().parse(
                eval_prompts.VISUAL_IMAGE_ATTACHED
            )
            if f
        ] == ["qa_hint"]
        assert not [
            f
            for _, f, _, _ in string.Formatter().parse(
                eval_prompts.VISUAL_IMAGE_MISSING
            )
            if f
        ]

    def test_qa_failure_text_with_braces_survives(self):
        from creative_eval.evaluate import evaluate_visual_concept

        client = _client(_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
        qa = {"passed": False, "failures": ["sign reads {oops}"]}
        evaluate_visual_concept(
            CONCEPT,
            CAMPAIGN,
            EvalConfig(),
            client=client,
            image={"gcs_uri": "gs://b/d.png", "qa": qa},
        )
        assert "sign reads {oops}" in _prompt_text(client)


# --- fail-soft: an unreadable image never zeroes a creative ---------------------


def _client_error(code: int):
    from google.genai import errors

    return errors.ClientError(code, {"error": {"message": "nope", "status": "X"}})


class TestImageFailSoft:
    def test_unreadable_image_falls_back_to_prompt(self):
        from creative_eval.evaluate import evaluate_visual_concept

        client = MagicMock()
        ok = MagicMock(text=_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
        client.models.generate_content.side_effect = [_client_error(403), ok]
        result = evaluate_visual_concept(
            CONCEPT,
            CAMPAIGN,
            EvalConfig(),
            client=client,
            image={"gcs_uri": "gs://b/d.png"},
        )
        assert client.models.generate_content.call_count == 2
        assert isinstance(
            client.models.generate_content.call_args.kwargs["contents"], str
        )
        assert result.image_judged is False
        assert result.score.passed is True

    def test_quota_error_is_not_retried_without_image(self):
        from creative_eval.evaluate import evaluate_visual_concept

        client = MagicMock()
        client.models.generate_content.side_effect = _client_error(429)
        result = evaluate_visual_concept(
            CONCEPT,
            CAMPAIGN,
            EvalConfig(),
            client=client,
            image={"gcs_uri": "gs://b/d.png"},
        )
        assert client.models.generate_content.call_count == 1
        assert result.score.improvements == ["evaluation_failed"]

    def test_fallback_is_a_report_warning(self):
        from creative_eval.evaluate import (
            image_fallback_concepts,
            image_fallback_warning,
        )

        def ev(name, judged, failed=False):
            return VisualConceptEvaluation(
                ad_copy_id=1,
                concept_name=name,
                image_judged=judged,
                score=CreativeScore(
                    overall_score=0.8,
                    passed=True,
                    verdicts=[],
                    strengths=[],
                    improvements=["evaluation_failed"] if failed else [],
                ),
            )

        images = {n: {"gcs_uri": f"gs://b/{n}.png"} for n in ("A", "B", "C")}
        evals = [
            ev("A", True),
            ev("B", False),
            ev("C", False, failed=True),
            ev("D", False),
        ]
        assert image_fallback_concepts(evals, images) == ["B"]
        (note,) = image_fallback_warning(["B"])
        assert "judged the prompt" in note and "B" in note
        assert image_fallback_warning([]) == []


# --- judge-facing response schemas (the judge cannot skip or self-grade) -------


class TestJudgeSchemas:
    def test_gates_are_required_and_non_empty(self):
        from pydantic import ValidationError

        from creative_eval.schemas import AdCopyJudgeOutput, VisualJudgeOutput

        score = {"verdicts": [], "strengths": [], "improvements": []}
        for model, ids in (
            (AdCopyJudgeOutput, {"original_id": 1, "headline": "h", "tone_style": "t"}),
            (VisualJudgeOutput, {"ad_copy_id": 1, "concept_name": "c"}),
        ):
            for bad in (score, {**score, "gates": []}):
                with pytest.raises(ValidationError):
                    model.model_validate({**ids, "score": bad})
            ok = {**score, "gates": [{"gate": "g", "passed": True, "note": ""}]}
            model.model_validate({**ids, "score": ok})
            schema = json.dumps(model.model_json_schema())
            assert '"minItems": 1' in schema

    def test_code_set_fields_are_not_in_the_judge_schema(self):
        from creative_eval.schemas import (
            AdCopyJudgeOutput,
            GateResultIn,
            JudgeScore,
            VisualJudgeOutput,
        )

        assert "advisory" not in GateResultIn.model_fields
        assert not {"overall_score", "passed", "gates_passed"} & set(
            JudgeScore.model_fields
        )
        assert "image_judged" not in VisualJudgeOutput.model_fields
        assert set(JudgeScore.model_fields) >= {"gates", "verdicts"}
        assert AdCopyJudgeOutput.model_fields["score"].annotation is JudgeScore

    def test_calls_use_the_judge_schemas(self):
        from creative_eval.evaluate import evaluate_ad_copy, evaluate_visual_concept
        from creative_eval.schemas import AdCopyJudgeOutput, VisualJudgeOutput

        client = _client(_judge_json("ad", dict.fromkeys(AD_COPY_GATES, True)))
        evaluate_ad_copy(AD_COPY, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF)
        cfg = client.models.generate_content.call_args.kwargs["config"]
        assert cfg.response_schema is AdCopyJudgeOutput
        client = _client(_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
        evaluate_visual_concept(CONCEPT, CAMPAIGN, EvalConfig(), client=client)
        cfg = client.models.generate_content.call_args.kwargs["config"]
        assert cfg.response_schema is VisualJudgeOutput

    def test_judge_returning_zero_gates_does_not_pass(self):
        from creative_eval.evaluate import evaluate_ad_copy

        client = _client(_judge_json("ad", {}))
        result = evaluate_ad_copy(
            AD_COPY, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        assert result.score.passed is False and result.score.gates_passed is False

    def test_judge_reporting_only_unknown_gates_does_not_pass(self):
        from creative_eval.evaluate import NO_GATES_NOTE, evaluate_visual_concept

        client = _client(_judge_json("visual", {"looks_great": True}))
        result = evaluate_visual_concept(
            CONCEPT, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        assert result.score.overall_score == 0.9
        assert result.score.passed is False and result.score.gates_passed is False
        assert [g.note for g in result.score.gates] == [NO_GATES_NOTE]

    def test_code_maps_judge_output_to_report_models(self):
        from creative_eval.evaluate import evaluate_visual_concept

        client = _client(_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
        result = evaluate_visual_concept(
            CONCEPT, CAMPAIGN, EvalConfig(), client=client, brief=BRIEF
        )
        assert isinstance(result, VisualConceptEvaluation)
        assert result.concept_name == "Dust" and result.ad_copy_id == 3
        assert result.score.strengths == ["s"]  # model text kept
        assert result.score.overall_score == 0.9  # judge's 0.1 ignored


# --- partially omitted gates: pass, but surface a report warning --------------


def _eval_with_gates(notes: list[str]) -> AdCopyEvaluation:
    ev = _ad_eval(True, True)
    ev.score.gates = [
        GateResult(gate=f"g{i}", passed=True, note=n) for i, n in enumerate(notes)
    ]
    return ev


class TestUnreportedGatesWarning:
    def test_partial_omission_passes_with_not_checked_note(self):
        from creative_eval.evaluate import NOT_REPORTED_NOTE, normalize_gates

        raw = [GateResult(gate="product_visible", passed=True)]
        gates = normalize_gates(raw, VISUAL_GATES, brief_used=True)
        assert all(g.passed for g in gates)
        assert sum(g.note == NOT_REPORTED_NOTE for g in gates) == 4

    def test_warning_counts_not_checked_gates(self):
        from creative_eval.evaluate import NOT_REPORTED_NOTE, unreported_gates_warning

        evals = [
            _eval_with_gates([NOT_REPORTED_NOTE, "ok"]),
            _eval_with_gates([NOT_REPORTED_NOTE]),
        ]
        assert unreported_gates_warning(evals, []) == [
            "2 checks not reported by the judge (passed as not checked)"
        ]
        assert unreported_gates_warning(evals[1:], []) == [
            "1 check not reported by the judge (passed as not checked)"
        ]
        assert unreported_gates_warning([_eval_with_gates(["ok"])], []) == []

    def test_report_carries_the_warning(self, monkeypatch):
        import creative_eval.evaluate as ev

        monkeypatch.setattr(ev, "_get_client", lambda cfg: MagicMock())
        evals = [_eval_with_gates([ev.NOT_REPORTED_NOTE])]
        monkeypatch.setattr(
            ev, "evaluate_all_concurrently", lambda *a, **k: (evals, [])
        )
        report = ev.evaluate_creatives(CAMPAIGN, [], [], EvalConfig(), brief=BRIEF)
        assert any("not reported by the judge" in w for w in report.warnings)

    def test_agent_tool_report_carries_the_warning(self, monkeypatch):
        import creative_eval.agent as ev_agent
        from creative_eval.evaluate import NOT_REPORTED_NOTE

        evals = [_eval_with_gates([NOT_REPORTED_NOTE])]
        monkeypatch.setattr(
            ev_agent, "evaluate_all_concurrently", lambda *a, **k: (evals, [])
        )
        ctx = FakeToolContext(
            {**CAMPAIGN, "ad_copy_critique": {"ad_copies": [AD_COPY]}}
        )
        ev_agent.evaluate_all_creatives(ctx)
        warnings = ctx.state["creative_evaluation_report"]["warnings"]
        assert any("not reported by the judge" in w for w in warnings)


# --- gate-default wording: presence vs violation checks agree ----------------


class TestGateDefaults:
    @pytest.mark.parametrize(
        ("system", "user", "presence", "violation"),
        [
            (
                eval_prompts.AD_COPY_EVAL_SYSTEM,
                eval_prompts.AD_COPY_EVAL_USER,
                ("delivers_proposition", "product_named", "uses_reason_to_believe"),
                ("mandatories_met", "avoid_respected"),
            ),
            (
                eval_prompts.VISUAL_CONCEPT_EVAL_SYSTEM,
                eval_prompts.VISUAL_CONCEPT_EVAL_USER,
                ("product_visible", "trend_motif_visible", "text_correct"),
                ("avoid_respected",),
            ),
        ],
    )
    def test_presence_and_violation_defaults(self, system, user, presence, violation):
        for text in (system, user):
            assert "Fail a gate only on clear evidence" not in text
            assert "pass only when the rule is" not in text
            assert "Presence checks" in text and "Violation checks" in text
            assert "pass only when" in text and "fail only on a clear violation" in text
        rule = next(
            line for line in user.splitlines() if line.startswith("Presence checks")
        )
        assert all(g in rule for g in presence)
        rule = next(
            line for line in user.splitlines() if line.startswith("Violation checks")
        )
        assert all(g in rule for g in violation)
        assert "does not apply" in user and "reason in the note" in user


# --- image fallback limited to image-related client errors -------------------


def _client_error_msg(code: int, message: str):
    from google.genai import errors

    return errors.ClientError(code, {"error": {"message": message, "status": "X"}})


class TestImageFallbackLimited:
    def _run(self, err):
        from creative_eval.evaluate import evaluate_visual_concept

        client = MagicMock()
        ok = MagicMock(text=_judge_json("visual", dict.fromkeys(VISUAL_GATES, True)))
        client.models.generate_content.side_effect = [err, ok]
        result = evaluate_visual_concept(
            CONCEPT,
            CAMPAIGN,
            EvalConfig(),
            client=client,
            image={"gcs_uri": "gs://b/d.png"},
        )
        return client.models.generate_content.call_count, result

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            (403, "nope"),
            (404, "nope"),
            (400, "Unable to process input image."),
            (400, "Invalid file_uri gs://b/d.png"),
            (400, "The caller does not have Permission"),
            (400, "Cannot fetch content from the provided URI."),
        ],
    )
    def test_image_errors_fall_back_to_the_prompt(self, code, message):
        calls, result = self._run(_client_error_msg(code, message))
        assert calls == 2 and result.image_judged is False and result.score.passed

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            (400, "Request contains an invalid argument: temperature"),
            (400, "Schema is too complex"),
            (401, "Unauthenticated: image access"),
            (429, "Resource exhausted"),
        ],
    )
    def test_other_client_errors_are_judge_failures(self, code, message):
        calls, result = self._run(_client_error_msg(code, message))
        assert calls == 1
        assert result.score.improvements == ["evaluation_failed"]
        assert result.score.passed is False

    def test_is_image_fallback_error_pure(self):
        from creative_eval.evaluate import is_image_fallback_error

        assert is_image_fallback_error(_client_error_msg(404, ""))
        assert is_image_fallback_error(_client_error_msg(400, "bad IMAGE"))
        assert not is_image_fallback_error(_client_error_msg(400, ""))
        assert not is_image_fallback_error(_client_error_msg(429, "image"))


# --- finalize_summary: gate pass rate + failed judge entries ------------------


class TestFinalizeSummaryGates:
    def _report(self, **summary):
        return {
            "summary": {
                "total_ad_copies": 2,
                "total_visual_concepts": 2,
                "ad_copies_passed": 1,
                "visual_concepts_passed": 1,
                "overall_pass_rate": 0.5,
                **summary,
            },
            "ad_copy_evaluations": [
                {
                    "headline": "Zoom",
                    "score": {
                        "overall_score": 0.0,
                        "passed": False,
                        "improvements": ["evaluation_failed"],
                        "gates": [],
                    },
                },
                {"headline": "Ok", "score": {"overall_score": 0.9, "passed": True}},
            ],
            "visual_concept_evaluations": [
                {
                    "concept_name": "Dust",
                    "score": {
                        "overall_score": 0.9,
                        "passed": True,
                        "gates_passed": True,
                    },
                },
                {
                    "concept_name": "Gone",
                    "score": {
                        "overall_score": 0.9,
                        "passed": False,
                        "gates_passed": False,
                        "gates": [{"gate": "product_visible", "passed": False}],
                    },
                },
            ],
        }

    def test_gates_pass_rate_line(self):
        from creative_agent.finalize import finalize_summary

        out = finalize_summary(
            {"creative_evaluation_report": self._report(gates_pass_rate=2 / 3)}
        )
        assert "2/3 passed all checks (gates pass rate 67%)" in out

    def test_no_gate_line_without_rate(self):
        from creative_agent.finalize import finalize_summary

        for rate in (None, "garbage"):
            out = finalize_summary(
                {"creative_evaluation_report": self._report(gates_pass_rate=rate)}
            )
            assert "passed all checks" not in out

    def test_failed_judge_entry_says_evaluation_failed(self):
        from creative_agent.finalize import finalize_summary

        out = finalize_summary({"creative_evaluation_report": self._report()})
        assert "'Zoom' (ad copy, 0.00, evaluation failed)" in out
        assert "'Gone' (visual, 0.90, failed checks: Product visible)" in out
