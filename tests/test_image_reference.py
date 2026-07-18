"""generate_image assembles multimodal contents + a valid ImageConfig.

Covers the Phase-4 reference-image path (gs:// / http(s):// → a Part appended to
contents, with a text-only fallback) and the Phase-1 aspect-ratio config
(per-concept choice with a fallback to the configured default for bad values).
"""

import asyncio

from creative_agent import image_tools


async def _noop_async(*a, **k):
    return None


class MockState(dict):
    pass


class MockToolContext:
    def __init__(self):
        self.state = MockState()
        self.state["gcs_folder"] = "f"
        self.state["agent_output_dir"] = "d"

    async def save_artifact(self, *a, **k):
        return None


class _Part:
    class inline_data:
        data = b"\x89PNG"
        mime_type = "image/png"


class _Content:
    parts = [_Part()]


class _Candidate:
    content = _Content()


class _GoodResponse:
    candidates = [_Candidate()]


class _RecordingModels:
    """Records the (contents, config) of each generate_content call."""

    def __init__(self):
        self.calls = []

    def generate_content(self, *a, **k):
        self.calls.append(k)
        return _GoodResponse()


def _patch_client(monkeypatch):
    models = _RecordingModels()

    class _Client:
        pass

    client = _Client()
    client.models = models
    monkeypatch.setattr(image_tools, "_get_genai_client", lambda: client)
    monkeypatch.setattr(image_tools.asyncio, "sleep", _noop_async)
    monkeypatch.setattr(image_tools, "_save_to_gcs", lambda *a, **k: "gs://b/c.png")
    return models


def test_no_reference_uses_bare_string_contents(monkeypatch):
    """Without reference_image_uri, contents is the bare prompt string."""
    models = _patch_client(monkeypatch)
    ctx = MockToolContext()
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [
            {"image_generation_prompt": "a flat cartoon", "concept_name": "c"}
        ]
    }

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    assert len(models.calls) == 1
    assert models.calls[0]["contents"] == "a flat cartoon"


def test_gs_reference_appends_part_to_contents(monkeypatch):
    """A gs:// reference_image_uri → contents is [prompt, Part]; _download_blob
    is called with the parsed bucket/object."""
    models = _patch_client(monkeypatch)
    dl = {}

    def fake_download(bucket, obj):
        dl["bucket"], dl["obj"] = bucket, obj
        return b"\x89PNGREF"

    monkeypatch.setattr(image_tools, "_download_blob", fake_download)

    ctx = MockToolContext()
    ctx.state["reference_image_uri"] = "gs://my-bucket/products/guitar.png"
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [
            {"image_generation_prompt": "a studio shot", "concept_name": "c"}
        ]
    }

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    assert dl == {"bucket": "my-bucket", "obj": "products/guitar.png"}
    contents = models.calls[0]["contents"]
    assert isinstance(contents, list) and len(contents) == 2
    assert contents[0] == "a studio shot"
    # The second element is a genai Part built from the reference bytes.
    assert getattr(contents[1], "inline_data", None) is not None


def test_reference_fetch_failure_falls_back_to_text_only(monkeypatch):
    """If the reference fetch raises, generation proceeds text-only (no crash)."""
    models = _patch_client(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(image_tools, "_download_blob", boom)

    ctx = MockToolContext()
    ctx.state["reference_image_uri"] = "gs://my-bucket/x.png"
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [{"image_generation_prompt": "a scene", "concept_name": "c"}]
    }

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    assert models.calls[0]["contents"] == "a scene"


def test_bad_aspect_ratio_falls_back_to_default(monkeypatch):
    """An aspect_ratio outside the allowed set falls back to the configured
    default; a valid choice is passed through."""
    models = _patch_client(monkeypatch)
    ctx = MockToolContext()
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [
            {
                "image_generation_prompt": "p1",
                "concept_name": "c1",
                "aspect_ratio": "banana",
            },
            {
                "image_generation_prompt": "p2",
                "concept_name": "c2",
                "aspect_ratio": "1:1",
            },
        ]
    }

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    ar0 = models.calls[0]["config"].image_config.aspect_ratio
    ar1 = models.calls[1]["config"].image_config.aspect_ratio
    assert ar0 == image_tools.config.image_aspect_ratio_default
    assert ar1 == "1:1"


# --- Deterministic state-level aspect-ratio override (visual_aspect_ratio) ---
class TestResolveAspectRatio:
    """Pure resolution: state override wins, else per-concept, else default."""

    ALLOWED = ("9:16", "1:1", "4:5", "16:9", "3:4")
    DEFAULT = "9:16"

    def test_valid_override_wins_over_per_concept(self):
        assert (
            image_tools._resolve_aspect_ratio(
                {"aspect_ratio": "3:4"}, "1:1", self.ALLOWED, self.DEFAULT
            )
            == "1:1"
        )

    def test_no_override_uses_per_concept(self):
        assert (
            image_tools._resolve_aspect_ratio(
                {"aspect_ratio": "16:9"}, "", self.ALLOWED, self.DEFAULT
            )
            == "16:9"
        )

    def test_invalid_override_ignored_falls_to_per_concept(self):
        assert (
            image_tools._resolve_aspect_ratio(
                {"aspect_ratio": "1:1"}, "banana", self.ALLOWED, self.DEFAULT
            )
            == "1:1"
        )

    def test_bad_per_concept_falls_to_default(self):
        assert (
            image_tools._resolve_aspect_ratio(
                {"aspect_ratio": "nope"}, "", self.ALLOWED, self.DEFAULT
            )
            == self.DEFAULT
        )

    def test_missing_everything_uses_default(self):
        assert (
            image_tools._resolve_aspect_ratio({}, "", self.ALLOWED, self.DEFAULT)
            == self.DEFAULT
        )


def test_new_aspect_ratios_are_allowed():
    """4:5 and 16:9 were added to the allowed set."""
    allowed = image_tools.config.image_aspect_ratios_allowed
    assert "4:5" in allowed
    assert "16:9" in allowed


def test_state_aspect_ratio_override_applies_to_all_concepts(monkeypatch):
    """A valid state['visual_aspect_ratio'] overrides every concept's own ratio."""
    models = _patch_client(monkeypatch)
    ctx = MockToolContext()
    ctx.state["visual_aspect_ratio"] = "16:9"
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [
            {
                "image_generation_prompt": "p1",
                "concept_name": "c1",
                "aspect_ratio": "9:16",
            },
            {
                "image_generation_prompt": "p2",
                "concept_name": "c2",
                "aspect_ratio": "3:4",
            },
        ]
    }

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    assert models.calls[0]["config"].image_config.aspect_ratio == "16:9"
    assert models.calls[1]["config"].image_config.aspect_ratio == "16:9"


def test_empty_state_aspect_ratio_preserves_per_concept(monkeypatch):
    """An empty/unset override leaves the per-concept diversity intact."""
    models = _patch_client(monkeypatch)
    ctx = MockToolContext()
    ctx.state["visual_aspect_ratio"] = ""
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [
            {
                "image_generation_prompt": "p1",
                "concept_name": "c1",
                "aspect_ratio": "1:1",
            },
            {
                "image_generation_prompt": "p2",
                "concept_name": "c2",
                "aspect_ratio": "4:5",
            },
        ]
    }

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    assert models.calls[0]["config"].image_config.aspect_ratio == "1:1"
    assert models.calls[1]["config"].image_config.aspect_ratio == "4:5"


# --- Reference-image role labeling (reference_image_role) ---
class TestRolePrefixedPrompt:
    """Pure: role → an appended instruction about the reference image."""

    def test_product_role(self):
        out = image_tools._role_prefixed_prompt("a studio shot", "product")
        assert "a studio shot" in out
        assert "product shown in the reference image" in out

    def test_logo_role(self):
        out = image_tools._role_prefixed_prompt("a scene", "logo")
        assert "brand logo shown in the reference image" in out

    def test_style_role(self):
        out = image_tools._role_prefixed_prompt("a scene", "style")
        assert "visual style" in out and "reference image" in out

    def test_no_role_unchanged(self):
        assert image_tools._role_prefixed_prompt("a scene", "") == "a scene"

    def test_unknown_role_unchanged(self):
        assert image_tools._role_prefixed_prompt("a scene", "banana") == "a scene"


def test_reference_role_adds_instruction_to_prompt(monkeypatch):
    """With a reference present + a role set, the prompt text gains the role
    instruction (contents[0]); the reference Part is still appended."""
    models = _patch_client(monkeypatch)
    monkeypatch.setattr(image_tools, "_download_blob", lambda *a, **k: b"\x89PNGREF")

    ctx = MockToolContext()
    ctx.state["reference_image_uri"] = "gs://b/logo.png"
    ctx.state["reference_image_role"] = "logo"
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [{"image_generation_prompt": "a scene", "concept_name": "c"}]
    }

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    contents = models.calls[0]["contents"]
    assert isinstance(contents, list) and len(contents) == 2
    assert "a scene" in contents[0]
    assert "brand logo shown in the reference image" in contents[0]


def test_reference_role_ignored_without_reference(monkeypatch):
    """A role with no reference image leaves the prompt unchanged (text-only)."""
    models = _patch_client(monkeypatch)
    ctx = MockToolContext()
    ctx.state["reference_image_role"] = "product"
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [{"image_generation_prompt": "a scene", "concept_name": "c"}]
    }

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    assert models.calls[0]["contents"] == "a scene"
