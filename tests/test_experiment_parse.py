"""Offline tests for the creative_latency event-log parser (no creds, no network).

The parser turns a run's serialized event log (the exact camelCase shape the
async ``/runs`` poll endpoint emits) plus its final session state into a
per-phase wall-clock / model-call breakdown. These tests pin the phase rollup
and the timing arithmetic against a synthetic 3-phase run.
"""

import json

from experiments.creative_latency.parse_run import (
    RunSummary,
    phase_of,
    phase_of_tool,
    summarize_run,
    summary_to_dict,
)


class TestPhaseOf:
    def test_orchestrator(self):
        assert phase_of("root_agent") == "orchestrator"

    def test_research_leaf_authors(self):
        for author in (
            "gs_web_searcher",
            "campaign_web_synthesizer",
            "ca_sequential_planner",
            "merge_planners",
            "combined_web_evaluator",
            "enhanced_combined_searcher_resilient",
            "refined_web_synthesizer",
            "combined_report_composer",
            "parallel_planner_agent",
        ):
            assert phase_of(author) == "research", author

    def test_ad_copy(self):
        assert phase_of("ad_copy_drafter") == "ad_copy"
        assert phase_of("ad_copy_critic") == "ad_copy"

    def test_visual_concepts(self):
        assert phase_of("visual_concept_drafter") == "visual_concepts"
        assert phase_of("visual_concept_finalizer") == "visual_concepts"

    def test_image_gen(self):
        assert phase_of("visual_generator") == "image_gen"
        assert phase_of("visual_generator_resilient") == "image_gen"

    def test_eval(self):
        assert phase_of("creative_eval_agent") == "eval"

    def test_runserver_and_user(self):
        assert phase_of("__runserver__") == "runserver"
        assert phase_of("user") == "user"

    def test_unknown_author_is_other(self):
        assert phase_of("some_new_agent_we_forgot") == "other"


class TestPhaseOfTool:
    def test_heavy_pipelines_map_to_phases(self):
        assert phase_of_tool("combined_research_pipeline") == "research"
        assert phase_of_tool("ad_creative_pipeline") == "ad_copy"
        assert phase_of_tool("visual_production_pipeline") == "visual"
        assert phase_of_tool("creative_eval_agent") == "eval"

    def test_persistence_tools(self):
        for name in (
            "save_draft_report_artifact",
            "save_eval_report_to_gcs",
            "save_creative_gallery_html",
            "write_trends_to_bq",
            "write_eval_report_to_bq",
        ):
            assert phase_of_tool(name) == "persistence", name

    def test_untracked_tool_is_orchestrator(self):
        # e.g. the parallel `memorize` batch stays in the orchestrator remainder.
        assert phase_of_tool("memorize") == "orchestrator"


def _fc(name):
    return {"parts": [{"functionCall": {"name": name}}]}


def _fr(name):
    return {"parts": [{"functionResponse": {"name": name}}]}


def _text(t):
    return {"parts": [{"text": t}]}


# A synthetic DEPLOYED run: every model turn is authored ``root_agent`` and each
# sub-pipeline is an AgentTool (functionCall -> functionResponse span). Fixed
# timestamps make the span arithmetic exact:
#   research 20 | ad_copy 10 | visual 30 | eval 15 | persistence 2
#   runserver tail 4 | orchestrator remainder 4 | total 85
SYNTHETIC_EVENTS = [
    {"author": "user", "timestamp": 100.0, "content": _text("campaign")},
    {"author": "root_agent", "timestamp": 100.5, "actions": {"stateDelta": {}}},
    {
        "author": "root_agent",
        "timestamp": 101.0,
        "content": _fc("combined_research_pipeline"),
    },
    {
        "author": "root_agent",
        "timestamp": 121.0,
        "content": _fr("combined_research_pipeline"),
    },
    {
        "author": "root_agent",
        "timestamp": 121.5,
        "content": _fc("ad_creative_pipeline"),
    },
    {
        "author": "root_agent",
        "timestamp": 131.5,
        "content": _fr("ad_creative_pipeline"),
    },
    {
        "author": "root_agent",
        "timestamp": 132.0,
        "content": _fc("visual_production_pipeline"),
    },
    {
        "author": "root_agent",
        "timestamp": 162.0,
        "content": _fr("visual_production_pipeline"),
    },
    {"author": "root_agent", "timestamp": 162.5, "content": _fc("creative_eval_agent")},
    {"author": "root_agent", "timestamp": 177.5, "content": _fr("creative_eval_agent")},
    {
        "author": "root_agent",
        "timestamp": 178.0,
        "content": _fc("save_eval_report_to_gcs"),
    },
    {
        "author": "root_agent",
        "timestamp": 180.0,
        "content": _fr("save_eval_report_to_gcs"),
    },
    {"author": "root_agent", "timestamp": 181.0, "content": _text("all done")},
    {
        "author": "__runserver__",
        "timestamp": 185.0,
        "actions": {"stateDelta": {"__run_status": "done"}},
    },
]

SYNTHETIC_STATE = {
    "brand": "PRS Guitars",
    "info_gtrends__retry_exhausted": True,
}


class TestSummarizeRun:
    def _summary(self) -> RunSummary:
        return summarize_run(SYNTHETIC_EVENTS, SYNTHETIC_STATE)

    def test_total_wall_is_span(self):
        assert self._summary().total_wall_s == 85.0

    def test_phase_wall_sums_to_total(self):
        s = self._summary()
        assert abs(sum(s.phase_wall_s.values()) - s.total_wall_s) < 1e-6

    def test_phase_wall_from_tool_spans(self):
        # Each phase = its AgentTool's call->response span.
        s = self._summary()
        assert s.phase_wall_s["research"] == 20.0
        assert s.phase_wall_s["ad_copy"] == 10.0
        assert s.phase_wall_s["visual"] == 30.0
        assert s.phase_wall_s["eval"] == 15.0
        assert s.phase_wall_s["persistence"] == 2.0

    def test_runserver_tail_and_orchestrator_remainder(self):
        # Tail to the terminal marker is runserver; the rest is orchestrator.
        s = self._summary()
        assert s.phase_wall_s["runserver"] == 4.0
        assert s.phase_wall_s["orchestrator"] == 4.0

    def test_model_calls_authored_orchestrator_when_deployed(self):
        # All model turns are root_agent -> orchestrator; user/runserver excluded.
        s = self._summary()
        assert s.model_calls == {"orchestrator": 6}

    def test_tool_calls_counter_counts_pipelines(self):
        s = self._summary()
        assert s.tool_calls["combined_research_pipeline"] == 1
        assert s.tool_calls["visual_production_pipeline"] == 1
        assert s.tool_calls["save_eval_report_to_gcs"] == 1

    def test_exhaustion_detected(self):
        assert self._summary().exhaustion == ["info_gtrends__retry_exhausted"]

    def test_status_from_terminal_marker(self):
        assert self._summary().status == "done"

    def test_event_count(self):
        assert self._summary().event_count == 14

    def test_empty_events_is_safe(self):
        s = summarize_run([], {})
        assert s.total_wall_s == 0.0
        assert s.status == "running"
        assert s.event_count == 0

    def test_run_without_span_tools_is_all_orchestrator(self):
        # A local/ADK-web run (or trend_scout) with no tracked tool spans still
        # yields a valid breakdown: the whole span rolls into orchestrator.
        events = [
            {"author": "user", "timestamp": 0.0, "content": _text("hi")},
            {"author": "root_agent", "timestamp": 10.0, "content": _text("done")},
        ]
        s = summarize_run(events, {})
        assert s.phase_wall_s == {"orchestrator": 10.0}


class TestSummaryToDict:
    """`summary_to_dict` must yield a JSON-safe dict.

    Regression: ``dataclasses.asdict`` reconstructs the ``Counter`` field via
    ``Counter((k, v) for ...)``, which counts each ``(key, value)`` pair as an
    element and produces TUPLE keys, breaking ``json.dumps``. The helper must
    coerce ``tool_calls`` to a plain ``{name: count}`` dict instead.
    """

    def _d(self) -> dict:
        return summary_to_dict(summarize_run(SYNTHETIC_EVENTS, SYNTHETIC_STATE))

    def test_is_json_serializable(self):
        # Must not raise "keys must be str, int, float, bool or None, not tuple".
        json.dumps(self._d())

    def test_tool_calls_is_plain_str_keyed_dict(self):
        tc = self._d()["tool_calls"]
        assert tc["combined_research_pipeline"] == 1
        assert all(isinstance(k, str) for k in tc)

    def test_preserves_scalar_and_dict_fields(self):
        d = self._d()
        assert d["total_wall_s"] == 85.0
        assert d["status"] == "done"
        assert d["phase_wall_s"]["research"] == 20.0
        assert d["phase_wall_s"]["visual"] == 30.0
        assert d["exhaustion"] == ["info_gtrends__retry_exhausted"]
