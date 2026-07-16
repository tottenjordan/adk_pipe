"""Pure event-log parser for creative_agent latency runs.

Turns a run's serialized event log (the exact camelCase shape the async
``/runs`` poll endpoint emits — ``ev.model_dump(mode="json", by_alias=True,
exclude_none=True)``; see ``runserver/async_runs.py:_serialize_event``) plus its
final session-state dict into a per-phase wall-clock / model-call breakdown.

Every function here is pure and side-effect free so the whole thing is unit
tested against synthetic fixtures with no creds or network (see
``tests/test_experiment_parse.py``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# Terminal-marker constants mirror runserver/async_runs.py (kept local so this
# offline package has no import dependency on the server module).
RUNSERVER_AUTHOR = "__runserver__"
RUN_STATUS_KEY = "__run_status"


# --- phase rollup -----------------------------------------------------------
# Leaf agent authors roll up to a handful of pipeline phases. Exact names win
# first; a prefix fallback keeps us robust to retry-wrapper renames and new
# leaf agents that follow the existing naming scheme.
_EXACT_PHASES: dict[str, str] = {
    "user": "user",
    RUNSERVER_AUTHOR: "runserver",
    "root_agent": "orchestrator",
    "creative_eval_agent": "eval",
    "visual_generation_pipeline": "visual_concepts",
    "visual_production_pipeline": "image_gen",
}

# Order matters: more specific prefixes first (visual_concept before the
# broader visual_generator image bucket).
_PREFIX_PHASES: tuple[tuple[str, str], ...] = (
    ("gs_", "research"),
    ("ca_", "research"),
    ("campaign_", "research"),
    ("merge_", "research"),
    ("combined_", "research"),
    ("enhanced_combined_", "research"),
    ("refined_", "research"),
    ("parallel_planner", "research"),
    ("ad_copy", "ad_copy"),
    ("ad_creative", "ad_copy"),
    ("visual_concept", "visual_concepts"),
    ("visual_generat", "image_gen"),  # visual_generator, visual_generator_resilient
)

# Authors that are not model turns and must be excluded from model_call counts.
_NON_MODEL_AUTHORS = frozenset({"user", RUNSERVER_AUTHOR})

# --- tool-span phase attribution (the load-bearing one for a DEPLOYED run) ----
# Agent Engine does NOT stream leaf sub-agent events: every model turn is
# authored ``root_agent`` and each sub-pipeline runs as an AgentTool, surfacing
# only as a ``functionCall`` -> ``functionResponse`` pair on the root. So real
# per-phase wall-clock comes from the span between a heavy tool's call and its
# response, NOT from the (uniformly ``root_agent``) author. These are the
# creative_agent top-level tools; each is serial + singular, so first-call →
# first-response pairing gives an exact span. Anything not listed (e.g. the
# parallel ``memorize`` batch) stays in the ``orchestrator`` remainder.
_SPAN_TOOLS: dict[str, str] = {
    "combined_research_pipeline": "research",
    "ad_creative_pipeline": "ad_copy",
    "visual_production_pipeline": "visual",
    "creative_eval_agent": "eval",
    "save_draft_report_artifact": "persistence",
    "save_eval_report_to_gcs": "persistence",
    "save_creative_gallery_html": "persistence",
    "write_trends_to_bq": "persistence",
    "write_eval_report_to_bq": "persistence",
}


def phase_of(author: str) -> str:
    """Roll a leaf event author up to its pipeline phase. Unknown -> ``other``.

    Retained for ``model_calls`` and for any local/ADK-web run that DOES emit
    leaf authors; a deployed Agent Engine run authors everything ``root_agent``
    (rolls to ``orchestrator``) — use the tool spans for its phase breakdown.
    """
    a = author or ""
    if a in _EXACT_PHASES:
        return _EXACT_PHASES[a]
    for prefix, phase in _PREFIX_PHASES:
        if a.startswith(prefix):
            return phase
    return "other"


def phase_of_tool(name: str) -> str:
    """Phase a heavy AgentTool span belongs to. Unknown -> ``orchestrator``."""
    return _SPAN_TOOLS.get(name, "orchestrator")


@dataclass
class RunSummary:
    """Per-phase wall-clock / model-call breakdown of one run."""

    total_wall_s: float
    phase_wall_s: dict[str, float]
    model_calls: dict[str, int]
    tool_calls: Counter
    exhaustion: list[str]
    status: str
    event_count: int
    started_at_epoch: float | None = None
    extra: dict = field(default_factory=dict)


def summary_to_dict(summary: RunSummary) -> dict:
    """JSON-safe dict of a :class:`RunSummary`.

    NOT ``dataclasses.asdict``: that reconstructs the ``Counter`` field via
    ``Counter((k, v) for ...)``, which counts each ``(name, count)`` pair as an
    element and yields TUPLE keys that ``json.dumps`` rejects. Coerce the Counter
    to a plain ``{name: count}`` dict explicitly; everything else is already
    JSON-native.
    """
    return {
        "total_wall_s": summary.total_wall_s,
        "phase_wall_s": dict(summary.phase_wall_s),
        "model_calls": dict(summary.model_calls),
        "tool_calls": dict(summary.tool_calls),
        "exhaustion": list(summary.exhaustion),
        "status": summary.status,
        "event_count": summary.event_count,
        "started_at_epoch": summary.started_at_epoch,
        "extra": dict(summary.extra),
    }


def _timestamp(ev: dict) -> float | None:
    ts = ev.get("timestamp")
    return float(ts) if isinstance(ts, (int, float)) else None


def _parts(ev: dict) -> list[dict]:
    content = ev.get("content") or {}
    return content.get("parts") or []


def _is_model_turn(ev: dict) -> bool:
    """True when an event carries model output (text or a function call)."""
    if ev.get("author") in _NON_MODEL_AUTHORS:
        return False
    for part in _parts(ev):
        if part.get("text") or part.get("functionCall"):
            return True
    return False


def _function_call_names(ev: dict) -> list[str]:
    names = []
    for part in _parts(ev):
        fc = part.get("functionCall")
        if fc and fc.get("name"):
            names.append(fc["name"])
    return names


def _function_response_names(ev: dict) -> list[str]:
    names = []
    for part in _parts(ev):
        fr = part.get("functionResponse")
        if fr and fr.get("name"):
            names.append(fr["name"])
    return names


def _derive_status(events: list[dict]) -> str:
    """Last ``__run_status`` marker wins; absent one, the run is still running."""
    status = "running"
    for ev in events:
        delta = (ev.get("actions") or {}).get("stateDelta") or {}
        marker = delta.get(RUN_STATUS_KEY)
        if marker:
            status = marker
    return status


def summarize_run(events: list[dict], state: dict) -> RunSummary:
    """Reduce a serialized event log + final state into a :class:`RunSummary`.

    Timing model (deployed-run aware): a heavy sub-pipeline runs as an AgentTool,
    so its wall-clock is the span between its ``functionCall`` and its matching
    ``functionResponse`` (``_SPAN_TOOLS`` -> phase). The tail gap to the terminal
    ``__runserver__`` marker is ``runserver``; whatever total time is left over
    (root-LLM turns between tools, setup ``memorize``, small gaps) is
    ``orchestrator``. So ``sum(phase_wall_s.values()) == total_wall_s`` and the
    breakdown is faithful even though every event is authored ``root_agent``.
    """
    timed = [(ev, _timestamp(ev)) for ev in events]
    timed = [(ev, ts) for ev, ts in timed if ts is not None]
    timed.sort(key=lambda pair: pair[1])

    model_calls: dict[str, int] = {}
    tool_calls: Counter = Counter()
    for ev, _ts in timed:
        if _is_model_turn(ev):
            phase = phase_of(ev.get("author", ""))
            model_calls[phase] = model_calls.get(phase, 0) + 1
        tool_calls.update(_function_call_names(ev))

    # Heavy-tool spans: pair each call to the next response of the same name.
    phase_wall_s: dict[str, float] = {}
    open_starts: dict[str, list[float]] = {}
    for ev, ts in timed:
        for name in _function_call_names(ev):
            if name in _SPAN_TOOLS:
                open_starts.setdefault(name, []).append(ts)
        for name in _function_response_names(ev):
            starts = open_starts.get(name)
            if name in _SPAN_TOOLS and starts:
                phase = _SPAN_TOOLS[name]
                phase_wall_s[phase] = phase_wall_s.get(phase, 0.0) + (
                    ts - starts.pop(0)
                )

    total_wall_s = (timed[-1][1] - timed[0][1]) if len(timed) >= 2 else 0.0

    # Tail gap from the last real event to the terminal marker -> runserver.
    non_marker = [ts for ev, ts in timed if ev.get("author") != RUNSERVER_AUTHOR]
    markers = [ts for ev, ts in timed if ev.get("author") == RUNSERVER_AUTHOR]
    if markers and non_marker:
        tail = max(0.0, markers[-1] - non_marker[-1])
        if tail:
            phase_wall_s["runserver"] = phase_wall_s.get("runserver", 0.0) + tail

    # Everything not inside a tracked span is orchestrator (root-LLM) time.
    orchestrator = total_wall_s - sum(phase_wall_s.values())
    if orchestrator > 1e-9 or not phase_wall_s:
        phase_wall_s["orchestrator"] = phase_wall_s.get("orchestrator", 0.0) + max(
            0.0, orchestrator
        )

    started_at = timed[0][1] if timed else None
    exhaustion = sorted(k for k in state if k.endswith("__retry_exhausted"))

    return RunSummary(
        total_wall_s=total_wall_s,
        phase_wall_s=phase_wall_s,
        model_calls=model_calls,
        tool_calls=tool_calls,
        exhaustion=exhaustion,
        status=_derive_status(events),
        event_count=len(events),
        started_at_epoch=started_at,
    )
