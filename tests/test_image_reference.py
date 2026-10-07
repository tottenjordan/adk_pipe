"""generate_image assembles multimodal contents + a valid ImageConfig.

Covers the Phase-4 reference-image path (gs:// / http(s):// → a Part appended to
contents, with a text-only fallback) and the Phase-1 aspect-ratio config
(per-concept choice with a fallback to the configured default for bad values).
"""

import asyncio

from creative_agent import image_tools
from tests._fakes import FakeToolContext, noop_async

_STATE = {"gcs_folder": "f", "agent_output_dir": "d"}


def _ctx() -> FakeToolContext:
    return FakeToolContext(_STATE)


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
    monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)
    monkeypatch.setattr(image_tools, "_save_to_gcs", lambda *a, **k: "gs://b/c.png")
    return models


def test_no_reference_uses_bare_string_contents(monkeypatch):
    """Without reference_image_uri, contents is the bare prompt string."""
    models = _patch_client(monkeypatch)
    ctx = _ctx()
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

    ctx = _ctx()
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
    # Legacy reference with no role defaults to `product`; the prompt gains the
    # numbered reference block after the concept prompt.
    assert contents[0].startswith("a studio shot\n\n")
    assert "Reference image 1 (product)" in contents[0]
    # The second element is a genai Part built from the reference bytes.
    assert getattr(contents[1], "inline_data", None) is not None


def test_reference_fetch_failure_falls_back_to_text_only(monkeypatch):
    """If the reference fetch raises, generation proceeds text-only (no crash)."""
    models = _patch_client(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(image_tools, "_download_blob", boom)

    ctx = _ctx()
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
    ctx = _ctx()
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
    ctx = _ctx()
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
    ctx = _ctx()
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


# --- Reference-image prompt block (numbered roles + ignore-text) ---
class TestReferencePrompt:
    """Pure: (prompt, roles) → the prompt plus a numbered reference block."""

    def test_no_roles_unchanged(self):
        assert image_tools._reference_prompt("a scene", []) == "a scene"

    def test_numbered_roles_in_order(self):
        out = image_tools._reference_prompt("a scene", ["product", "style"])
        assert out.startswith("a scene\n\n")
        i1 = out.index("Reference image 1 (product):")
        i2 = out.index("Reference image 2 (style):")
        assert i1 < i2

    def test_role_wording(self):
        out = image_tools._reference_prompt("s", ["product", "logo", "style"])
        assert "shape, colour, label and proportions" in out
        assert "small, legible and undistorted" in out
        assert "palette, texture and lighting" in out
        assert "not its content" in out

    def test_ignore_text_line_once(self):
        out = image_tools._reference_prompt("s", ["product", "style", "logo"])
        line = image_tools.REFERENCE_IGNORE_TEXT_LINE
        assert "ignore any text, captions or watermarks" in line.lower()
        assert out.count(line) == 1


def test_reference_role_adds_instruction_to_prompt(monkeypatch):
    """With a reference present + a role set, the prompt text gains the role
    instruction (contents[0]); the reference Part is still appended."""
    models = _patch_client(monkeypatch)
    monkeypatch.setattr(image_tools, "_download_blob", lambda *a, **k: b"\x89PNGREF")

    ctx = _ctx()
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
    assert "Reference image 1 (logo)" in contents[0]
    assert image_tools.REFERENCE_IGNORE_TEXT_LINE in contents[0]


def test_reference_role_ignored_without_reference(monkeypatch):
    """A role with no reference image leaves the prompt unchanged (text-only)."""
    models = _patch_client(monkeypatch)
    ctx = _ctx()
    ctx.state["reference_image_role"] = "product"
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [{"image_generation_prompt": "a scene", "concept_name": "c"}]
    }

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    assert models.calls[0]["contents"] == "a scene"


class _ImgPart:
    """A content part carrying inline image bytes; ``thought`` marks a draft."""

    def __init__(self, data: bytes, *, thought: bool = False):
        self.thought = thought

        class _Inline:
            mime_type = "image/png"

        _Inline.data = data
        self.inline_data = _Inline


class _TextPart:
    inline_data = None
    thought = False
    text = "here is your image"


def test_final_image_part_skips_thought_draft():
    """nano-banana-2.1 returns a thought image before the final one: keep the final."""
    parts = [_ImgPart(b"draft", thought=True), _TextPart(), _ImgPart(b"final")]
    assert image_tools._final_image_part(parts).inline_data.data == b"final"


def test_final_image_part_falls_back_to_last_image():
    parts = [_ImgPart(b"a", thought=True), _ImgPart(b"b", thought=True)]
    assert image_tools._final_image_part(parts).inline_data.data == b"b"
    assert image_tools._final_image_part([_TextPart()]) is None


def test_generate_image_saves_final_not_thought_image(monkeypatch):
    """The bytes uploaded to GCS are the final image, not the thought draft."""

    class _Resp:
        class _C:
            class content:
                parts = [_ImgPart(b"draft", thought=True), _ImgPart(b"final")]

        candidates = [_C()]

    class _Models:
        def generate_content(self, *a, **k):
            return _Resp()

    class _Client:
        models = _Models()

    saved = []
    monkeypatch.setattr(image_tools, "_get_genai_client", lambda: _Client())
    monkeypatch.setattr(image_tools.asyncio, "sleep", noop_async)
    monkeypatch.setattr(
        image_tools,
        "_save_to_gcs",
        lambda **k: saved.append(k["image_bytes"]) or "gs://b/c.png",
    )
    ctx = _ctx()
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [{"image_generation_prompt": "p", "concept_name": "c"}]
    }
    asyncio.run(image_tools.generate_image(ctx))
    assert saved and saved[0] == b"final"


# --- Multiple reference images (reference_images) ---
class TestResolveReferences:
    """Pure: state → ordered, deduped, capped (uri, role) pairs."""

    def test_empty_state(self):
        assert image_tools.resolve_references({}) == []

    def test_list_in_order(self):
        state = {
            "reference_images": [
                {"uri": "gs://b/p.png", "role": "product"},
                {"uri": "https://x/s.jpg", "role": "style"},
            ]
        }
        assert image_tools.resolve_references(state) == [
            ("gs://b/p.png", "product"),
            ("https://x/s.jpg", "style"),
        ]

    def test_json_string(self):
        state = {"reference_images": '[{"uri": "gs://b/l.png", "role": "logo"}]'}
        assert image_tools.resolve_references(state) == [("gs://b/l.png", "logo")]

    def test_bad_json_string_ignored(self):
        assert image_tools.resolve_references({"reference_images": "[oops"}) == []

    def test_invalid_entries_and_roles_skipped(self):
        state = {
            "reference_images": [
                {"uri": "gs://b/a.png", "role": "banana"},
                {"role": "product"},
                {"uri": "   ", "role": "product"},
                "gs://b/bare.png",
                None,
                {"uri": 5, "role": "logo"},
                {"uri": "gs://b/ok.png", "role": " Style "},
            ]
        }
        assert image_tools.resolve_references(state) == [("gs://b/ok.png", "style")]

    def test_missing_role_defaults_to_product(self):
        state = {"reference_images": [{"uri": "gs://b/a.png"}]}
        assert image_tools.resolve_references(state) == [("gs://b/a.png", "product")]

    def test_legacy_folded_in_first(self):
        state = {
            "reference_image_uri": "gs://b/legacy.png",
            "reference_image_role": "logo",
            "reference_images": [{"uri": "gs://b/s.png", "role": "style"}],
        }
        assert image_tools.resolve_references(state) == [
            ("gs://b/legacy.png", "logo"),
            ("gs://b/s.png", "style"),
        ]

    def test_legacy_without_role_is_product(self):
        state = {"reference_image_uri": "gs://b/legacy.png", "reference_image_role": ""}
        assert image_tools.resolve_references(state) == [
            ("gs://b/legacy.png", "product")
        ]

    def test_legacy_duplicate_not_repeated(self):
        """The frontend sends row 1 both ways for one release: one entry."""
        state = {
            "reference_image_uri": "gs://b/p.png",
            "reference_image_role": "product",
            "reference_images": [
                {"uri": "gs://b/p.png", "role": "product"},
                {"uri": "gs://b/s.png", "role": "style"},
            ],
        }
        assert image_tools.resolve_references(state) == [
            ("gs://b/p.png", "product"),
            ("gs://b/s.png", "style"),
        ]

    def test_dedupe_by_uri(self):
        state = {
            "reference_images": [
                {"uri": "gs://b/p.png", "role": "product"},
                {"uri": " gs://b/p.png ", "role": "style"},
            ]
        }
        assert image_tools.resolve_references(state) == [("gs://b/p.png", "product")]

    def test_capped(self):
        state = {
            "reference_image_uri": "gs://b/0.png",
            "reference_images": [
                {"uri": f"gs://b/{i}.png", "role": "style"} for i in range(1, 6)
            ],
        }
        refs = image_tools.resolve_references(state)
        assert len(refs) == image_tools.MAX_REFERENCE_IMAGES == 3
        assert refs[0] == ("gs://b/0.png", "product")


def test_reference_roles_summary():
    state = {
        "reference_images": [
            {"uri": "gs://b/p.png", "role": "product"},
            {"uri": "gs://b/s.png", "role": "style"},
        ]
    }
    assert image_tools.reference_roles_summary(state) == "product, style"
    assert image_tools.reference_roles_summary({}) == ""


def _multi_ctx(refs):
    ctx = _ctx()
    ctx.state["reference_images"] = refs
    ctx.state["final_visual_concepts"] = {
        "visual_concepts": [{"image_generation_prompt": "a scene", "concept_name": "c"}]
    }
    return ctx


def test_multiple_references_appended_in_order(monkeypatch):
    models = _patch_client(monkeypatch)
    monkeypatch.setattr(image_tools, "_download_blob", lambda bucket, obj: obj.encode())
    ctx = _multi_ctx(
        [
            {"uri": "gs://b/product.png", "role": "product"},
            {"uri": "gs://b/logo.png", "role": "logo"},
            {"uri": "gs://b/style.png", "role": "style"},
        ]
    )

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    contents = models.calls[0]["contents"]
    assert len(contents) == 4
    assert [c.inline_data.data for c in contents[1:]] == [
        b"product.png",
        b"logo.png",
        b"style.png",
    ]
    text = contents[0]
    assert text.startswith("a scene\n\n")
    assert (
        text.index("Reference image 1 (product)")
        < text.index("Reference image 2 (logo)")
        < text.index("Reference image 3 (style)")
    )
    assert text.count(image_tools.REFERENCE_IGNORE_TEXT_LINE) == 1


def test_references_fetched_once_for_all_concepts(monkeypatch):
    models = _patch_client(monkeypatch)
    fetched = []

    def fake_download(bucket, obj):
        fetched.append(obj)
        return b"x"

    monkeypatch.setattr(image_tools, "_download_blob", fake_download)
    ctx = _multi_ctx(
        [
            {"uri": "gs://b/p.png", "role": "product"},
            {"uri": "gs://b/s.png", "role": "style"},
        ]
    )
    ctx.state["final_visual_concepts"]["visual_concepts"].append(
        {"image_generation_prompt": "another", "concept_name": "d"}
    )

    asyncio.run(image_tools.generate_image(ctx))
    assert sorted(fetched) == ["p.png", "s.png"]
    assert len(models.calls) == 2
    assert all(len(call["contents"]) == 3 for call in models.calls)


def test_one_failed_fetch_skips_only_that_reference(monkeypatch):
    models = _patch_client(monkeypatch)

    def fake_download(bucket, obj):
        if obj == "logo.png":
            raise RuntimeError("gcs down")
        return obj.encode()

    monkeypatch.setattr(image_tools, "_download_blob", fake_download)
    ctx = _multi_ctx(
        [
            {"uri": "gs://b/product.png", "role": "product"},
            {"uri": "gs://b/logo.png", "role": "logo"},
            {"uri": "gs://b/style.png", "role": "style"},
        ]
    )

    result = asyncio.run(image_tools.generate_image(ctx))
    assert result["status"] == "success"
    contents = models.calls[0]["contents"]
    assert [c.inline_data.data for c in contents[1:]] == [
        b"product.png",
        b"style.png",
    ]
    # Renumbered over the references actually attached.
    assert "Reference image 1 (product)" in contents[0]
    assert "Reference image 2 (style)" in contents[0]
    assert "(logo)" not in contents[0]


def test_all_fetches_failed_is_text_only(monkeypatch):
    models = _patch_client(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(image_tools, "_download_blob", boom)
    ctx = _multi_ctx([{"uri": "gs://b/p.png", "role": "product"}])

    asyncio.run(image_tools.generate_image(ctx))
    assert models.calls[0]["contents"] == "a scene"
