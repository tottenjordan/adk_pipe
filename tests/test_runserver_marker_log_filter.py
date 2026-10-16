"""The api drops ADK's "unknown agent" warning for runserver's own markers only.

Every resume replays the session through ``Runner._find_agent_to_run``, which
scans events newest-first by author and warns about any author outside the agent
tree. runserver's run-status / edit markers (``author="__runserver__"``) are such
events by design, so each resume logged a spurious WARNING. The filter installed
by ``deployment/async_app.py`` drops exactly that record; an unknown-agent warning
for any other author (a real stale or foreign agent name) still logs.
"""

from __future__ import annotations

import logging

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents._agent_router import find_agent_to_run
from google.adk.events import Event, EventActions
from google.adk.sessions import Session

from runserver.async_runs import (
    ADK_AGENT_ROUTER_LOGGER,
    RUNSERVER_AUTHOR,
    RunserverMarkerLogFilter,
    build_terminal_event,
    install_runserver_marker_log_filter,
)

UNKNOWN = "Event from an unknown agent"


@pytest.fixture
def router_logger():
    """The ADK router logger with the filter installed, restored afterwards."""
    logger = logging.getLogger(ADK_AGENT_ROUTER_LOGGER)
    saved = list(logger.filters)
    install_runserver_marker_log_filter()
    yield logger
    logger.filters[:] = saved


def _session(*events: Event) -> Session:
    return Session(
        id="s1", app_name="creative_agent", user_id="u1", events=list(events)
    )


def _unknown_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == ADK_AGENT_ROUTER_LOGGER and r.getMessage().startswith(UNKNOWN)
    ]


def test_logger_name_matches_the_installed_adk_router():
    from google.adk.agents import _agent_router

    assert _agent_router.logger.name == ADK_AGENT_ROUTER_LOGGER


def test_routing_past_a_runserver_marker_logs_no_unknown_agent_warning(
    router_logger, caplog
):
    root = LlmAgent(name="root_agent", model="gemini-2.5-flash")
    session = _session(
        Event(author="root_agent", invocation_id="i1"),
        build_terminal_event("done", invocation_id="i1"),
    )
    with caplog.at_level(logging.WARNING, logger=ADK_AGENT_ROUTER_LOGGER):
        agent = find_agent_to_run(session, root)
    assert agent is root
    assert _unknown_warnings(caplog) == []


def test_other_unknown_authors_still_warn(router_logger, caplog):
    root = LlmAgent(name="root_agent", model="gemini-2.5-flash")
    session = _session(
        Event(author="root_agent", invocation_id="i1"),
        Event(author="stale_agent", invocation_id="i1"),
        Event(
            author=RUNSERVER_AUTHOR,
            invocation_id=RUNSERVER_AUTHOR,
            actions=EventActions(state_delta={"k": 1}),
        ),
    )
    with caplog.at_level(logging.WARNING, logger=ADK_AGENT_ROUTER_LOGGER):
        agent = find_agent_to_run(session, root)
    assert agent is root
    records = _unknown_warnings(caplog)
    assert [r.args[0] for r in records] == ["stale_agent"]


def _record(msg: str, args: tuple, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord(
        ADK_AGENT_ROUTER_LOGGER, level, __file__, 1, msg, args, None
    )


def test_filter_matches_only_the_exact_marker_warning():
    f = RunserverMarkerLogFilter()
    fmt = "Event from an unknown agent: %s, event id: %s"
    assert f.filter(_record(fmt, (RUNSERVER_AUTHOR, "e1"))) is False
    assert f.filter(_record(fmt, ("someone_else", "e1"))) is True
    # Another message that happens to carry the marker author is kept.
    assert f.filter(_record("Other thing: %s, %s", (RUNSERVER_AUTHOR, "e1"))) is True
    # Malformed / missing args never raise and are kept.
    assert f.filter(_record(fmt, ())) is True
    assert f.filter(_record("plain message", ())) is True


def test_install_is_idempotent(router_logger):
    install_runserver_marker_log_filter()
    install_runserver_marker_log_filter()
    assert (
        sum(isinstance(f, RunserverMarkerLogFilter) for f in router_logger.filters) == 1
    )
