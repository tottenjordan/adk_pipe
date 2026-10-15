"""Tests for backend tool functions (pure logic, no external service calls)."""

import asyncio
import datetime
import string
from types import SimpleNamespace

import pytest

from tests._fake_bq import FakeBigQueryClient
from tests._fakes import FakeToolContext


def _squash(sql: str) -> str:
    """Collapse whitespace so SQL-fragment asserts ignore layout/indentation."""
    return " ".join(sql.split())


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
class TestMemorizeTool:
    def test_memorize_stores_value(self):
        from creative_agent.tools import memorize

        ctx = FakeToolContext()
        result = memorize("brand", "PRS Guitars", ctx)
        assert ctx.state["brand"] == "PRS Guitars"
        assert result["status"] == 'Stored "brand": "PRS Guitars"'

    def test_memorize_overwrites_existing(self):
        from creative_agent.tools import memorize

        ctx = FakeToolContext()
        memorize("brand", "Old Brand", ctx)
        memorize("brand", "New Brand", ctx)
        assert ctx.state["brand"] == "New Brand"

    def test_memorize_different_keys(self):
        from creative_agent.tools import memorize

        ctx = FakeToolContext()
        memorize("brand", "PRS", ctx)
        memorize("target_product", "SE CE24", ctx)
        assert ctx.state["brand"] == "PRS"
        assert ctx.state["target_product"] == "SE CE24"


class TestTrendTrawlerMemorizeTool:
    def test_memorize_stores_value(self):
        from trend_scout.tools import memorize

        ctx = FakeToolContext()
        result = memorize("target_audience", "Musicians", ctx)
        assert ctx.state["target_audience"] == "Musicians"
        assert "status" in result


# --- review_trends checkpoint tool (opt-in interactive trend picking) ---
class TestReviewTrendsTool:
    def _ctx(self):
        """A tool_context double exposing the `.actions.skip_summarization`
        attribute the LongRunningFunctionTool checkpoint sets (mirrors the shape
        interactive_creative's review_* checkpoints rely on)."""

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

        ctx = FakeToolContext()
        ctx.state["info_gtrends__retry_exhausted"] = True
        result = record_research_gaps(ctx)

        assert result["status"] == "success"
        assert result["count"] == 1
        assert "info_gtrends" in ctx.state["research_gaps"]
        assert ctx.state["research_gaps"] == result["research_gaps"]

    def test_clean_state_is_empty_string(self):
        from trend_scout.tools import record_research_gaps

        ctx = FakeToolContext()
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
            table="test-project.trend_trawler.target_trends_crf",
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
        assert "S.research_gaps," in sql
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

    def test_is_insert_only_merge_keyed_on_uuid_and_trend(self):
        # at-least-once tool execution: a repeat write for the same session's
        # (uuid, trend) must be a no-op, so the statement is an INSERT-only MERGE.
        sql = _squash(self._sql()[0])
        assert "MERGE" in sql
        assert "INSERT INTO" not in sql
        assert "ON T.uuid = S.uuid AND T.target_trend = S.target_trend" in sql
        assert "WHEN NOT MATCHED THEN" in sql
        assert "WHEN MATCHED" not in sql


class TestTrendScoutWriteTrendsIdempotent:
    def _run(self, monkeypatch, session_id):
        import trend_scout.tools as t

        bq = FakeBigQueryClient()
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)
        monkeypatch.setattr(t, "_get_gtrends_max_date", lambda: "07/17/2026")
        ctx = FakeToolContext(session_id=session_id)
        ctx.state.update(
            {
                "gcs_folder": "2026_07_13_run",
                "agent_output_dir": "trawler_output",
                "target_search_trends": {"target_search_trends": ["t1", "t2", "t3"]},
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_points": "wide tonal range",
            }
        )
        asyncio.run(t.write_trends_to_bq(ctx))
        out = []
        for sql, job_config in bq.queries:
            assert "MERGE" in sql
            params = {p.name: p.value for p in job_config.query_parameters}
            out.append((params["unique_id"], params["trend"]))
        return out

    def test_same_session_same_ids_per_trend(self, monkeypatch):
        first = self._run(monkeypatch, "sess-1")
        second = self._run(monkeypatch, "sess-1")
        assert first == second
        assert [trend for _, trend in first] == ["t1", "t2", "t3"]
        # one batch per session: every trend row shares the session-derived uuid
        assert len({uid for uid, _ in first}) == 1
        assert len(first[0][0]) == 8

    def test_different_sessions_different_ids(self, monkeypatch):
        a = self._run(monkeypatch, "sess-1")
        b = self._run(monkeypatch, "sess-2")
        assert a[0][0] != b[0][0]


class TestTrendContextColumn:
    """write_trends_to_bq binds each trend's compact context (trend_context)."""

    def _sql(self, **overrides):
        return TestBuildTrendInsertSql()._sql(**overrides)

    def test_merge_inserts_and_binds_trend_context(self):
        ctx = '{"hook": "It\'s game day"}'
        sql, params = self._sql(trend_context=ctx)
        squashed = _squash(sql)
        assert "@trend_context AS trend_context" in squashed
        assert "research_gaps, trend_context)" in squashed
        assert "S.research_gaps, S.trend_context)" in squashed
        assert ctx not in sql
        assert TestBuildTrendInsertSql._param_value(params, "trend_context") == ctx

    def test_trend_context_defaults_to_empty(self):
        _, params = self._sql()
        assert TestBuildTrendInsertSql._param_value(params, "trend_context") == ""

    def test_write_trends_binds_per_trend_context(self, monkeypatch):
        import json

        import trend_scout.tools as t

        bq = FakeBigQueryClient()
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)
        monkeypatch.setattr(t, "_get_gtrends_max_date", lambda: "10/10/2026")
        ctx = FakeToolContext(session_id="sess-ctx")
        ctx.state.update(
            {
                "gcs_folder": "2026_10_11_run",
                "target_search_trends": {"target_search_trends": ["T1", "T2"]},
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_points": "tone",
                "info_gtrends": json.dumps(
                    {
                        "analyzed_trends": [
                            {
                                "term": "T1",
                                "context": "ctx one",
                                "cultural_angle": "angle",
                                "event_hint": "final on 2026-10-12",
                            }
                        ]
                    }
                ),
                "selected_gtrends": '### T1\n* **The "Hook":** Hook one\n',
            }
        )
        asyncio.run(t.write_trends_to_bq(ctx))
        bound = {}
        for _, job_config in bq.queries:
            params = {p.name: p.value for p in job_config.query_parameters}
            bound[params["trend"]] = params["trend_context"]
        assert json.loads(bound["T1"]) == {
            "context": "ctx one",
            "cultural_angle": "angle",
            "event_hint": "final on 2026-10-12",
            "hook": "Hook one",
        }
        assert bound["T2"] == ""  # no research for it → empty, never an error

    def test_write_trends_passes_trend_flags(self, monkeypatch):
        import json

        import trend_scout.tools as t

        bq = FakeBigQueryClient()
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)
        monkeypatch.setattr(t, "_get_gtrends_max_date", lambda: "10/10/2026")
        ctx = FakeToolContext(session_id="sess-flags")
        ctx.state.update(
            {
                "gcs_folder": "2026_10_12_run",
                "target_search_trends": {"target_search_trends": ["T1", "T2"]},
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_points": "tone",
                "info_gtrends": json.dumps(
                    {"analyzed_trends": [{"term": "T1", "context": "ctx one"}]}
                ),
                "trend_flags": json.dumps(
                    {"flags": [{"term": "t1", "sensitive": True, "reason": "loss"}]}
                ),
            }
        )
        asyncio.run(t.write_trends_to_bq(ctx))
        bound = {}
        for _, job_config in bq.queries:
            params = {p.name: p.value for p in job_config.query_parameters}
            bound[params["trend"]] = params["trend_context"]
        assert json.loads(bound["T1"]) == {
            "context": "ctx one",
            "sensitive": True,
            "sensitive_reason": "loss",
        }
        assert bound["T2"] == ""


class TestTrawlerDate:
    """trawler_date is the run's date (state current_date, TREND_TIMEZONE), not
    the server's UTC clock, so it matches the date the prompts were given."""

    def _bound_current_date(self, monkeypatch, state_extra):
        import trend_scout.tools as t

        bq = FakeBigQueryClient()
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)
        monkeypatch.setattr(t, "_get_gtrends_max_date", lambda: "10/10/2026")
        ctx = FakeToolContext(session_id="sess-date")
        ctx.state.update(
            {
                "gcs_folder": "2026_10_11_run",
                "target_search_trends": {"target_search_trends": ["T1"]},
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_points": "tone",
                **state_extra,
            }
        )
        asyncio.run(t.write_trends_to_bq(ctx))
        (_, job_config) = bq.queries[-1]
        params = {p.name: p.value for p in job_config.query_parameters}
        return params["current_date"]

    def test_uses_state_current_date(self, monkeypatch):
        bound = self._bound_current_date(monkeypatch, {"current_date": "2026-10-11"})
        assert bound == "10/11/2026"

    def test_falls_back_to_trend_timezone_clock(self, monkeypatch):
        import trend_scout.tools as t

        seen = []

        def fake_now(tz):
            seen.append(tz)
            return datetime.datetime(2026, 10, 12, 23, 30)

        monkeypatch.setattr(t, "now_in_tz", fake_now)
        for extra in ({}, {"current_date": ""}, {"current_date": "not-a-date"}):
            assert self._bound_current_date(monkeypatch, extra) == "10/12/2026"
        assert seen == [t.config.trend_timezone] * 3


class TestGatheredRefreshDate:
    """TS-2: rows carry the date the trends were GATHERED (state
    gtrends_refresh_date), not the public table's latest date at write time
    (an interactive pause can span a table refresh)."""

    def _bound(self, monkeypatch, state_extra):
        import trend_scout.tools as t

        bq = FakeBigQueryClient()
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)
        calls = []

        def max_date():
            calls.append(1)
            return "10/14/2026"

        monkeypatch.setattr(t, "_get_gtrends_max_date", max_date)
        ctx = FakeToolContext(session_id="sess-refresh")
        ctx.state.update(
            {
                "gcs_folder": "2026_10_14_run",
                "target_search_trends": {"target_search_trends": ["T1"]},
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_points": "tone",
                **state_extra,
            }
        )
        asyncio.run(t.write_trends_to_bq(ctx))
        (_, job_config) = bq.queries[-1]
        params = {p.name: p.value for p in job_config.query_parameters}
        return params["max_date"], len(calls)

    def test_uses_the_gathered_refresh_date(self, monkeypatch):
        bound, calls = self._bound(
            monkeypatch, {"gtrends_refresh_date": "2026-10-13"}
        )
        assert bound == "10/13/2026"
        assert calls == 0  # the max-date query never runs

    def test_falls_back_to_the_table_max_date(self, monkeypatch):
        for extra in ({}, {"gtrends_refresh_date": ""}, {"gtrends_refresh_date": "x"}):
            assert self._bound(monkeypatch, extra) == ("10/14/2026", 1)

    def test_gather_queries_are_time_bounded(self, monkeypatch):
        import trend_scout.tools as t

        day = datetime.date(2026, 10, 13)

        def rows(sql, _job_config):
            if "MAX(refresh_date)" in sql:
                return [{"max_date": day}]
            return [{"term": "a", "refresh_date": day}]

        bq = FakeBigQueryClient(rows)
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)
        assert t._get_gtrends_max_date() == "10/13/2026"
        assert t._query_daily_gtrends() == ("10/13/2026", [("a", 1, "2026-10-13")])
        assert len(bq.queries) == 3  # max date, then (max date + top terms)
        for _sql, job_config in bq.queries:
            assert job_config is not None
            assert int(job_config.job_timeout_ms) == t.GTRENDS_JOB_TIMEOUT_MS
        assert [job.result_timeout for job in bq.jobs] == [
            t.GTRENDS_RESULT_TIMEOUT_SECONDS
        ] * 3


# --- save_search_trends_to_session_state logic ---
class TestSaveSearchTrends:
    def test_appends_trend_to_existing_list(self):
        from trend_scout.tools import save_search_trends_to_session_state

        ctx = FakeToolContext()
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

        ctx = FakeToolContext()
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

    def test_weakest_dimension_labels_human_readable(self):
        report = {
            **SAMPLE_REPORT,
            "summary": {
                **SAMPLE_REPORT["summary"],
                "weakest_dimensions": ["trend_visual_connection", "copy_quality"],
            },
        }
        row = self._row(report=report)
        assert row["weakest_dimension_labels"] == "Trend connection, Copy quality"
        empty = {
            **SAMPLE_REPORT,
            "summary": {**SAMPLE_REPORT["summary"], "weakest_dimensions": []},
        }
        assert self._row(report=empty)["weakest_dimension_labels"] == ""

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
            "weakest_dimension_labels",
            "eval_report_gcs_uri",
            "research_gaps",
            "gates_pass_rate",
        }
        assert set(self._row().keys()) == expected

    def test_gates_pass_rate_from_summary(self):
        report = {
            **SAMPLE_REPORT,
            "summary": {**SAMPLE_REPORT["summary"], "gates_pass_rate": "0.75"},
        }
        assert self._row(report=report)["gates_pass_rate"] == 0.75

    def test_gates_pass_rate_null_for_pre_gate_reports(self):
        # SAMPLE_REPORT predates the gates: NULL, not a misleading 0.0.
        assert self._row()["gates_pass_rate"] is None
        report = {
            **SAMPLE_REPORT,
            "summary": {**SAMPLE_REPORT["summary"], "gates_pass_rate": None},
        }
        assert self._row(report=report)["gates_pass_rate"] is None


class TestBuildEvalMergeSql:
    """The eval row dict stays the single source of columns; the MERGE builder
    types + parameterizes every value and keys on the row's uuid."""

    TABLE = "test-project.trend_trawler.creative_evals"

    def _row(self, **overrides):
        from creative_agent.tools import build_eval_bq_row

        kwargs = dict(
            report=SAMPLE_REPORT,
            eval_uuid="ev123456",
            creative_uuid="cr789012",
            now_datetime="2026-07-13 10:30:00",
            target_trend='Taylor\'s "engaged"',
            brand="PRS Guitars",
            target_product="SE CE24",
            eval_report_gcs_uri="gs://bucket/run/creative_output/creative_eval_report.json",
        )
        kwargs.update(overrides)
        return build_eval_bq_row(**kwargs)

    def _build(self, row):
        from creative_agent.bq_tools import _build_eval_merge_sql

        return _build_eval_merge_sql(self.TABLE, row)

    def test_merge_keyed_on_uuid(self):
        sql = _squash(self._build(self._row())[0])
        assert "MERGE" in sql
        assert self.TABLE in sql
        assert "ON T.uuid = S.uuid" in sql
        assert "WHEN NOT MATCHED THEN" in sql

    def test_rerun_updates_the_matched_row(self):
        """A re-run overwrites the GCS report JSON, so the matched BQ row must be
        updated too (same idempotent uuid key, never a second row)."""
        row = self._row()
        sql = _squash(self._build(row)[0])
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        update = sql.split("WHEN MATCHED THEN UPDATE SET", 1)[1].split("WHEN NOT")[0]
        assert " uuid = S.uuid" not in f" {update}"  # the key is never updated
        for col in set(row) - {"uuid"}:
            assert f"{col} = S.{col}" in update

    def test_every_row_column_inserted_and_bound(self):
        row = self._row()
        sql, params = self._build(row)
        by_name = {p.name: p for p in params}
        assert set(by_name) == set(row)
        flat = _squash(sql)
        for col, value in row.items():
            assert f"@{col} AS {col}" in flat
            assert f"S.{col}" in sql
            if col == "datetime":
                # DATETIME is bound as a datetime, not the row's string form
                value = datetime.datetime.fromisoformat(value)
            assert by_name[col].value == value

    def test_values_not_interpolated(self):
        row = self._row()
        sql, _ = self._build(row)
        for value in ("Taylor", "PRS Guitars", "ev123456", "cr789012", "2026-07-13"):
            assert value not in sql

    def test_param_types_match_table_schema(self):
        _, params = self._build(self._row())
        types = {p.name: p.type_ for p in params}
        assert types["datetime"] == "DATETIME"
        assert types["overall_pass_rate"] == "FLOAT64"
        assert types["avg_visual_score"] == "FLOAT64"
        assert types["total_ad_copies"] == "INT64"
        assert types["visual_concepts_passed"] == "INT64"
        assert types["uuid"] == "STRING"
        assert types["research_gaps"] == "STRING"
        assert types["gates_pass_rate"] == "FLOAT64"

    def test_none_becomes_typed_null(self):
        row = {**self._row(), "avg_visual_score": None}
        _, params = self._build(row)
        p = next(p for p in params if p.name == "avg_visual_score")
        assert p.type_ == "FLOAT64" and p.value is None

    def test_column_types_cover_row_keys(self):
        from creative_agent.bq_tools import EVAL_COLUMN_TYPES

        assert set(EVAL_COLUMN_TYPES) == set(self._row())

    def test_unknown_column_rejected(self):
        with pytest.raises(KeyError):
            self._build({**self._row(), "bogus": "x"})


class TestWriteEvalReportIdempotent:
    """write_eval_report_to_bq must derive eval_uuid from the session and MERGE,
    never stream (insert_rows_json can't dedupe an at-least-once re-run)."""

    def _patch(self, monkeypatch, result_error=None):
        import creative_agent.bq_tools as t

        class _NoStreamingBQ(FakeBigQueryClient):
            def insert_rows_json(self, *a, **k):
                raise AssertionError("streaming insert is not idempotent")

        bq = _NoStreamingBQ(result_error=result_error)
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)
        self.bq = bq
        return t, bq.queries

    @staticmethod
    def _ctx(session_id="sess-1"):
        ctx = FakeToolContext(session_id=session_id)
        ctx.state.update(
            {
                "creative_evaluation_report": SAMPLE_REPORT,
                "creative_row_uuid": "abcd1234",
                "target_search_trends": "tswift engaged",
                "brand": "PRS",
                "target_product": "SE CE24",
            }
        )
        return ctx

    def test_same_session_same_eval_uuid_via_merge(self, monkeypatch):
        t, captured = self._patch(monkeypatch)
        first = t.write_eval_report_to_bq(self._ctx())
        second = t.write_eval_report_to_bq(self._ctx())
        assert first["status"] == second["status"] == "success"
        assert first["eval_uuid"] == second["eval_uuid"]
        assert len(first["eval_uuid"]) == 8
        assert len(captured) == 2
        assert all("MERGE" in sql for sql, _ in captured)
        params = {p.name: p.value for p in captured[0][1].query_parameters}
        assert params["uuid"] == first["eval_uuid"]
        assert params["creative_uuid"] == "abcd1234"

    def test_success_records_eval_row_uuid(self, monkeypatch):
        """The eval row uuid is recorded for the finalize summary."""
        t, _ = self._patch(monkeypatch)
        ctx = self._ctx()
        result = t.write_eval_report_to_bq(ctx)
        assert ctx.state["eval_bq_row_uuid"] == result["eval_uuid"]

    def test_failure_leaves_eval_row_uuid_unset(self, monkeypatch):
        from google.api_core.exceptions import BadRequest

        t, _ = self._patch(monkeypatch, result_error=BadRequest("invalid"))
        ctx = self._ctx()
        with pytest.raises(BadRequest):
            t.write_eval_report_to_bq(ctx)
        assert "eval_bq_row_uuid" not in ctx.state

    def test_different_sessions_different_eval_uuid(self, monkeypatch):
        t, _ = self._patch(monkeypatch)
        a = t.write_eval_report_to_bq(self._ctx("sess-1"))
        b = t.write_eval_report_to_bq(self._ctx("sess-2"))
        assert a["eval_uuid"] != b["eval_uuid"]

    def test_raises_when_the_job_fails(self, monkeypatch):
        """QueryJob.result() raises on a failed job; the error propagates."""
        from google.api_core.exceptions import BadRequest

        t, _ = self._patch(monkeypatch, result_error=BadRequest("invalid"))
        with pytest.raises(BadRequest):
            t.write_eval_report_to_bq(self._ctx())

    def test_merge_is_bounded(self, monkeypatch):
        """The MERGE carries a server-side job timeout and a bounded wait."""
        t, captured = self._patch(monkeypatch)
        t.write_eval_report_to_bq(self._ctx())
        _, job_config = captured[0]
        assert int(job_config.job_timeout_ms) == t.BQ_WRITE_JOB_TIMEOUT_MS
        assert self.bq.jobs[0].result_timeout == t.BQ_WRITE_RESULT_TIMEOUT_SECS
        assert 0 < t.BQ_WRITE_RESULT_TIMEOUT_SECS * 1000 >= t.BQ_WRITE_JOB_TIMEOUT_MS

    def test_missing_report_returns_error(self, monkeypatch):
        t, captured = self._patch(monkeypatch)
        ctx = FakeToolContext()
        assert t.write_eval_report_to_bq(ctx)["status"] == "error"
        assert captured == []


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
        assert "Run notes:" in html

    def test_notes_are_html_escaped(self):
        """A model-written claim quoted in a brief issue must not inject markup."""
        from agent_common import collect_degradation_warnings
        from creative_agent.brief_check import check_brief
        from creative_agent.tools import _build_research_warning_banner
        from tests.test_creative_agent_graph import _BRIEF

        claim = "<script>alert(1)</script>"
        brief = {**_BRIEF, "reasons_to_believe": [{"claim": claim, "source_id": ""}]}
        notes = collect_degradation_warnings(
            {"creative_brief__issues": check_brief(brief)}
        )
        html = _build_research_warning_banner(notes)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestWriteTrendsUuidStash:
    def test_stashes_creative_row_uuid(self, monkeypatch):
        """write_trends_to_bq must record its generated uuid in state so the
        eval row can foreign-key back to the creative row."""
        # write_trends_to_bq now lives in creative_agent.bq_tools (re-exported from
        # tools); patch/call it there so the _get_bigquery_client stub takes effect.
        import creative_agent.bq_tools as t

        bq = FakeBigQueryClient()
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)

        ctx = FakeToolContext()
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
        sql, job_config = bq.queries[-1]
        assert "tswift engaged" not in sql
        param_names = {p.name for p in job_config.query_parameters}
        assert "target_trend" in param_names


class TestWriteTrendsIdempotent:
    """Resumable apps / CRF retries give at-least-once tool execution, so the
    creative row key must be session-derived and the write a MERGE."""

    STATE = {
        "gcs_folder": "2026_07_13_run",
        "agent_output_dir": "creative_output",
        "target_search_trends": "tswift engaged",
        "brand": "PRS",
        "target_audience": "musicians",
        "target_product": "SE CE24",
        "key_selling_points": "wide tonal range",
    }

    def _run(self, monkeypatch, session_id):
        import creative_agent.bq_tools as t

        bq = FakeBigQueryClient()
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)
        ctx = FakeToolContext(session_id=session_id)
        ctx.state.update(self.STATE)
        t.write_trends_to_bq(ctx)
        return ctx.state["creative_row_uuid"], bq.queries

    def test_same_session_same_uuid(self, monkeypatch):
        first, _ = self._run(monkeypatch, "sess-1")
        second, _ = self._run(monkeypatch, "sess-1")
        assert first == second
        assert len(first) == 8  # CRF joins on the 8-char creative_uuid

    def test_different_sessions_different_uuid(self, monkeypatch):
        a, _ = self._run(monkeypatch, "sess-1")
        b, _ = self._run(monkeypatch, "sess-2")
        assert a != b

    def test_merge_is_bounded(self, monkeypatch):
        """The MERGE carries a server-side job timeout and a bounded wait."""
        import creative_agent.bq_tools as t

        bq = FakeBigQueryClient()
        monkeypatch.setattr(t, "_get_bigquery_client", lambda: bq)
        ctx = FakeToolContext(session_id="sess-1")
        ctx.state.update(self.STATE)
        t.write_trends_to_bq(ctx)
        _, job_config = bq.queries[0]
        assert int(job_config.job_timeout_ms) == t.BQ_WRITE_JOB_TIMEOUT_MS
        assert bq.jobs[0].result_timeout == t.BQ_WRITE_RESULT_TIMEOUT_SECS

    def test_sql_is_parameterized_merge(self, monkeypatch):
        uid, captured = self._run(monkeypatch, "sess-1")
        (sql, job_config), *_ = captured
        assert "MERGE" in sql
        assert "WHEN NOT MATCHED" in sql
        assert "INSERT INTO" not in sql
        assert "tswift engaged" not in sql
        params = {p.name: p.value for p in job_config.query_parameters}
        assert params["unique_id"] == uid
        assert params["target_trend"] == "tswift engaged"


class TestWriteTrendsRaisesOnBqErrors:
    """A failed BigQuery insert (QueryJob.result() raises) must NOT be reported
    as success (silent data loss). Both write_trends_to_bq implementations must
    raise, matching write_eval_report_to_bq's contract, so ADK RetryConfig can
    retry and a genuine failure surfaces instead of masquerading as success."""

    def test_creative_agent_write_trends_raises(self, monkeypatch):
        """creative_agent relies on QueryJob.result() raising on a failed job."""
        from google.api_core.exceptions import BadRequest

        import creative_agent.bq_tools as t

        monkeypatch.setattr(
            t,
            "_get_bigquery_client",
            lambda: FakeBigQueryClient(result_error=BadRequest("boom")),
        )

        ctx = FakeToolContext()
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
        with pytest.raises(BadRequest):
            t.write_trends_to_bq(ctx)

    def test_trend_scout_write_trends_raises(self, monkeypatch):
        """trend_scout also relies on QueryJob.result() raising on a failed job."""
        from google.api_core.exceptions import BadRequest

        import trend_scout.tools as t

        monkeypatch.setattr(
            t,
            "_get_bigquery_client",
            lambda: FakeBigQueryClient(result_error=BadRequest("boom")),
        )
        # avoid the live max-date lookup used to build the insert SQL
        monkeypatch.setattr(t, "_get_gtrends_max_date", lambda: "2026-07-17")

        ctx = FakeToolContext()
        ctx.state.update(
            {
                "gcs_folder": "2026_07_13_run",
                "agent_output_dir": "trawler_output",
                "target_search_trends": {"target_search_trends": ["tswift engaged"]},
                "brand": "PRS",
                "target_audience": "musicians",
                "target_product": "SE CE24",
                "key_selling_points": "wide tonal range",
            }
        )
        with pytest.raises(BadRequest):
            asyncio.run(t.write_trends_to_bq(ctx))


class TestGetDailyGtrends:
    """The latest refresh's top terms; the date is bound as a query parameter."""

    def test_binds_max_date_parameter_and_saves_terms(self, monkeypatch):
        import trend_scout.tools as t

        queries = []

        class _Job:
            def to_dataframe(self):  # pragma: no cover - must not be used
                raise AssertionError("to_dataframe needs pandas; iterate rows")

            def result(self, timeout=None):
                # BigQuery Row objects support mapping access by column name.
                return [
                    {
                        "term": "b",
                        "refresh_date": datetime.date(2026, 10, 11),
                        "x": [{"rank": 1}],
                    },
                    {
                        "term": "a",
                        "refresh_date": datetime.date(2026, 10, 11),
                        "x": [{"rank": 2}],
                    },
                ]

        class _Client:
            def query(self, sql, job_config=None):
                queries.append((sql, job_config))
                return _Job()

        monkeypatch.setattr(t, "_get_bigquery_client", lambda: _Client())
        monkeypatch.setattr(t, "_get_gtrends_max_date", lambda: "10/11/2026")
        ctx = FakeToolContext()
        out = asyncio.run(t.get_daily_gtrends(ctx))

        (sql, job_config) = queries[-1]
        assert "10/11/2026" not in sql
        assert "PARSE_DATE('%m/%d/%Y', @max_date)" in sql
        params = {p.name: p.value for p in job_config.query_parameters}
        assert params == {"max_date": "10/11/2026"}
        assert ctx.state["raw_gtrends"] == ["b", "a"]
        # The public table's date, ISO (the trend digest's cache key).
        assert ctx.state["gtrends_refresh_date"] == "2026-10-11"
        # Markdown table: term / rank (1-based result order) / refresh_date.
        lines = out.splitlines()
        assert len(lines) == 4
        assert [c.strip() for c in lines[0].strip("|").split("|")] == [
            "",
            "term",
            "rank",
            "refresh_date",
        ]
        assert [c.strip() for c in lines[2].strip("|").split("|")] == [
            "1",
            "b",
            "1",
            "2026-10-11",
        ]
        assert [c.strip() for c in lines[3].strip("|").split("|")] == [
            "2",
            "a",
            "2",
            "2026-10-11",
        ]

    def test_max_date_reads_first_row_without_pandas(self, monkeypatch):
        import trend_scout.tools as t

        class _Job:
            def to_dataframe(self):  # pragma: no cover - must not be used
                raise AssertionError("to_dataframe needs pandas; iterate rows")

            def result(self, timeout=None):
                return iter([{"max_date": datetime.date(2026, 10, 11)}])

        class _Client:
            def query(self, sql, job_config=None):
                return _Job()

        monkeypatch.setattr(t, "_get_bigquery_client", lambda: _Client())
        assert t._get_gtrends_max_date() == "10/11/2026"
