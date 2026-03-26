"""Tests for backend tool functions (pure logic, no external service calls)."""
import string
import pytest


# --- Artifact name sanitization ---
REMOVE_PUNCTUATION = str.maketrans("", "", string.punctuation)


def sanitize_artifact_name(concept_name: str) -> str:
    """Replicates the artifact key generation logic from creative_agent/tools.py."""
    return concept_name.translate(REMOVE_PUNCTUATION).replace(" ", "_") + ".png"


class TestArtifactNameSanitization:
    def test_basic_name(self):
        assert sanitize_artifact_name("Sunset Serenade") == "Sunset_Serenade.png"

    def test_name_with_punctuation(self):
        assert sanitize_artifact_name("Rock & Roll's Best!") == "Rock__Rolls_Best.png"

    def test_name_with_special_chars(self):
        result = sanitize_artifact_name("Concept #1: The \"Vibe\"")
        assert ".png" in result
        assert "#" not in result
        assert '"' not in result
        assert ":" not in result

    def test_empty_name(self):
        assert sanitize_artifact_name("") == ".png"

    def test_all_punctuation(self):
        assert sanitize_artifact_name("!@#$%") == ".png"

    def test_spaces_become_underscores(self):
        assert sanitize_artifact_name("a b c") == "a_b_c.png"


# --- Memorize tool ---
class MockState(dict):
    """Simple dict-based mock for ToolContext.state."""
    pass


class MockToolContext:
    def __init__(self):
        self.state = MockState()


class TestMemorizeTool:
    def test_memorize_stores_value(self):
        from creative_agent.tools import memorize

        ctx = MockToolContext()
        result = memorize("brand", "PRS Guitars", ctx)
        assert ctx.state["brand"] == "PRS Guitars"
        assert result["status"] == 'Stored "brand": "PRS Guitars"'

    def test_memorize_overwrites_existing(self):
        from creative_agent.tools import memorize

        ctx = MockToolContext()
        memorize("brand", "Old Brand", ctx)
        memorize("brand", "New Brand", ctx)
        assert ctx.state["brand"] == "New Brand"

    def test_memorize_different_keys(self):
        from creative_agent.tools import memorize

        ctx = MockToolContext()
        memorize("brand", "PRS", ctx)
        memorize("target_product", "SE CE24", ctx)
        assert ctx.state["brand"] == "PRS"
        assert ctx.state["target_product"] == "SE CE24"


class TestTrendTrawlerMemorizeTool:
    def test_memorize_stores_value(self):
        from trend_trawler.tools import memorize

        ctx = MockToolContext()
        result = memorize("target_audience", "Musicians", ctx)
        assert ctx.state["target_audience"] == "Musicians"
        assert "status" in result


# --- save_search_trends_to_session_state logic ---
class TestSaveSearchTrends:
    def test_appends_trend_to_existing_list(self):
        from trend_trawler.tools import save_search_trends_to_session_state

        ctx = MockToolContext()
        ctx.state["target_search_trends"] = {"target_search_trends": ["trend_a"]}

        result = save_search_trends_to_session_state("trend_b", ctx)
        assert result["status"] == "ok"
        trends = ctx.state["target_search_trends"]["target_search_trends"]
        assert "trend_a" in trends
        assert "trend_b" in trends
