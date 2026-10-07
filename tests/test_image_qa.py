"""Post-render image QA (creative_agent/image_qa.py) + generate_image's
inspect → targeted re-render flow.

The pure rule (``qa_failures`` / ``qa_failed_rules``), the expected-text
extraction and ``inspect_image``'s request are tested with a fake genai client;
the generate_image flow fakes both the image client and the QA call.
"""

import asyncio
import json

import pytest

from creative_agent import image_qa, image_tools
from creative_agent.image_qa import ImageQAResult
from tests._fakes import FakeToolContext, noop_async

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


# --- generate_image: inspect → targeted re-render ---


class _ImgPart:
    thought = False

    def __init__(self, data):
        self.inline_data = type("D", (), {"data": data, "mime_type": "image/png"})()


class _ImgResponse:
    def __init__(self, data):
        content = type("C", (), {"parts": [_ImgPart(data)]})()
        self.candidates = [type("Cand", (), {"content": content})()]


class _SeqImageModels:
    """Each render returns the next numbered image bytes (b'img1', b'img2' …)."""

    def __init__(self):
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _ImgResponse(f"img{len(self.calls)}".encode())


class _Flow:
    """Patched generate_image environment: fake renders, scripted QA verdicts,
    recorded uploads/artifacts."""

    def __init__(self, monkeypatch, verdicts, *, enabled=True, max_rerenders=1):
        self.models = _SeqImageModels()
        client = type("Client", (), {})()
        client.models = self.models
        monkeypatch.setattr(image_tools, "_get_genai_client", lambda: client)
        monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)
        self.uploads = []

        def fake_save(*, tool_context, image_bytes, filename):
            self.uploads.append((filename, image_bytes))
            return f"gs://b/{filename}"

        monkeypatch.setattr(image_tools, "_save_to_gcs", fake_save)
        monkeypatch.setattr(image_tools.config, "image_qa_enabled", enabled)
        monkeypatch.setattr(image_tools.config, "image_qa_max_rerenders", max_rerenders)
        monkeypatch.setattr(image_tools.config, "image_qa_model", "qa-model")
        monkeypatch.setattr(image_qa, "_get_qa_client", lambda: "qa-client")
        self.inspected = []
        verdicts = list(verdicts)

        def fake_inspect(
            image_bytes, mime, concept, *, brand, target_product, client, model
        ):
            self.inspected.append(
                (
                    image_bytes,
                    concept["concept_name"],
                    brand,
                    target_product,
                    client,
                    model,
                )
            )
            verdict = verdicts.pop(0)
            if isinstance(verdict, Exception):
                raise verdict
            return verdict

        monkeypatch.setattr(image_qa, "inspect_image", fake_inspect)
        self.artifacts = []

        class _Ctx(FakeToolContext):
            async def save_artifact(inner, filename, artifact):  # noqa: N805
                self.artifacts.append((filename, artifact.inline_data.data))

        self.ctx = _Ctx(
            {
                "gcs_folder": "f",
                "agent_output_dir": "d",
                "brand": "PRS",
                "target_product": "PRS SE guitar",
                "final_visual_concepts": {"visual_concepts": [dict(_CONCEPT)]},
            }
        )

    def run(self):
        return asyncio.run(image_tools.generate_image(self.ctx))

    @property
    def record(self):
        return self.ctx.state["generated_images"]["Jackpot"]


_KEY = image_tools.artifact_key_for("Jackpot")


def test_pass_first_time_one_render_one_check(monkeypatch):
    flow = _Flow(monkeypatch, [_result()])
    flow.run()
    assert len(flow.models.calls) == 1
    assert flow.inspected == [
        (b"img1", "Jackpot", "PRS", "PRS SE guitar", "qa-client", "qa-model")
    ]
    assert flow.record["attempts"] == 1
    assert flow.record["qa"]["passed"] is True
    assert flow.record["qa"]["failures"] == []
    assert flow.record["qa"]["product_visible"] is True
    assert "image_qa__issues" not in flow.ctx.state


def test_fail_then_pass_keeps_rerender(monkeypatch):
    flow = _Flow(
        monkeypatch,
        [_result(unrequested_logos=True, issues=["remove the swoosh"]), _result()],
    )
    flow.run()
    assert len(flow.models.calls) == 2
    second_prompt = flow.models.calls[1]["contents"]
    assert second_prompt == (
        _CONCEPT["image_generation_prompt"]
        + "\n\nCorrect these issues from the previous attempt: remove the swoosh"
    )
    # Same aspect ratio / image config on the re-render.
    first_cfg = flow.models.calls[0]["config"].image_config
    second_cfg = flow.models.calls[1]["config"].image_config
    assert first_cfg.aspect_ratio == second_cfg.aspect_ratio
    # Only the kept (second) image is uploaded and saved, once.
    assert flow.uploads == [(_KEY, b"img2")]
    assert flow.artifacts == [(_KEY, b"img2")]
    assert flow.record["attempts"] == 2
    assert flow.record["qa"]["passed"] is True
    assert "image_qa__issues" not in flow.ctx.state


def test_both_fail_keeps_fewer_failures_and_warns(monkeypatch):
    flow = _Flow(
        monkeypatch,
        [
            _result(unrequested_logos=True),
            _result(unrequested_logos=True, artifacts=True, product_visible=False),
        ],
    )
    flow.run()
    assert flow.uploads == [(_KEY, b"img1")]
    assert flow.record["attempts"] == 2
    assert flow.record["qa"]["passed"] is False
    assert flow.record["qa"]["failures"] == ["unrequested third-party logo"]
    assert flow.ctx.state["image_qa__issues"] == [
        "Jackpot: unrequested third-party logo"
    ]


def test_tie_keeps_latest_attempt(monkeypatch):
    flow = _Flow(
        monkeypatch, [_result(motif_visible=False), _result(gibberish_text=True)]
    )
    flow.run()
    assert flow.uploads == [(_KEY, b"img2")]
    assert flow.ctx.state["image_qa__issues"] == ["Jackpot: gibberish text"]


def test_rerender_budget_respected(monkeypatch):
    flow = _Flow(
        monkeypatch,
        [_result(artifacts=True)] * 3,
        max_rerenders=2,
    )
    flow.run()
    assert len(flow.models.calls) == 3
    assert flow.record["attempts"] == 3
    assert flow.uploads == [(_KEY, b"img3")]


def test_zero_rerenders_checks_but_never_rerenders(monkeypatch):
    flow = _Flow(monkeypatch, [_result(artifacts=True)], max_rerenders=0)
    flow.run()
    assert len(flow.models.calls) == 1
    assert flow.record["qa"]["passed"] is False
    assert flow.ctx.state["image_qa__issues"] == ["Jackpot: severe visual artifacts"]


def test_qa_exception_fails_open(monkeypatch):
    flow = _Flow(monkeypatch, [RuntimeError("vision down")])
    result = flow.run()
    assert result["status"] == "success"
    assert len(flow.models.calls) == 1
    assert flow.uploads == [(_KEY, b"img1")]
    assert flow.record["qa"] is None
    assert flow.ctx.state["image_qa__issues"] == ["QA unavailable for Jackpot"]


def test_qa_client_construction_failure_fails_open(monkeypatch):
    flow = _Flow(monkeypatch, [])

    def boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(image_qa, "_get_qa_client", boom)
    flow.run()
    assert flow.record["qa"] is None
    assert flow.ctx.state["image_qa__issues"] == ["QA unavailable for Jackpot"]


def test_rerender_qa_exception_keeps_inspected_first_attempt(monkeypatch):
    flow = _Flow(monkeypatch, [_result(artifacts=True), RuntimeError("down")])
    flow.run()
    assert flow.uploads == [(_KEY, b"img1")]
    assert flow.record["attempts"] == 2
    assert flow.record["qa"]["failures"] == ["severe visual artifacts"]


def test_rerender_exception_keeps_first_attempt(monkeypatch):
    flow = _Flow(monkeypatch, [_result(artifacts=True)])
    real = flow.models.generate_content

    def flaky(**kwargs):
        if flow.models.calls:
            flow.models.calls.append(kwargs)
            raise ValueError("bad request")
        return real(**kwargs)

    monkeypatch.setattr(flow.models, "generate_content", flaky)
    flow.run()
    assert flow.uploads == [(_KEY, b"img1")]
    assert flow.record["attempts"] == 2


def test_disabled_single_render_no_qa(monkeypatch):
    flow = _Flow(monkeypatch, [], enabled=False)
    flow.run()
    assert len(flow.models.calls) == 1
    assert flow.inspected == []
    assert flow.record == {
        "gcs_uri": f"gs://b/{_KEY}",
        "artifact_key": _KEY,
        "attempts": 1,
        "qa": None,
    }
    assert "image_qa__issues" not in flow.ctx.state


def test_brand_cue_missing_does_not_trigger_rerender(monkeypatch):
    flow = _Flow(monkeypatch, [_result(brand_cue_visible=False, issues=["no inlays"])])
    flow.run()
    assert len(flow.models.calls) == 1
    assert flow.record["qa"]["passed"] is True
    assert flow.record["qa"]["brand_cue_visible"] is False


def test_unrequested_logo_triggers_rerender(monkeypatch):
    flow = _Flow(monkeypatch, [_result(unrequested_logos=True), _result()])
    flow.run()
    assert len(flow.models.calls) == 2
    assert "unrequested third-party logo" in flow.models.calls[1]["contents"]


def test_unresolved_qa_issues_surface_as_degradation_warning(monkeypatch):
    from agent_common.observability import collect_degradation_warnings

    flow = _Flow(monkeypatch, [_result(unsafe=True), _result(unsafe=True)])
    flow.run()
    notes = collect_degradation_warnings(dict(flow.ctx.state))
    assert any(n.startswith("Image qa has unresolved issues: 1") for n in notes)
