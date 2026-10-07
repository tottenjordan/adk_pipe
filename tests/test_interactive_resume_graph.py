"""Offline pause/resume run of the REAL interactive_creative resumable App.

P2 T3: interactive_creative now exposes the shared creative_agent pipelines as
graph-Workflow NodeTools (inner events land in the parent session on branch
``<tool>@<fc_id>``) while its three review checkpoints stay
LongRunningFunctionTools. These tests drive the real ``interactive_creative.agent
.app`` through ``runserver.async_runs.start_run`` / ``start_resume`` (the exact
kick-off + resume path the frontend uses) with a real ``Runner`` and stub models,
checking what structure tests can't:

* the run pauses at checkpoint 1 (``review_research``) after the research
  NodeTool completed, and the resume's function response continues the root into
  the next pipeline tool without stalling;
* a pipeline node that fails once surfaces as an ``Error running node`` tool
  result; the root's retry re-runs the failed node and the run completes with
  the root's final text and nothing left paused (execution-plan Correction 3:
  the upstream node's re-execution count is CHARACTERIZED, not assumed);
* ``write_trends_to_bq`` invoked once before and once after the resume binds the
  same ``stable_row_id(session.id, trend)`` key both times (one logical row).
  It runs inside ``finalize_pipeline`` in production; the test adds it to the
  root's tools so the binding is checked without scripting the eval judge;
* after a checkpoint-3 resume the root's ``finalize_pipeline`` NodeTool runs
  (fake judge / GCS / BQ) and sets the ``finalize_done`` completion marker.
"""

import asyncio
import json
from typing import Any

import pytest
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from agent_common import drop_other_agent_context, stable_row_id
from agent_common.history import is_other_agent_context
from creative_agent.schemas import FinalAdCopyList
from runserver.async_runs import RUN_STATUS_KEY, start_resume, start_run
from tests._fake_bq import FakeBigQueryClient
from tests._fakes import RecordingLlm as _RecordingLlm
from tests._fakes import fc_response, text_response
from tests.test_creative_agent_graph import (
    _SEED,
    _llm_agents,
    _script_research,
    _stub_graph,
)

APP = "interactive_creative"
USER = "u"
SID = "sess-1"
TREND = "roadrunner"
# A schema-valid FinalAdCopyList (the critic has output_schema + SCHEMA_RETRY,
# so an invalid payload would be retried as a ValidationError).
_FINAL_AD = {
    "original_id": 1,
    "tone_style": "Humorous",
    "headline": "Beep beep",
    "body_text": "Outrun anything.",
    "trend_connection": "Roadrunner.",
    "audience_appeal_rationale": "Coyotes want speed.",
    "social_caption": "Zoom.",
    "call_to_action": "Skate now!",
    "detailed_performance_rationale": "Speed sells.",
}
_FINAL_ADS = {"ad_copies": [_FINAL_AD]}
# What ADK stores for it: the validated model, dumped with schema defaults.
_STORED_ADS = FinalAdCopyList.model_validate(_FINAL_ADS).model_dump(exclude_none=True)


class _FailOnceLlm(_RecordingLlm):
    """Raises on its first call (a non-transient pipeline failure), then scripts."""

    async def generate_content_async(self, llm_request: LlmRequest, stream=False):
        if not self.requests:
            self.requests.append(llm_request)
            raise RuntimeError("critic kaboom")
        async for r in super().generate_content_async(llm_request, stream):
            yield r


def _merge_params(sql: str, job_config: Any) -> list[Any]:
    """Every BQ write here is a MERGE; nothing to return (DML)."""
    assert "MERGE" in sql
    return []


@pytest.fixture(autouse=True)
def _offline_research_pdf(monkeypatch):
    """The research pipeline saves its PDF itself (save_research_pdf_node); keep
    that offline here (no PDF render, no GCS)."""
    from creative_agent import gcs_tools

    async def fake_save_pdf(ctx) -> dict:
        ctx.state["research_report_gcs_uri"] = "gs://bucket/report.pdf"
        return {"status": "success"}

    monkeypatch.setattr(gcs_tools, "save_draft_report_artifact", fake_save_pdf)


def _patch_root(monkeypatch: pytest.MonkeyPatch) -> _RecordingLlm:
    import interactive_creative.agent as ic

    root_llm = _RecordingLlm()
    monkeypatch.setattr(ic.root_agent, "model", root_llm)
    # The GCS/config-templated instruction + state loader are out of scope.
    monkeypatch.setattr(ic.root_agent, "instruction", "orchestrate")
    monkeypatch.setattr(ic.root_agent, "before_agent_callback", None)
    # Keep the history trim (agent_common.history), drop only the rate limiter.
    monkeypatch.setattr(
        ic.root_agent, "before_model_callback", [drop_other_agent_context]
    )
    return root_llm


def _seed_state() -> dict[str, Any]:
    return {
        **_SEED,
        "target_search_trends": TREND,
        "gcs_folder": "2026_09_29_run",
        "agent_output_dir": "interactive_output",
    }


async def _session(svc: InMemorySessionService):
    session = await svc.get_session(app_name=APP, user_id=USER, session_id=SID)
    assert session is not None
    return session


def _responses(events: list[Event]) -> dict[str, Any]:
    return {
        fr.id: fr.response for e in events for fr in e.get_function_responses() or []
    }


def _long_running_ids(events: list[Event]) -> set[str]:
    return {i for e in events for i in (e.long_running_tool_ids or ())}


def _answered_ids(events: list[Event]) -> set[str]:
    return {fr.id for e in events for fr in e.get_function_responses() or [] if fr.id}


def _final_texts(events: list[Event]) -> list[str]:
    return [
        p.text
        for e in events
        if e.author == "root_agent" and e.content and e.content.parts
        for p in e.content.parts
        if p.text
    ]


def _run_paused_then_resumed(
    monkeypatch: pytest.MonkeyPatch, retry_in_new_message: bool = False
):
    """Checkpoint-1 pause → resume → ad pipeline fails once → root retries.

    ``retry_in_new_message=False``: the root retries the failed tool within the
    resumed turn. ``True``: the root ends the resumed turn after the failure and
    the retry arrives in a fresh user message (a new invocation)."""
    import creative_agent.bq_tools as bq_tools
    import interactive_creative.agent as ic

    root_llm = _patch_root(monkeypatch)
    research = _stub_graph(monkeypatch, ic.combined_research_pipeline)
    _script_research(research, ["CA INSIGHTS"])
    ads = _stub_graph(monkeypatch, ic.ad_creative_pipeline)
    (critic,) = [
        a for a in _llm_agents(ic.ad_creative_pipeline) if a.name == "ad_copy_critic"
    ]
    critic_llm = _FailOnceLlm()
    monkeypatch.setattr(critic, "model", critic_llm)
    # Scripted generously: the drafter's re-execution count is what the
    # failure test characterizes (the StubLlm asserts on an unscripted call).
    ads["ad_copy_drafter"].push(
        text_response('{"ad_copies": []}'), text_response('{"ad_copies": []}')
    )
    critic_llm.push(text_response(json.dumps(_FINAL_ADS)))

    bq = FakeBigQueryClient(_merge_params)
    monkeypatch.setattr(bq_tools, "_get_bigquery_client", lambda: bq)
    # write_trends_to_bq now runs inside finalize_pipeline, not as a root tool;
    # expose it directly here so the at-least-once key binding across a resume
    # is still characterized on the real resumable App without the judge.
    monkeypatch.setattr(
        ic.root_agent, "tools", [*ic.root_agent.tools, bq_tools.write_trends_to_bq]
    )

    # Segment 1: research NodeTool → BQ write → checkpoint 1 (pause).
    root_llm.push(
        fc_response("combined_research_pipeline", {"request": "go"}, "fc-research"),
        fc_response("write_trends_to_bq", {}, "fc-bq1"),
        fc_response("review_research", {}, "fc-cp1"),
    )
    # Segment 2 (resume): BQ write again (at-least-once) → ad NodeTool whose
    # critic fails once → root retries the tool → final text.
    root_llm.push(
        fc_response("write_trends_to_bq", {}, "fc-bq2"),
        fc_response("ad_creative_pipeline", {"request": "go"}, "fc-ads1"),
    )
    if retry_in_new_message:
        root_llm.push(text_response("ADS FAILED"))
    root_llm.push(
        fc_response("ad_creative_pipeline", {"request": "retry"}, "fc-ads2"),
        text_response("ROOT DONE"),
    )

    def runner_factory(app_name: str) -> Runner:
        assert app_name == APP
        return Runner(app=ic.app, session_service=svc)

    svc = InMemorySessionService()

    async def go():
        await svc.create_session(
            app_name=APP, user_id=USER, session_id=SID, state=_seed_state()
        )
        _, task = await start_run(
            app_name=APP,
            user_id=USER,
            session_id=SID,
            message="go",
            session_service=svc,
            runner_factory=runner_factory,
        )
        await task
        paused = await _session(svc)
        seg1 = list(paused.events)
        # Snapshot before the resume (the lists keep growing).
        mid = {
            "root_calls": root_llm.calls,
            "drafter_calls": ads["ad_copy_drafter"].calls,
            "state": dict(paused.state),
        }
        _, task = await start_resume(
            app_name=APP,
            user_id=USER,
            session_id=SID,
            function_call_id="fc-cp1",
            function_name="review_research",
            response={"status": "approved", "instruction": "continue"},
            session_service=svc,
            runner_factory=runner_factory,
        )
        await task
        if retry_in_new_message:
            _, task = await start_run(
                app_name=APP,
                user_id=USER,
                session_id=SID,
                message="retry the ad copies",
                session_service=svc,
                runner_factory=runner_factory,
            )
            await task
        final = await _session(svc)
        return seg1, mid, list(final.events), dict(final.state), final.id

    seg1, mid, events, state, session_id = asyncio.run(go())
    return {
        "seg1": seg1,
        "mid": mid,
        "events": events,
        "state": state,
        "session_id": session_id,
        "root_llm": root_llm,
        "research": research,
        "ads": ads,
        "critic_llm": critic_llm,
        "bq_params": [
            {p.name: p.value for p in job_config.query_parameters}
            for _, job_config in bq.queries
        ],
    }


def test_resumed_root_sees_the_checkpoint_response_but_no_sub_agent_turns(
    monkeypatch,
):
    """The history trim keeps the checkpoint's (user-authored) function
    response and the root's own call/response pairs, and drops the research
    pipeline's replayed sub-agent turns and node inputs."""
    r = _run_paused_then_resumed(monkeypatch)
    resumed = r["root_llm"].requests[r["mid"]["root_calls"]]

    responses = [
        p.function_response.name
        for c in resumed.contents
        for p in c.parts or []
        if p.function_response
    ]
    assert responses == [
        "combined_research_pipeline",
        "write_trends_to_bq",
        "review_research",
    ]
    assert not [c for c in resumed.contents if is_other_agent_context(c)]
    user_texts = [
        p.text
        for c in resumed.contents
        if c.role == "user"
        for p in c.parts or []
        if p.text
    ]
    assert user_texts == ["go"]


def test_real_app_pauses_at_checkpoint_1_then_resumes_into_next_pipeline(
    monkeypatch,
):
    r = _run_paused_then_resumed(monkeypatch)
    seg1, events, state = r["seg1"], r["events"], r["state"]

    # Segment 1: the research NodeTool answered (truthy terminal), then the run
    # paused on the long-running checkpoint with it unanswered.
    seg1_responses = _responses(seg1)
    assert "Research report complete" in str(seg1_responses["fc-research"])
    assert "fc-cp1" in _long_running_ids(seg1)
    assert "fc-cp1" not in _answered_ids(seg1)
    # NodeTool is itself long-running, so the pipeline's function-call event is
    # ALSO flagged in long_running_tool_ids; it differs from a checkpoint only
    # in being answered in the same segment. A pause detector must therefore
    # look for an UNANSWERED long-running id (the frontend does: see
    # frontend/src/lib/pause-detection.ts), never just "a long-running call".
    assert "fc-research" in _long_running_ids(seg1)
    assert "fc-research" in _answered_ids(seg1)
    assert _long_running_ids(seg1) - _answered_ids(seg1) == {"fc-cp1"}
    assert r["mid"]["root_calls"] == 3  # research, bq, checkpoint → paused
    assert r["mid"]["drafter_calls"] == 0  # nothing ran past the checkpoint
    assert r["mid"]["state"][RUN_STATUS_KEY] == "done"  # segment marker
    # Inner pipeline events landed in the PARENT session on the tool's branch,
    # authored by the inner agents (not root_agent).
    inner = [e for e in seg1 if e.author == "combined_report_composer"]
    assert inner and all(
        (e.branch or "").startswith("combined_research_pipeline@") for e in inner
    )

    # Segment 2: the resume continued straight into the ad pipeline tool and
    # the run completed with the root's final text, nothing left paused.
    assert "fc-cp1" in _answered_ids(events)
    assert _long_running_ids(events) - _answered_ids(events) == set()
    assert _final_texts(events)[-1] == "ROOT DONE"
    assert state["ad_copy_critique"] == _STORED_ADS
    assert state[RUN_STATUS_KEY] == "done"
    assert "__run_error" not in state


@pytest.mark.parametrize(
    ("retry_in_new_message", "drafter_runs"),
    [(False, 1), (True, 2)],
    ids=["retry_same_turn", "retry_next_message"],
)
def test_failed_pipeline_node_reruns_after_resume(
    monkeypatch, retry_in_new_message, drafter_runs
):
    r = _run_paused_then_resumed(monkeypatch, retry_in_new_message)
    events = r["events"]
    responses = _responses(events)

    assert "Error running node" in str(responses["fc-ads1"])
    # The pipeline returns a short confirmation; the copies themselves are in state.
    assert "Ad copies complete" in responses["fc-ads2"]["result"]
    # The failed node re-ran: one raising call + one successful call.
    assert len(r["critic_llm"].requests) == 2
    assert r["critic_llm"].calls == 1  # StubLlm counter: successful pops only
    # CHARACTERIZATION (Correction 3, ADK 2.10) of the upstream drafter:
    # * retry in the same (resumed) turn -> ran ONCE: the resume re-uses the
    #   paused invocation id, so both tool calls share one invocation and the
    #   completed drafter is replayed; only the failed critic re-executes (same
    #   rule as test_nodetool_failure_characterization).
    # * retry in the next user message -> ran TWICE: a new invocation re-executes
    #   the whole pipeline (test_nodetool_repeat_call_replay_scope).
    assert r["ads"]["ad_copy_drafter"].calls == drafter_runs
    root_invocations = [e.invocation_id for e in events if e.author == "root_agent"]
    assert len(set(root_invocations)) == (2 if retry_in_new_message else 1)
    # Research ran exactly once overall: resuming did not re-run the
    # already-answered research NodeTool.
    assert r["research"]["combined_report_composer"].calls == 1
    # The run completes with the root's final text and nothing left paused.
    assert _final_texts(events)[-1] == "ROOT DONE"
    assert _long_running_ids(events) - _answered_ids(events) == set()
    assert r["state"][RUN_STATUS_KEY] == "done"
    assert r["root_llm"].calls == (8 if retry_in_new_message else 7)


def test_bq_write_across_resume_binds_one_logical_key(monkeypatch):
    r = _run_paused_then_resumed(monkeypatch)
    params = r["bq_params"]

    assert len(params) == 2  # at-least-once: invoked before AND after resume
    expected = stable_row_id(r["session_id"], TREND)
    assert [p["unique_id"] for p in params] == [expected, expected]
    assert r["state"]["creative_row_uuid"] == expected


def test_finalize_pipeline_runs_after_checkpoint_3_resume(monkeypatch):
    """The real resumable App: pause at checkpoint 3, resume, then the root's
    finalize_pipeline NodeTool evaluates + persists inside the resumed
    invocation and sets the auto-continue completion marker."""
    import creative_eval.agent as eval_agent
    import interactive_creative.agent as ic
    from creative_agent import bq_tools, gcs_tools
    from tests._fakes import FakeStorageClient
    from tests.test_creative_agent_graph import _FINALIZE_STATE, _fake_judge

    root_llm = _patch_root(monkeypatch)
    monkeypatch.setattr(eval_agent, "evaluate_all_concurrently", _fake_judge)
    storage = FakeStorageClient([])
    monkeypatch.setattr(gcs_tools, "_get_gcs_client", lambda: storage)
    bq = FakeBigQueryClient()
    monkeypatch.setattr(bq_tools, "_get_bigquery_client", lambda: bq)

    root_llm.push(fc_response("review_visual_concepts", {}, "fc-cp3"))
    root_llm.push(
        fc_response("finalize_pipeline", {"request": "go"}, "fc-fin"),
        text_response("ROOT DONE"),
    )
    svc = InMemorySessionService()

    def runner_factory(app_name: str) -> Runner:
        return Runner(app=ic.app, session_service=svc)

    async def go():
        await svc.create_session(
            app_name=APP,
            user_id=USER,
            session_id=SID,
            state={**_seed_state(), **_FINALIZE_STATE},
        )
        common = {
            "app_name": APP,
            "user_id": USER,
            "session_id": SID,
            "session_service": svc,
            "runner_factory": runner_factory,
        }
        _, task = await start_run(message="go", **common)
        await task
        paused = await _session(svc)
        mid_state = dict(paused.state)
        _, task = await start_resume(
            function_call_id="fc-cp3",
            function_name="review_visual_concepts",
            response={"status": "approved", "instruction": "continue"},
            **common,
        )
        await task
        final = await _session(svc)
        return mid_state, list(final.events), dict(final.state)

    mid_state, events, state = asyncio.run(go())

    assert "finalize_done" not in mid_state  # paused before finalize
    assert "fc-cp3" in _answered_ids(events)
    assert _long_running_ids(events) - _answered_ids(events) == set()
    assert "Evaluation complete" in str(_responses(events)["fc-fin"])
    assert _final_texts(events)[-1] == "ROOT DONE"
    assert state["finalize_done"] is True
    assert state["creative_evaluation_report"]
    assert state["eval_report_gcs_uri"] and state["eval_bq_row_uuid"]
    assert not [k for k in state if k.endswith(("__issues", "__retry_exhausted"))]
    assert state[RUN_STATUS_KEY] == "done"
    assert len(bq.sqls) == 2  # trend_creatives + creative_evals rows
