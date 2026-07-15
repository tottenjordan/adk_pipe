"""Offline tests for the creative_latency event-log parser (no creds, no network).

The parser turns a run's serialized event log (the exact camelCase shape the
async ``/runs`` poll endpoint emits) plus its final session state into a
per-phase wall-clock / model-call breakdown. These tests pin the phase rollup
and the timing arithmetic against a synthetic 3-phase run.
"""

from experiments.creative_latency.parse_run import (
    RunSummary,
    phase_of,
    summarize_run,
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


def _content(*, text=None, function_call=None):
    parts = []
    if text is not None:
        parts.append({"text": text})
    if function_call is not None:
        parts.append({"functionCall": {"name": function_call}})
    return {"parts": parts}


# A synthetic run: user -> orchestrator -> research (2 events) -> ad_copy ->
# terminal done marker. Timestamps are fixed so the arithmetic is exact.
SYNTHETIC_EVENTS = [
    {"author": "user", "timestamp": 100.0, "content": _content(text="campaign")},
    {"author": "root_agent", "timestamp": 102.0, "content": _content(text="plan")},
    {
        "author": "gs_web_searcher",
        "timestamp": 107.0,
        "content": _content(function_call="google_search"),
    },
    {"author": "merge_planners", "timestamp": 109.0, "content": _content(text="brief")},
    {"author": "ad_copy_drafter", "timestamp": 115.0, "content": _content(text="copy")},
    {
        "author": "__runserver__",
        "timestamp": 115.0,
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
        assert self._summary().total_wall_s == 15.0

    def test_phase_wall_sums_to_total(self):
        s = self._summary()
        assert sum(s.phase_wall_s.values()) == s.total_wall_s

    def test_phase_wall_attribution(self):
        # Each inter-event gap belongs to the phase of the LATER event (the wait
        # to *produce* that event). Orchestrator: 102-100=2. Research: (107-102)
        # + (109-107) = 7. Ad copy: 115-109 = 6. Terminal marker gap: 0.
        s = self._summary()
        assert s.phase_wall_s["orchestrator"] == 2.0
        assert s.phase_wall_s["research"] == 7.0
        assert s.phase_wall_s["ad_copy"] == 6.0

    def test_model_calls_per_phase(self):
        # Model calls = events with text/functionCall content, excluding the
        # user turn and the runserver marker.
        s = self._summary()
        assert s.model_calls["orchestrator"] == 1
        assert s.model_calls["research"] == 2
        assert s.model_calls["ad_copy"] == 1
        assert "user" not in s.model_calls
        assert "runserver" not in s.model_calls

    def test_tool_calls_counter(self):
        assert self._summary().tool_calls["google_search"] == 1

    def test_exhaustion_detected(self):
        assert self._summary().exhaustion == ["info_gtrends__retry_exhausted"]

    def test_status_from_terminal_marker(self):
        assert self._summary().status == "done"

    def test_event_count(self):
        assert self._summary().event_count == 6

    def test_empty_events_is_safe(self):
        s = summarize_run([], {})
        assert s.total_wall_s == 0.0
        assert s.status == "running"
        assert s.event_count == 0
