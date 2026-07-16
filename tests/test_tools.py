"""Tests for backend tool functions (pure logic, no external service calls)."""

import string


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
        result = sanitize_artifact_name('Concept #1: The "Vibe"')
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
        from trend_scout.tools import memorize

        ctx = MockToolContext()
        result = memorize("target_audience", "Musicians", ctx)
        assert ctx.state["target_audience"] == "Musicians"
        assert "status" in result


# --- review_trends checkpoint tool (opt-in interactive trend picking) ---
class TestReviewTrendsTool:
    def _ctx(self):
        """A tool_context double exposing the `.actions.skip_summarization`
        attribute the LongRunningFunctionTool checkpoint sets (mirrors the shape
        interactive_creative's review_* checkpoints rely on)."""
        from types import SimpleNamespace

        return SimpleNamespace(actions=SimpleNamespace(skip_summarization=False))

    def test_returns_none_and_skips_summarization(self):
        from trend_scout.review_tools import review_trends

        ctx = self._ctx()
        result = review_trends(ctx)
        assert result is None
        assert ctx.actions.skip_summarization is True

    def test_wrapped_in_long_running_function_tool(self):
        from google.adk.tools.long_running_tool import LongRunningFunctionTool
        from trend_scout.review_tools import review_trends_tool

        assert isinstance(review_trends_tool, LongRunningFunctionTool)


# --- record_research_gaps logic ---
class TestRecordResearchGaps:
    def test_exhaustion_marker_becomes_note(self):
        from trend_scout.tools import record_research_gaps

        ctx = MockToolContext()
        ctx.state["info_gtrends__retry_exhausted"] = True
        result = record_research_gaps(ctx)

        assert result["status"] == "success"
        assert result["count"] == 1
        assert "info_gtrends" in ctx.state["research_gaps"]
        assert ctx.state["research_gaps"] == result["research_gaps"]

    def test_clean_state_is_empty_string(self):
        from trend_scout.tools import record_research_gaps

        ctx = MockToolContext()
        ctx.state["info_gtrends"] = "some real briefing"
        result = record_research_gaps(ctx)

        assert result["status"] == "success"
        assert result["count"] == 0
        assert ctx.state["research_gaps"] == ""  # happy path renders nothing


# --- write_trends_to_bq SQL builder (pure, offline) ---
class TestBuildTrendInsertSql:
    def _sql(self, **overrides):
        from trend_scout.tools import _build_trend_insert_sql

        params = dict(
            table="hybrid-vertex.trend_trawler.target_trends_crf",
            unique_id="abcd1234",
            trend="Golden Dip",
            max_date="07/15/2026",
            current_date="07/15/2026",
            trawler_gcs="https://console.cloud.google.com/storage/browser/b/f/d",
            brand="PRS",
            target_audience="Musicians",
            target_product="SE CE24",
            key_selling_points="tone",
            research_gaps="",
        )
        params.update(overrides)
        return _build_trend_insert_sql(**params)

    @staticmethod
    def _param_value(params, name):
        for p in params:
            if p.name == name:
                return p.value
        raise AssertionError(f"query parameter {name!r} not found")

    def test_includes_research_gaps_column_and_trend(self):
        sql, params = self._sql()
        # column names live in the SQL; the trend value is a bound parameter,
        # never interpolated into the statement text.
        assert "research_gaps" in sql
        assert "target_trends_crf" in sql
        assert "Golden Dip" not in sql
        assert self._param_value(params, "trend") == "Golden Dip"

    def test_research_gaps_value_bound_as_parameter(self):
        note = "Step 'info_gtrends' exhausted retries and produced no output."
        sql, params = self._sql(research_gaps=note)
        # the apostrophe in the note must not appear (unescaped) in the SQL text
        assert note not in sql
        assert self._param_value(params, "research_gaps") == note

    def test_empty_research_gaps_still_bound(self):
        sql, params = self._sql(research_gaps="")
        assert "research_gaps)" in sql
        assert self._param_value(params, "research_gaps") == ""

    def test_values_are_parameter_placeholders_not_literals(self):
        # regression: values must be @named placeholders, so quotes/apostrophes
        # in a trend can't terminate a string literal early (the "Prophetic" bug).
        sql, _ = self._sql()
        assert "@trend" in sql
        assert "@brand" in sql

    def test_trend_with_quotes_does_not_leak_into_sql(self):
        # regression for the 400 "Expected ) or , but got identifier" error: a
        # trend containing a double quote used to break the INSERT literal.
        tricky = 'The "Prophetic" Trend'
        sql, params = self._sql(trend=tricky)
        assert tricky not in sql
        assert self._param_value(params, "trend") == tricky


# --- save_search_trends_to_session_state logic ---
class TestSaveSearchTrends:
    def test_appends_trend_to_existing_list(self):
        from trend_scout.tools import save_search_trends_to_session_state

        ctx = MockToolContext()
        ctx.state["target_search_trends"] = {"target_search_trends": ["trend_a"]}

        result = save_search_trends_to_session_state("trend_b", ctx)
        assert result["status"] == "ok"
        trends = ctx.state["target_search_trends"]["target_search_trends"]
        assert "trend_a" in trends
        assert "trend_b" in trends

    def test_appends_first_trend_to_empty_init_state(self):
        """The initial state is {"target_search_trends": []}; the first trend
        must still be appended (regression guard for the old identity check)."""
        from trend_scout.tools import save_search_trends_to_session_state

        ctx = MockToolContext()
        ctx.state["target_search_trends"] = {"target_search_trends": []}

        result = save_search_trends_to_session_state("trend_a", ctx)
        assert result["status"] == "ok"
        trends = ctx.state["target_search_trends"]["target_search_trends"]
        assert trends == ["trend_a"]


# --- build_eval_bq_row (pure eval-report -> BQ row) ---
SAMPLE_REPORT = {
    "brand": "PRS Guitars",
    "target_product": "SE CE24",
    "target_search_trend": "tswift engaged",
    "summary": {
        "total_ad_copies": 4,
        "ad_copies_passed": 3,
        "avg_ad_copy_score": 0.82,
        "total_visual_concepts": 4,
        "visual_concepts_passed": 2,
        "avg_visual_score": 0.71,
        "overall_pass_rate": 0.625,
        "weakest_dimensions": ["stopping_power", "cta_strength"],
    },
}


class TestBuildEvalBqRow:
    def _row(self, **overrides):
        from creative_agent.tools import build_eval_bq_row

        kwargs = dict(
            report=SAMPLE_REPORT,
            eval_uuid="ev123456",
            creative_uuid="cr789012",
            now_datetime="2026-07-13 10:30:00",
            target_trend="tswift engaged",
            brand="PRS Guitars",
            target_product="SE CE24",
            eval_report_gcs_uri="gs://bucket/run/creative_output/creative_eval_report.json",
        )
        kwargs.update(overrides)
        return build_eval_bq_row(**kwargs)

    def test_maps_summary_fields(self):
        row = self._row()
        assert row["overall_pass_rate"] == 0.625
        assert row["total_ad_copies"] == 4
        assert row["ad_copies_passed"] == 3
        assert row["avg_visual_score"] == 0.71

    def test_weakest_dimensions_comma_joined(self):
        row = self._row()
        assert row["weakest_dimensions"] == "stopping_power,cta_strength"

    def test_carries_ids_and_link(self):
        row = self._row()
        assert row["uuid"] == "ev123456"
        assert row["creative_uuid"] == "cr789012"
        assert row["datetime"] == "2026-07-13 10:30:00"
        assert row["eval_report_gcs_uri"].endswith("creative_eval_report.json")

    def test_numeric_coercion(self):
        # Judge/JSON round-trips can hand back ints-as-strings; row must be typed.
        report = {
            **SAMPLE_REPORT,
            "summary": {
                **SAMPLE_REPORT["summary"],
                "total_ad_copies": "4",
                "overall_pass_rate": "0.5",
            },
        }
        row = self._row(report=report)
        assert row["total_ad_copies"] == 4 and isinstance(row["total_ad_copies"], int)
        assert row["overall_pass_rate"] == 0.5 and isinstance(
            row["overall_pass_rate"], float
        )

    def test_empty_weakest_dimensions(self):
        report = {
            **SAMPLE_REPORT,
            "summary": {**SAMPLE_REPORT["summary"], "weakest_dimensions": []},
        }
        assert self._row(report=report)["weakest_dimensions"] == ""

    def test_research_gaps_pipe_joined_from_warnings(self):
        report = {
            **SAMPLE_REPORT,
            "warnings": [
                "Research step 'gs' exhausted.",
                "Research step 'ca' exhausted.",
            ],
        }
        row = self._row(report=report)
        assert (
            row["research_gaps"]
            == "Research step 'gs' exhausted. | Research step 'ca' exhausted."
        )

    def test_research_gaps_empty_when_no_warnings(self):
        # SAMPLE_REPORT has no `warnings` key -> empty string, not KeyError.
        assert self._row()["research_gaps"] == ""

    def test_row_keys_match_table_schema(self):
        # Guard: row keys must equal the creative_evals column set exactly.
        expected = {
            "uuid",
            "creative_uuid",
            "datetime",
            "target_trend",
            "brand",
            "target_product",
            "overall_pass_rate",
            "total_ad_copies",
            "ad_copies_passed",
            "avg_ad_copy_score",
            "total_visual_concepts",
            "visual_concepts_passed",
            "avg_visual_score",
            "weakest_dimensions",
            "eval_report_gcs_uri",
            "research_gaps",
        }
        assert set(self._row().keys()) == expected


class TestResearchWarningBanner:
    """The HTML gallery must surface research degradation as a visible banner."""

    def test_empty_when_no_warnings(self):
        from creative_agent.tools import _build_research_warning_banner

        assert _build_research_warning_banner([]) == ""

    def test_renders_banner_with_each_note(self):
        from creative_agent.tools import _build_research_warning_banner

        html = _build_research_warning_banner(
            ["Research step 'gs' exhausted.", "Research step 'ca' exhausted."]
        )
        assert 'class="research-warning"' in html
        assert "Research step 'gs' exhausted." in html
        assert "Research step 'ca' exhausted." in html
        # one list item per note
        assert html.count("<li>") == 2


class TestWriteTrendsUuidStash:
    def test_stashes_creative_row_uuid(self, monkeypatch):
        """write_trends_to_bq must record its generated uuid in state so the
        eval row can foreign-key back to the creative row."""
        # write_trends_to_bq now lives in creative_agent.bq_tools (re-exported from
        # tools); patch/call it there so the _get_bigquery_client stub takes effect.
        import creative_agent.bq_tools as t

        class _Job:
            errors = None
            job_id = "j1"
            num_dml_affected_rows = 1

            def result(self):
                return None

        captured = {}

        class _BQ:
            def query(self, sql, job_config=None):
                captured["sql"] = sql
                captured["job_config"] = job_config
                return _Job()

        monkeypatch.setattr(t, "_get_bigquery_client", lambda: _BQ())

        ctx = MockToolContext()
        ctx.state.update(
            {
                "gcs_folder": "2026_07_13_run",
                "agent_output_dir": "creative_output",
                "target_search_trends": "tswift engaged",
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_points": "wide tonal range",
            }
        )
        result = t.write_trends_to_bq(ctx)
        assert result["status"] == "success"
        assert ctx.state["creative_row_uuid"]  # non-empty 8-char id
        assert len(ctx.state["creative_row_uuid"]) == 8
        # the trend value must be a bound parameter, not interpolated into SQL
        assert "tswift engaged" not in captured["sql"]
        param_names = {p.name for p in captured["job_config"].query_parameters}
        assert "target_trend" in param_names
