"""Safer angle for sensitive trends (PR A2): the copy gate's endorsement and
trend-tie checks, ``safer_angle_named_copies``, the trend-tie skip on
trend_scout's sensitivity hint, and the image-QA public-figure rule.

State contract (written by PR A1): ``safer_angle_active`` / ``trend_sensitive_hint``
"yes" or "", the brief's ``trend_bridge.safer_angle``. Missing keys = inactive.
"""

from __future__ import annotations

import pytest

from creative_agent.copy_gate import (
    brief_risks,
    brief_safer_angle,
    gate_copies,
    safer_angle_named_copies,
    safer_angle_tie_avoid,
)

TREND = "tupac shakur murder trial"
PRODUCT = "Hohner Clavinet D7"
RISKS = [
    "Never reference the murder or the trial.",
    "Never imply that Tupac Shakur or his estate endorses the product.",
]
ANGLE = "Tupac Shakur's legacy of chasing your music dreams"


def _copy(original_id=1, **overrides):
    copy = {
        "original_id": original_id,
        "tone_style": "Inspirational",
        "headline": "Chase the sound like Tupac",
        "body_text": "The Hohner Clavinet D7 keeps your music dreams funky.",
        "social_caption": "Keys for dreamers.",
        "call_to_action": "Find your sound",
    }
    copy.update(overrides)
    return copy


def _texts(issues):
    """Issue texts, minus the at-least-half safer-angle tie (its own file,
    tests/test_safer_angle_copy_tie.py; these single-copy cases would always
    trip it)."""
    texts = {
        k: [str(i) for i in v if not str(i).startswith("not tied to the safer angle")]
        for k, v in issues.items()
    }
    return {k: v for k, v in texts.items() if v}


def _gate(*copies, **kwargs):
    kwargs.setdefault("target_product", PRODUCT)
    kwargs.setdefault("trend_terms", TREND)
    kwargs.setdefault("risks", RISKS)
    kwargs.setdefault("safer_angle", ANGLE)
    return _texts(gate_copies(list(copies), **kwargs))


# --- copy gate, active -------------------------------------------------------
def test_active_flags_endorsement_wording_in_any_field():
    for field in ("headline", "body_text", "social_caption", "call_to_action"):
        bad = _copy(**{field: "The official Tupac keys, Hohner Clavinet D7"})
        issues = _gate(bad, safer_angle_active=True, trend_sensitive=True)
        assert issues == {
            "1": [
                "implies endorsement or speaks for the real person (official): "
                "remove it."
            ]
        }, field


def test_inactive_ignores_endorsement_wording():
    bad = _copy(headline="The official Tupac keys")
    assert _gate(bad, trend_sensitive=True) == {}
    assert _gate(bad) == {}


def test_active_re_enables_the_trend_tie_on_a_sensitive_trend():
    untied = _copy(headline="Funky keys", body_text="The Hohner Clavinet D7 grooves.")
    assert _gate(untied, trend_sensitive=True) == {}
    (issue,) = _gate(untied, trend_sensitive=True, safer_angle_active=True)["1"]
    assert issue.startswith("not clearly tied to the trend")
    # The event's words are never offered as examples.
    assert "Murder" not in issue and "Trial" not in issue
    assert "Tupac" in issue


def test_active_event_words_never_count_as_a_tie():
    trial = _copy(
        headline="A trial run on the keys",
        body_text="The Hohner Clavinet D7 grooves.",
    )
    (issue,) = _gate(trial, safer_angle_active=True)["1"]
    assert issue.startswith("not clearly tied to the trend")
    # Without the risks, "trial" is just a distinctive trend word.
    assert _gate(trial, safer_angle_active=True, risks=()) == {}


def test_active_avoid_terms_never_count_as_a_tie():
    copy = _copy(headline="Shakur energy", body_text="Hohner Clavinet D7 grooves.")
    assert _gate(copy, safer_angle_active=True) == {}
    avoided, untied = _gate(copy, safer_angle_active=True, avoid=["shakur"])["1"]
    assert avoided.startswith("contains the avoided term 'shakur'")
    assert untied.startswith("not clearly tied to the trend")


def test_safer_angle_tie_avoid_keeps_the_subject():
    assert safer_angle_tie_avoid(
        TREND, avoid=["x"], risks=RISKS, safer_angle=ANGLE
    ) == ["x", "murder", "trial"]
    # Risks naming the subject do not drop it while the angle uses it.
    assert "tupac" not in safer_angle_tie_avoid(TREND, risks=RISKS, safer_angle=ANGLE)
    assert "tupac" in safer_angle_tie_avoid(TREND, risks=RISKS, safer_angle="")


def test_named_copies():
    copies = [
        _copy(1),
        _copy(2, headline="Funky keys", body_text="Hohner Clavinet D7."),
        _copy(3, headline="A trial run", body_text="Hohner Clavinet D7."),
        _copy(4, headline="Shakur style"),
    ]
    assert safer_angle_named_copies(
        copies, trend_terms=TREND, risks=RISKS, safer_angle=ANGLE
    ) == ["1", "4"]
    assert safer_angle_named_copies(copies, trend_terms="") == []
    assert safer_angle_named_copies("junk", trend_terms=TREND) == []


def test_brief_helpers():
    brief = {"trend_bridge": {"risks": [" a ", 3, ""], "safer_angle": " angle "}}
    assert brief_risks(brief) == ["a"]
    assert brief_safer_angle(brief) == "angle"
    assert brief_risks(None) == [] and brief_safer_angle({}) == ""


# --- agent wiring ------------------------------------------------------------
def _brief():
    from tests.test_creative_agent_graph import _BRIEF

    brief = dict(_BRIEF)
    brief["trend_bridge"] = {
        **brief["trend_bridge"],
        "risks": RISKS,
        "safer_angle": ANGLE,
        "motifs": ["clavinet funk"],
    }
    brief["avoid"] = []
    brief["mandatories"] = []
    return brief


def _final(original_id, **overrides):
    copy = _copy(original_id, **overrides)
    copy["brief_checks"] = [
        {"item": item, "passed": True, "note": ""}
        for item in ("proposition", "mandatories", "risks", "trend_bridge")
    ]
    return copy


def _state(*copies, **extra):
    copies = copies or tuple(_final(i) for i in range(1, 5))
    return {
        "ad_copy_critique": {"ad_copies": list(copies)},
        "target_product": PRODUCT,
        "brand": "Hohner",
        "target_search_trends": TREND,
        "creative_brief": _brief(),
        **extra,
    }


UNTIED = {"headline": "Funky keys", "body_text": "Hohner Clavinet D7 grooves."}


def test_gate_decision_active_records_named_copies_and_flags():
    from creative_agent.agent import copy_gate_decision

    copies = (
        _final(1),
        _final(2, headline="His favourite keys, Tupac"),
        _final(3, **UNTIED),
        _final(4),
    )
    state = _state(*copies, safer_angle_active="yes", trend_sensitive_hint="yes")
    route, delta = copy_gate_decision(state, 1)
    assert route == "revise"
    assert delta["ad_copy_flagged_ids"] == ["2", "3"]
    assert "implies endorsement" in delta["ad_copy_issues"]
    assert "his favourite" in delta["ad_copy_issues"]
    assert "not clearly tied to the trend" in delta["ad_copy_issues"]
    assert delta["safer_angle_named_copies"] == ["1", "2", "4"]

    clean = _state(safer_angle_active="yes")
    route, delta = copy_gate_decision(clean, 1)
    assert route == "ok"
    assert delta["safer_angle_named_copies"] == ["1", "2", "3", "4"]


def test_gate_decision_inactive_writes_no_named_copies():
    from creative_agent.agent import copy_gate_decision

    for extra in ({}, {"safer_angle_active": ""}, {"safer_angle_active": True}):
        _, delta = copy_gate_decision(_state(**extra), 1)
        assert "safer_angle_named_copies" not in delta, extra


@pytest.mark.parametrize(
    "extra",
    [
        {"trend_sensitive_hint": "yes"},
        {"trend_situation": {"phase": "sensitive", "timely_ok": False}},
    ],
)
def test_inactive_trend_tie_skipped_on_hint_or_phase(extra):
    from creative_agent.agent import copy_gate_decision

    state = _state(_final(1, **UNTIED), *(_final(i) for i in (2, 3, 4)), **extra)
    assert copy_gate_decision(state, 1)[0] == "ok"
    # Without either signal the tie is checked.
    plain = _state(_final(1, **UNTIED), *(_final(i) for i in (2, 3, 4)))
    assert copy_gate_decision(plain, 1)[0] == "revise"
    # A hint other than "yes" does not skip it.
    other = {**plain, "trend_sensitive_hint": ""}
    assert copy_gate_decision(other, 1)[0] == "revise"


def test_interactive_residual_applies_the_same_checks():
    from creative_agent.agent import cap_and_residual_copies

    copies = (_final(1, headline="RIP Tupac"), *(_final(i) for i in (2, 3, 4)))
    delta = cap_and_residual_copies(_state(*copies, safer_angle_active="yes"))
    (issue,) = delta["ad_copy_critique__issues"]
    assert "implies endorsement or speaks for the real person (rip)" in issue
    assert delta["safer_angle_named_copies"] == ["1", "2", "3", "4"]
    off = cap_and_residual_copies(_state(*copies))
    assert off["ad_copy_critique__issues"] is None
    assert "safer_angle_named_copies" not in off


# --- image QA: the public-figure likeness check ------------------------------
from creative_agent import image_qa, image_tools  # noqa: E402
from tests.test_image_qa import (  # noqa: E402
    _CONCEPT,
    _PRODUCT,
    _Flow,
    _QAClient,
    _result,
)


def _inspect(*, safer_angle_active, payload=None):
    client = _QAClient(payload or _result(public_figure_likeness=True).model_dump())
    kwargs = {"safer_angle_active": True} if safer_angle_active else {}
    result = image_qa.inspect_image(
        b"x",
        "image/png",
        _CONCEPT,
        brand="PRS",
        target_product=_PRODUCT,
        client=client,
        model="m",
        **kwargs,
    )
    (call,) = client.models.calls
    text = " ".join(p.text for p in call["contents"] if getattr(p, "text", None))
    return result, text, call["config"].response_schema


def test_inspect_asks_the_public_figure_question_only_when_active():
    result, text, schema = _inspect(safer_angle_active=True)
    assert image_qa.PUBLIC_FIGURE_LINE in text
    assert "recognisable likeness of a real public figure (not the cast person)" in text
    assert schema is image_qa.ImageQAResult
    assert result.public_figure_likeness is True

    off, off_text, off_schema = _inspect(safer_angle_active=False)
    assert "public_figure_likeness" not in off_text
    assert "public_figure_likeness" not in off_schema.model_fields
    # A stray verdict is cleared without the question.
    assert off.public_figure_likeness is False


def test_public_figure_rule_is_critical_with_its_correction():
    result = _result(public_figure_likeness=True)
    rules = image_qa.qa_failed_rules(result, _CONCEPT, target_product=_PRODUCT)
    assert rules == ["real public figure likeness"]
    assert "real public figure likeness" in image_qa.CRITICAL_RULES
    text = image_qa.correction_text(result, _CONCEPT, target_product=_PRODUCT)
    assert (
        "show the theme through instruments, era and setting, never the real "
        "person's likeness" in text
    )
    assert image_qa.qa_failed_rules(_result(), _CONCEPT, target_product=_PRODUCT) == []


def test_public_figure_likeness_weighs_first_when_keeping_an_attempt():
    # A likeness (1 critical) loses to two non-critical failures.
    likeness = ["real public figure likeness"]
    two_minor = ["malformed hands", "severe visual artifacts"]
    assert image_tools._keep_retry(likeness, two_minor) is True
    assert image_tools._keep_retry(two_minor, likeness) is False


def _safer_flow(monkeypatch, verdicts, state):
    flow = _Flow(monkeypatch, verdicts)
    calls: list[dict] = []
    queue = list(verdicts)

    def fake_inspect(image_bytes, mime, concept, **kwargs):
        calls.append(kwargs)
        return queue.pop(0)

    monkeypatch.setattr(image_qa, "inspect_image", fake_inspect)
    flow.ctx.state.update(state)
    flow.run()
    return flow, calls


def test_generate_image_plumbs_the_safer_angle_to_image_qa(monkeypatch):
    likeness = _result(public_figure_likeness=True)
    flow, calls = _safer_flow(
        monkeypatch, [likeness, _result()], {"safer_angle_active": "yes"}
    )
    assert [c.get("safer_angle_active") for c in calls] == [True, True]
    assert len(flow.models.calls) == 2  # re-rendered
    retry_prompt = flow.models.calls[1]["contents"]
    assert "never the real person's likeness" in retry_prompt
    assert flow.record["qa"]["public_figure_likeness"] is False
    assert flow.record["qa"]["passed"] is True


@pytest.mark.parametrize("state", [{}, {"safer_angle_active": ""}])
def test_generate_image_without_the_safer_angle_is_unchanged(monkeypatch, state):
    flow, calls = _safer_flow(monkeypatch, [_result()], state)
    assert all("safer_angle_active" not in c for c in calls)
    assert "public_figure_likeness" not in flow.record["qa"]
