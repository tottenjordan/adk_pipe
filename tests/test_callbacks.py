"""Tests for callback functions (citation replacement, state init, rate limiting)."""

import re
import time
import types as pytypes

# --- Citation replacement regex ---
# Extracted from creative_agent/callbacks.py citation_replacement_callback
CITE_PATTERN = r'<cite\s+source\s*=\s*["\']?\s*(src-\d+)\s*["\']?\s*/>'


class TestCitationRegex:
    def test_matches_standard_cite_tag(self):
        text = 'This is a claim.<cite source="src-1" />'
        matches = re.findall(CITE_PATTERN, text)
        assert matches == ["src-1"]

    def test_matches_single_quoted(self):
        text = "A claim.<cite source='src-42' />"
        matches = re.findall(CITE_PATTERN, text)
        assert matches == ["src-42"]

    def test_matches_no_quotes(self):
        text = "A claim.<cite source=src-7 />"
        matches = re.findall(CITE_PATTERN, text)
        assert matches == ["src-7"]

    def test_matches_multiple_tags(self):
        text = 'Claim one.<cite source="src-1" /> Claim two.<cite source="src-2" />'
        matches = re.findall(CITE_PATTERN, text)
        assert matches == ["src-1", "src-2"]

    def test_no_match_for_invalid_tag(self):
        text = "<cite>not valid</cite>"
        matches = re.findall(CITE_PATTERN, text)
        assert matches == []

    def test_replacement_with_sources(self):
        sources = {
            "src-1": {
                "title": "Guitar World",
                "url": "https://guitarworld.com/article",
            },
            "src-2": {
                "title": "Rolling Stone",
                "url": "https://rollingstone.com/review",
            },
        }

        def tag_replacer(match: re.Match) -> str:
            short_id = match.group(1)
            source_info = sources.get(short_id)
            if not source_info:
                return ""
            display_text = source_info.get("title", short_id)
            return f" [{display_text}]({source_info['url']})"

        text = (
            'Great tone.<cite source="src-1" /> Critics agree.<cite source="src-2" />'
        )
        result = re.sub(CITE_PATTERN, tag_replacer, text)
        assert "[Guitar World](https://guitarworld.com/article)" in result
        assert "[Rolling Stone](https://rollingstone.com/review)" in result

    def test_replacement_missing_source_removed(self):
        sources = {}

        def tag_replacer(match: re.Match) -> str:
            short_id = match.group(1)
            source_info = sources.get(short_id)
            if not source_info:
                return ""
            return f" [{source_info['title']}]({source_info['url']})"

        text = 'A claim.<cite source="src-99" />'
        result = re.sub(CITE_PATTERN, tag_replacer, text)
        assert result == "A claim."

    def test_punctuation_spacing_fix(self):
        text = "Some text . More text , and more ;"
        result = re.sub(r"\s+([.,;:])", r"\1", text)
        assert result == "Some text. More text, and more;"


# --- State initialization ---
class TestSetInitialStates:
    def test_sets_gcs_fields_on_empty_target(self):
        from creative_agent.callbacks import _set_initial_states
        from creative_agent.config import config

        target = {}
        source = {"brand": "TestBrand", "target_product": "TestProduct"}
        _set_initial_states(source, target)

        assert target[config.state_init] is True
        assert target["gcs_bucket"] == config.GCS_BUCKET
        assert target["gcs_bucket_name"] == config.GCS_BUCKET_NAME
        assert target["agent_output_dir"] == "creative_output"
        assert "gcs_folder" in target
        assert target["brand"] == "TestBrand"
        assert target["target_product"] == "TestProduct"

    def test_does_not_overwrite_existing_init(self):
        from creative_agent.callbacks import _set_initial_states
        from creative_agent.config import config

        target = {config.state_init: True, "gcs_folder": "existing_folder"}
        source = {"brand": "NewBrand"}
        _set_initial_states(source, target)

        # Should not overwrite since state_init already present
        assert target["gcs_folder"] == "existing_folder"
        assert "brand" not in target  # source not applied

    def test_trend_scout_sets_trawler_output(self):
        from trend_scout.callbacks import _set_initial_states

        target = {}
        source = {"brand": "PRS"}
        _set_initial_states(source, target)

        assert target["agent_output_dir"] == "trawler_output"
        assert "gcs_bucket_name" not in target
        assert target["brand"] == "PRS"

    # --- Optional visual-intent keys (image-intent-capture) ---
    _INTENT_KEYS = (
        "visual_intent",
        "brand_colors",
        "visual_style_preference",
        "visual_avoid",
        "visual_aspect_ratio",
        "reference_image_role",
    )

    def test_intent_keys_default_to_empty_when_unseeded(self):
        from creative_agent.callbacks import _set_initial_states

        target = {}
        source = {"brand": "TestBrand"}
        _set_initial_states(source, target)

        for key in self._INTENT_KEYS:
            assert target[key] == "", f"{key} should default to empty string"

    def test_intent_keys_survive_caller_seeding(self):
        """Caller-seeded intent values must NOT be blanked by state init.

        These flow via createSession initialState, so they are already present
        on `target` before _set_initial_states runs. setdefault must preserve
        them (they are deliberately absent from `source`).
        """
        from creative_agent.callbacks import _set_initial_states

        target = {
            "visual_intent": "moody film noir",
            "brand_colors": "#1a1a1a and gold",
            "visual_style_preference": "cinematic",
            "visual_avoid": "clutter",
            "visual_aspect_ratio": "1:1",
            "reference_image_role": "product",
        }
        source = {"brand": "TestBrand"}
        _set_initial_states(source, target)

        assert target["visual_intent"] == "moody film noir"
        assert target["brand_colors"] == "#1a1a1a and gold"
        assert target["visual_style_preference"] == "cinematic"
        assert target["visual_avoid"] == "clutter"
        assert target["visual_aspect_ratio"] == "1:1"
        assert target["reference_image_role"] == "product"

    # --- Opt-in rating learning (learn_from_ratings) ---
    def test_learn_from_ratings_defaults_off(self):
        from creative_agent.callbacks import _set_initial_states

        target = {}
        _set_initial_states({"brand": "TestBrand"}, target)
        assert target["learn_from_ratings"] is False

    def test_learn_from_ratings_seed_is_not_clobbered(self):
        from creative_agent.callbacks import _set_initial_states

        target = {"learn_from_ratings": True}
        _set_initial_states({"brand": "TestBrand"}, target)
        assert target["learn_from_ratings"] is True

    # --- Multiple reference images (reference_images → reference_roles) ---
    def test_reference_keys_default_when_unseeded(self):
        from creative_agent.callbacks import _set_initial_states

        target = {}
        _set_initial_states({"brand": "TestBrand"}, target)

        assert target["reference_images"] == []
        assert target["reference_roles"] == ""

    def test_reference_roles_derived_from_seeded_references(self):
        from creative_agent.callbacks import _set_initial_states

        refs = [
            {"uri": "gs://b/p.png", "role": "product"},
            {"uri": "gs://b/s.png", "role": "style"},
        ]
        target = {"reference_images": refs}
        _set_initial_states({"brand": "TestBrand"}, target)

        assert target["reference_images"] == refs  # not clobbered
        assert target["reference_roles"] == "product, style"

    def test_reference_roles_include_legacy_reference(self):
        from creative_agent.callbacks import _set_initial_states

        target = {
            "reference_image_uri": "gs://b/logo.png",
            "reference_image_role": "logo",
        }
        _set_initial_states({"brand": "TestBrand"}, target)

        assert target["reference_roles"] == "logo"

    # --- Person reference (person_reference → person_reference_available) ---
    def test_person_reference_defaults_when_unseeded(self):
        from creative_agent.callbacks import _set_initial_states

        target = {}
        _set_initial_states({"brand": "TestBrand"}, target)
        assert target["person_reference"] == {}
        assert target["person_reference_available"] == ""

    def test_person_reference_available_derived_from_seed(self):
        from creative_agent.callbacks import _set_initial_states

        ref = {"uri": "gs://b/person-refs/a-1/me.jpg", "consent_id": "c1234567"}
        target = {"person_reference": ref}
        _set_initial_states({"brand": "TestBrand"}, target)
        assert target["person_reference"] == ref  # not clobbered
        assert target["person_reference_available"] == "yes"

    def test_person_reference_outside_prefix_is_not_available(self):
        from creative_agent.callbacks import _set_initial_states

        for ref in (
            {"uri": "gs://b/other/me.jpg", "consent_id": "c1234567"},
            {"uri": "https://x.com/person-refs/me.jpg", "consent_id": "c1234567"},
            {"uri": "", "consent_id": "c1234567"},
            "gs://b/person-refs/a-1/me.jpg",
        ):
            target = {"person_reference": ref}
            _set_initial_states({"brand": "TestBrand"}, target)
            assert target["person_reference_available"] == ""

    # --- Core campaign fields (deterministic inputs via createSession state) ---
    _CAMPAIGN_KEYS = (
        "brand",
        "target_product",
        "target_audience",
        "key_selling_points",
        "target_search_trends",
    )

    @staticmethod
    def _run_load_session_state(state: dict) -> dict:
        from types import SimpleNamespace

        from creative_agent.callbacks import load_session_state

        ctx = SimpleNamespace(
            state=state,
            agent_name="root_agent",
            invocation_id="inv-1",
            session=SimpleNamespace(id="sess-1"),
            user_id="u-1",
        )
        load_session_state(ctx)  # ty: ignore[invalid-argument-type]
        return state

    def test_campaign_keys_default_to_empty_when_unseeded(self):
        state = self._run_load_session_state({})
        for key in self._CAMPAIGN_KEYS:
            assert state[key] == "", f"{key} should default to empty string"

    def test_campaign_keys_survive_caller_seeding(self):
        """createSession-seeded campaign fields must NOT be blanked by init."""
        seeded = {
            "brand": "PRS Guitars",
            "target_product": "SE CE24",
            "target_audience": "gigging guitarists",
            "key_selling_points": "versatile, affordable",
            "target_search_trends": "powerball",
        }
        state = self._run_load_session_state(dict(seeded))
        for key, value in seeded.items():
            assert state[key] == value

    def test_campaign_seeding_runs_once(self):
        from creative_agent.config import config

        state = self._run_load_session_state({"brand": "PRS"})
        folder = state["gcs_folder"]
        assert state[config.state_init] is True
        # A later turn (state already initialised; memorize changed a value)
        # must neither re-seed nor reset campaign fields.
        state["brand"] = "Memorized"
        self._run_load_session_state(state)
        assert state["gcs_folder"] == folder
        assert state["brand"] == "Memorized"

    def test_style_shortlist_seeded_once(self):
        from creative_agent.callbacks import _set_initial_states
        from creative_agent.style_shortlist import STYLE_GROUPS

        target: dict = {}
        _set_initial_states({"brand": "TestBrand"}, target)
        shortlist = target["style_shortlist"]
        families = [f for fs in STYLE_GROUPS.values() for f in fs]
        assert sum(f in shortlist for f in families) == 6

        # A second call on the already-initialised state must not re-roll it.
        _set_initial_states({"brand": "TestBrand"}, target)
        assert target["style_shortlist"] == shortlist

    def test_style_shortlist_not_clobbered(self):
        from creative_agent.callbacks import _set_initial_states

        target: dict = {"style_shortlist": "Comic panel; Diecut sticker"}
        _set_initial_states({"brand": "TestBrand"}, target)
        assert target["style_shortlist"] == "Comic panel; Diecut sticker"


# --- Rate limit callback ---
class TestRateLimitLogic:
    """Tests the rate limiting logic without importing CallbackContext."""

    def test_first_call_initializes_state(self):
        """First call should set timer_start and request_count=1."""
        state = {}
        now = time.time()

        # Simulate first call logic
        if "timer_start" not in state:
            state["timer_start"] = now
            state["request_count"] = 1

        assert state["request_count"] == 1
        assert state["timer_start"] == now

    def test_subsequent_calls_increment_count(self):
        state = {"timer_start": time.time(), "request_count": 5}
        state["request_count"] = state["request_count"] + 1
        assert state["request_count"] == 6

    def test_quota_exceeded_resets_count(self):
        rpm_quota = 10
        now = time.time()
        state = {
            "timer_start": now - 30,  # 30 seconds ago
            "request_count": rpm_quota,
        }

        request_count = state["request_count"] + 1
        if request_count > rpm_quota:
            state["timer_start"] = now
            state["request_count"] = 1

        assert state["request_count"] == 1

    def test_under_quota_keeps_counting(self):
        rpm_quota = 1000
        state = {"timer_start": time.time(), "request_count": 50}

        request_count = state["request_count"] + 1
        if request_count > rpm_quota:
            state["request_count"] = 1
        else:
            state["request_count"] = request_count

        assert state["request_count"] == 51


# --- Force-image-tool-call callback (issue #116) ---
class TestForceImageToolCall:
    """`force_image_tool_call` deterministically constrains the visual_generator
    turn to emit the `generate_image` function call (tool_config mode=ANY),
    eliminating the intermittent MALFORMED_FUNCTION_CALL empty-gallery flake —
    but ONLY while the image step has not yet succeeded (gated on
    `_images_generated`), so a resilient re-run after success stays a no-op.
    """

    @staticmethod
    def _fake_request():
        from google.adk.models.llm_request import LlmRequest

        return LlmRequest()

    @staticmethod
    def _ctx(state):
        return pytypes.SimpleNamespace(state=state)

    def test_forces_tool_call_when_images_not_generated(self):
        from google.genai.types import FunctionCallingConfigMode

        from creative_agent.callbacks import force_image_tool_call

        req = self._fake_request()
        result = force_image_tool_call(self._ctx({}), req)

        assert result is None  # callbacks must not short-circuit the model call
        cfg = req.config.tool_config.function_calling_config
        assert cfg.mode == FunctionCallingConfigMode.ANY
        assert cfg.allowed_function_names == ["generate_image"]

    def test_noop_once_images_generated(self):
        from creative_agent.callbacks import force_image_tool_call

        req = self._fake_request()
        result = force_image_tool_call(self._ctx({"_images_generated": True}), req)

        assert result is None
        # Already succeeded → must NOT re-force the tool (idempotent re-run).
        assert req.config.tool_config is None


class TestSkipReviserWithoutNotes:
    """interactive_creative's visual_concept_reviser before_agent_callback: skip the
    model unless there are both revision notes and finalized concepts, so merged
    checkpoint-3 edits are never paraphrased away. The skip reply must validate
    against the reviser's output_schema (ADK validates it for output_key/AgentTool).
    """

    _CONCEPTS = {
        "visual_concepts": [
            {
                "ad_copy_id": 1,
                "concept_name": "Edited",
                "trend": "t",
                "trend_reference": "r",
                "markets_product": "m",
                "audience_appeal": "a",
                "selection_rationale": "s",
                "headline": "h",
                "social_caption": "c",
                "call_to_action": "cta",
                "concept_summary": "sum",
                "visual_style": "diecut sticker",
                "aspect_ratio": "1:1",
                "trend_motif": "a trend motif",
                "brand_cue": "the brand's red logo",
                "angle_id": "A1",
                "image_generation_prompt": "A diecut sticker of a user-edited prompt",
            }
        ]
    }

    @staticmethod
    def _ctx(state):
        return pytypes.SimpleNamespace(state=state)

    @staticmethod
    def _reply_as_concepts(content):
        from creative_agent.schemas import VisualConceptFinalList

        assert content is not None
        text = "".join(p.text for p in content.parts)
        return VisualConceptFinalList.model_validate_json(text).model_dump(
            exclude_none=True
        )

    def test_skips_and_echoes_concepts_when_notes_empty(self):
        from interactive_creative.callbacks import skip_reviser_without_notes

        for notes in (None, "", "   \n"):
            state = {"final_visual_concepts": self._CONCEPTS}
            if notes is not None:
                state["visual_revision_notes"] = notes
            content = skip_reviser_without_notes(self._ctx(state))
            assert self._reply_as_concepts(content) == self._CONCEPTS

    def test_skips_with_empty_list_when_concepts_missing(self):
        from interactive_creative.callbacks import skip_reviser_without_notes

        for concepts in (None, {}, {"visual_concepts": []}):
            state = {"visual_revision_notes": "Concept 0: make it blue"}
            if concepts is not None:
                state["final_visual_concepts"] = concepts
            content = skip_reviser_without_notes(self._ctx(state))
            assert self._reply_as_concepts(content) == {"visual_concepts": []}

    def test_runs_model_when_notes_and_concepts_present(self):
        from interactive_creative.callbacks import skip_reviser_without_notes

        state = {
            "final_visual_concepts": self._CONCEPTS,
            "visual_revision_notes": "Concept 0 (Edited): make it blue",
        }
        assert skip_reviser_without_notes(self._ctx(state)) is None

    def test_wired_on_reviser(self):
        from interactive_creative.agent import visual_concept_reviser
        from interactive_creative.callbacks import skip_reviser_without_notes

        assert (
            visual_concept_reviser.before_agent_callback is skip_reviser_without_notes
        )


class TestEnsureTrendAndProductCallback:
    """The finalizer/reviser after_agent_callback repairs `final_visual_concepts`
    in place (same shape back: dict or JSON string) so the prompts that
    generate_image reads always name the trend motif and the product."""

    _CONCEPT = {
        "concept_name": "Plain",
        "trend_motif": "a ballot box",
        "image_generation_prompt": "A watercolor of a quiet plaza.",
    }

    @staticmethod
    def _ctx(state):
        return pytypes.SimpleNamespace(state=state)

    def _assert_repaired(self, prompt: str) -> None:
        assert "a ballot box" in prompt
        assert "Rocket Skates" in prompt

    def test_repairs_dict_state(self):
        from creative_agent.callbacks import ensure_trend_and_product_callback

        state = {
            "target_product": "Rocket Skates",
            "final_visual_concepts": {"visual_concepts": [dict(self._CONCEPT)]},
        }
        assert ensure_trend_and_product_callback(self._ctx(state)) is None
        value = state["final_visual_concepts"]
        assert isinstance(value, dict)
        self._assert_repaired(value["visual_concepts"][0]["image_generation_prompt"])

    def test_repairs_json_string_state(self):
        import json

        from creative_agent.callbacks import ensure_trend_and_product_callback

        state = {
            "target_product": "Rocket Skates",
            "final_visual_concepts": json.dumps({"visual_concepts": [self._CONCEPT]}),
        }
        assert ensure_trend_and_product_callback(self._ctx(state)) is None
        value = state["final_visual_concepts"]
        assert isinstance(value, str)
        prompt = json.loads(value)["visual_concepts"][0]["image_generation_prompt"]
        self._assert_repaired(prompt)

    def test_noop_when_missing_or_unparseable(self):
        from creative_agent.callbacks import ensure_trend_and_product_callback

        for value in (None, "", "not json", {"visual_concepts": None}, {}):
            state = {"target_product": "Rocket Skates"}
            if value is not None:
                state["final_visual_concepts"] = value
            before = dict(state)
            assert ensure_trend_and_product_callback(self._ctx(state)) is None
            assert state == before

    def test_noop_write_when_already_compliant(self):
        from creative_agent.callbacks import ensure_trend_and_product_callback

        concepts = {
            "visual_concepts": [
                {
                    "concept_name": "Good",
                    "trend_motif": "a ballot box",
                    "image_generation_prompt": "Rocket Skates by a ballot box.",
                }
            ]
        }

        class _NoWriteState(dict):
            def __setitem__(self, key, value):
                raise AssertionError(f"unexpected write to {key}")

        state = _NoWriteState(
            target_product="Rocket Skates", final_visual_concepts=concepts
        )
        assert ensure_trend_and_product_callback(self._ctx(state)) is None


def test_ensure_trend_and_product_callback_reads_rating_strictness():
    from types import SimpleNamespace

    from creative_agent.callbacks import ensure_trend_and_product_callback

    def _state(**extra):
        concept = {
            "concept_name": "Good",
            "trend_motif": "a ballot box",
            "image_generation_prompt": "Rocket Skates by ballot boxes.",
        }
        return {
            "target_product": "Rocket Skates",
            "final_visual_concepts": {"visual_concepts": [concept]},
            **extra,
        }

    plain = _state()
    ensure_trend_and_product_callback(SimpleNamespace(state=plain))
    prompt = plain["final_visual_concepts"]["visual_concepts"][0][
        "image_generation_prompt"
    ]
    assert prompt == "Rocket Skates by ballot boxes."

    strict = _state(rating_strictness=["product_not_visible", "trend_unclear"])
    ensure_trend_and_product_callback(SimpleNamespace(state=strict))
    prompt = strict["final_visual_concepts"]["visual_concepts"][0][
        "image_generation_prompt"
    ]
    assert prompt == (
        "Rocket Skates by ballot boxes. The scene visibly includes a ballot box."
        " The product is large and in the foreground."
    )


def test_recheck_concept_issues_callback_reads_rating_strictness():
    from types import SimpleNamespace

    from creative_agent.callbacks import recheck_concept_issues_callback

    copies = {
        "ad_copies": [
            {"original_id": i, "headline": f"Outrun Monday {i}"} for i in (1, 2)
        ]
    }
    concepts = [
        {
            "ad_copy_id": i,
            "concept_name": f"Dash {i}",
            "trend_motif": "a roadrunner",
            "image_generation_prompt": f'A roadrunner; text reads "Outrun Monday {i}".',
        }
        for i in (1, 2)
    ]
    state = {
        "target_product": "SE CE24",
        "ad_copy_critique": copies,
        "final_visual_concepts": {"visual_concepts": concepts},
    }
    recheck_concept_issues_callback(SimpleNamespace(state=state))
    assert state["final_visual_concepts__issues"] is None
    state["rating_strictness"] = ["text_problem"]
    recheck_concept_issues_callback(SimpleNamespace(state=state))
    (issue,) = state["final_visual_concepts__issues"]
    assert "more than 1 concept:" in issue


def test_recheck_concept_issues_callback():
    """Interactive's post-checkpoint recheck: the residual marker reflects the
    CURRENT concepts (no stale pre-checkpoint warning), brand quotes allowed."""
    from types import SimpleNamespace

    from creative_agent.callbacks import recheck_concept_issues_callback

    copies = {"ad_copies": [{"original_id": 1, "headline": "Outrun Monday"}]}

    def _state(prompt, motif="a roadrunner"):
        concept = {
            "ad_copy_id": 1,
            "concept_name": "Dash",
            "trend_motif": motif,
            "image_generation_prompt": prompt,
        }
        return {
            "brand": "PRS",
            "target_product": "SE CE24",
            "ad_copy_critique": copies,
            "final_visual_concepts": {"visual_concepts": [concept]},
            "final_visual_concepts__issues": ["Concept 1: stale warning"],
        }

    clean = _state('The headstock bears the "PRS" logo; text reads "PRS".')
    assert recheck_concept_issues_callback(SimpleNamespace(state=clean)) is None
    assert clean["final_visual_concepts__issues"] is None

    bad = _state('A sign reading "Speed is life".', motif="")
    assert recheck_concept_issues_callback(SimpleNamespace(state=bad)) is None
    issues = bad["final_visual_concepts__issues"]
    assert len(issues) == 2
    assert issues[0].startswith('Concept 1 ("Dash"): in-image text "Speed is life"')
    assert "trend_motif is empty" in issues[1]

    missing = {"final_visual_concepts__issues": ["stale"]}
    assert recheck_concept_issues_callback(SimpleNamespace(state=missing)) is None
    assert missing["final_visual_concepts__issues"] is None


def test_restore_unflagged_copies_callback(caplog):
    """The reviser's safety net reverts unflagged copies (with a warning) and
    is a no-op without a pre-revision snapshot or when the reviser complied."""
    import logging
    from types import SimpleNamespace

    from creative_agent.callbacks import restore_unflagged_copies_callback

    before = {"ad_copies": [{"original_id": 1, "h": "a"}, {"original_id": 2, "h": "b"}]}
    after = {"ad_copies": [{"original_id": 1, "h": "A"}, {"original_id": 2, "h": "B"}]}

    state = {"ad_copy_critique": after}
    restore_unflagged_copies_callback(SimpleNamespace(state=state))
    assert state["ad_copy_critique"] is after  # no snapshot: untouched

    state = {
        "ad_copy_critique": after,
        "ad_copy_critique__before_revision": before,
        "ad_copy_flagged_ids": ["2"],
    }
    with caplog.at_level(logging.WARNING):
        restore_unflagged_copies_callback(SimpleNamespace(state=state))
    assert state["ad_copy_critique"] == {
        "ad_copies": [{"original_id": 1, "h": "a"}, {"original_id": 2, "h": "B"}]
    }
    assert "restored copy 1: it was not flagged for revision" in caplog.text

    complied = {
        "ad_copies": [{"original_id": 1, "h": "a"}, {"original_id": 2, "h": "B"}]
    }
    state["ad_copy_critique"] = complied
    restore_unflagged_copies_callback(SimpleNamespace(state=state))
    assert state["ad_copy_critique"] is complied
