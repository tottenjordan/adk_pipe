"""Tests for callback functions (citation replacement, state init, rate limiting)."""
import re
import time
import pytest


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
            "src-1": {"title": "Guitar World", "url": "https://guitarworld.com/article"},
            "src-2": {"title": "Rolling Stone", "url": "https://rollingstone.com/review"},
        }

        def tag_replacer(match: re.Match) -> str:
            short_id = match.group(1)
            source_info = sources.get(short_id)
            if not source_info:
                return ""
            display_text = source_info.get("title", short_id)
            return f" [{display_text}]({source_info['url']})"

        text = 'Great tone.<cite source="src-1" /> Critics agree.<cite source="src-2" />'
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

    def test_trend_trawler_sets_trawler_output(self):
        from trend_trawler.callbacks import _set_initial_states
        from trend_trawler.config import config

        target = {}
        source = {"brand": "PRS"}
        _set_initial_states(source, target)

        assert target["agent_output_dir"] == "trawler_output"
        assert target["brand"] == "PRS"


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
