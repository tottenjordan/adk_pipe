"""Post-render image QA (creative_agent/image_qa.py) + generate_image's
inspect → targeted re-render flow.

The pure rule (``qa_failures`` / ``qa_failed_rules``), the expected-text
extraction and ``inspect_image``'s request are tested with a fake genai client;
the generate_image flow fakes both the image client and the QA call.
"""

import json

import pytest

from creative_agent import image_qa
from creative_agent.image_qa import ImageQAResult

_CONCEPT = {
    "concept_name": "Jackpot",
    "image_generation_prompt": (
        'A risograph poster of a guitar beside a lottery ball, headline reads "Play Loud"'
    ),
    "trend_motif": "a lottery ball",
    "brand_cue": "PRS bird inlays",
    "headline": "Play Loud",
    "call_to_action": "Shop now",
    "visual_style": "risograph",
}


def _result(**overrides) -> ImageQAResult:
    base = {
        "product_visible": True,
        "motif_visible": True,
        "brand_cue_visible": True,
        "text_expected": True,
        "text_exact": True,
        "text_legible": True,
        "gibberish_text": False,
        "unrequested_logos": False,
        "artifacts": False,
        "unsafe": False,
        "issues": [],
    }
    base.update(overrides)
    return ImageQAResult(**base)


# --- expected_text ---
def test_expected_text_extracts_in_image_quotes():
    assert image_qa.expected_text(_CONCEPT) == ["Play Loud"]


def test_expected_text_none_without_in_image_quotes():
    concept = {"image_generation_prompt": 'a guitar bathed in "golden hour" light'}
    assert image_qa.expected_text(concept) is None


def test_expected_text_none_for_missing_prompt():
    assert image_qa.expected_text({}) is None


# --- qa_failures matrix ---
def test_clean_result_passes():
    assert image_qa.qa_failures(_result(), _CONCEPT) == []


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"product_visible": False}, "product not visible"),
        ({"motif_visible": False}, "trend motif not visible"),
        ({"gibberish_text": True}, "gibberish text"),
        ({"unrequested_logos": True}, "unrequested third-party logo"),
        ({"artifacts": True}, "severe visual artifacts"),
        ({"unsafe": True}, "unsafe content"),
        ({"text_exact": False}, "in-image text not exact"),
        ({"text_legible": False}, "in-image text not legible"),
    ],
)
def test_each_rule_fails_with_rule_name_fallback(overrides, rule):
    result = _result(**overrides)
    assert image_qa.qa_failed_rules(result, _CONCEPT) == [rule]
    assert image_qa.qa_failures(result, _CONCEPT) == [rule]


def test_model_issues_preferred_over_rule_names():
    result = _result(unrequested_logos=True, issues=["  Nike swoosh on the shirt "])
    assert image_qa.qa_failures(result, _CONCEPT) == ["Nike swoosh on the shirt"]


def test_model_issues_ignored_when_no_rule_failed():
    """Advisory model issues alone never fail an image."""
    result = _result(brand_cue_visible=False, issues=["bird inlays not visible"])
    assert image_qa.qa_failures(result, _CONCEPT) == []


def test_brand_cue_missing_is_advisory():
    assert image_qa.qa_failures(_result(brand_cue_visible=False), _CONCEPT) == []


def test_text_checks_ignored_when_no_text_expected():
    concept = {**_CONCEPT, "image_generation_prompt": "a guitar beside a ball"}
    result = _result(text_expected=False, text_exact=False, text_legible=False)
    assert image_qa.qa_failures(result, concept) == []


def test_text_checks_apply_when_prompt_expects_text_even_if_model_says_not():
    """The deterministic expectation (quoted in-image text) wins over the
    model's text_expected flag."""
    result = _result(text_expected=False, text_exact=False)
    assert image_qa.qa_failed_rules(result, _CONCEPT) == ["in-image text not exact"]


def test_text_unknown_is_not_a_failure():
    assert (
        image_qa.qa_failures(_result(text_exact=None, text_legible=None), _CONCEPT)
        == []
    )


# --- inspect_image ---
class _Resp:
    def __init__(self, text):
        self.text = text
        self.parsed = None


class _QAModels:
    def __init__(self, payload):
        self.calls = []
        self._payload = payload

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(json.dumps(self._payload))


class _QAClient:
    def __init__(self, payload):
        self.models = _QAModels(payload)


def test_inspect_image_sends_image_and_schema():
    client = _QAClient(_result(unrequested_logos=True).model_dump())

    result = image_qa.inspect_image(
        b"\x89PNG",
        "image/png",
        _CONCEPT,
        brand="PRS",
        target_product="PRS SE guitar",
        client=client,
        model="qa-model",
    )

    assert result.unrequested_logos is True
    (call,) = client.models.calls
    assert call["model"] == "qa-model"
    cfg = call["config"]
    assert cfg.response_schema is ImageQAResult
    assert cfg.response_mime_type == "application/json"
    assert cfg.temperature == 0
    contents = call["contents"]
    image_parts = [p for p in contents if getattr(p, "inline_data", None)]
    assert image_parts and image_parts[0].inline_data.data == b"\x89PNG"
    text = " ".join(p.text for p in contents if getattr(p, "text", None))
    for needle in ("PRS SE guitar", "a lottery ball", "PRS bird inlays", "Play Loud"):
        assert needle in text
    assert "only the campaign brand's logo" in text.lower()


def test_inspect_image_without_expected_text_says_none():
    client = _QAClient(_result(text_expected=False).model_dump())
    concept = {**_CONCEPT, "image_generation_prompt": "a guitar", "brand_cue": ""}
    image_qa.inspect_image(
        b"x",
        "image/png",
        concept,
        brand="PRS",
        target_product="guitar",
        client=client,
        model="m",
    )
    contents = client.models.calls[0]["contents"]
    text = " ".join(p.text for p in contents if getattr(p, "text", None))
    assert "no in-image text was requested" in text.lower()
    assert "brand cue" not in text.lower()


def test_inspect_image_uses_parsed_when_present():
    class _ParsedModels:
        def generate_content(self, **kwargs):
            resp = _Resp("not json")
            resp.parsed = _result(artifacts=True)
            return resp

    class _C:
        models = _ParsedModels()

    result = image_qa.inspect_image(
        b"x",
        "image/png",
        _CONCEPT,
        brand="b",
        target_product="p",
        client=_C(),
        model="m",
    )
    assert result.artifacts is True


# --- config knobs ---
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),
        ("", True),
        ("true", True),
        ("1", True),
        ("false", False),
        ("0", False),
        ("OFF", False),
        (" no ", False),
    ],
)
def test_image_qa_enabled_env(monkeypatch, raw, expected):
    from creative_agent.config import ResearchConfiguration

    if raw is None:
        monkeypatch.delenv("IMAGE_QA_ENABLED", raising=False)
    else:
        monkeypatch.setenv("IMAGE_QA_ENABLED", raw)
    assert ResearchConfiguration().image_qa_enabled is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 1), ("", 1), ("0", 0), ("2", 2), ("5", 2), ("-1", 0), ("x", 1)],
)
def test_image_qa_max_rerenders_env(monkeypatch, raw, expected):
    from creative_agent.config import ResearchConfiguration

    if raw is None:
        monkeypatch.delenv("IMAGE_QA_MAX_RERENDERS", raising=False)
    else:
        monkeypatch.setenv("IMAGE_QA_MAX_RERENDERS", raw)
    assert ResearchConfiguration().image_qa_max_rerenders == expected


def test_image_qa_model_defaults_to_worker_model(monkeypatch):
    from creative_agent.config import ResearchConfiguration

    monkeypatch.delenv("IMAGE_QA_MODEL", raising=False)
    cfg = ResearchConfiguration()
    assert cfg.image_qa_model == cfg.worker_model
    monkeypatch.setenv("IMAGE_QA_MODEL", "other-model")
    assert ResearchConfiguration().image_qa_model == "other-model"


def test_image_qa_knobs_ship_to_agent_engine():
    import deployment.deploy_agent as da

    for key in ("IMAGE_QA_ENABLED", "IMAGE_QA_MAX_RERENDERS", "IMAGE_QA_MODEL"):
        assert da.ENV_VAR_DICT[key] is not None
