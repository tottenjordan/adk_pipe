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


def phase_of(author: str) -> str:
    """Roll a leaf event author up to its pipeline phase. Unknown -> ``other``."""
    a = author or ""
    if a in _EXACT_PHASES:
        return _EXACT_PHASES[a]
    for prefix, phase in _PREFIX_PHASES:
        if a.startswith(prefix):
            return phase
    return "other"


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

    Timing model: events are timestamped at *emission* (after the model produced
    them), so each inter-event gap is the wait to produce the LATER event and is
    attributed to that event's phase. Every gap is attributed exactly once, so
    ``sum(phase_wall_s.values()) == total_wall_s``.
    """
    timed = [(ev, _timestamp(ev)) for ev in events]
    timed = [(ev, ts) for ev, ts in timed if ts is not None]
    timed.sort(key=lambda pair: pair[1])

    phase_wall_s: dict[str, float] = {}
    model_calls: dict[str, int] = {}
    tool_calls: Counter = Counter()

    for ev, _ts in timed:
        if _is_model_turn(ev):
            phase = phase_of(ev.get("author", ""))
            model_calls[phase] = model_calls.get(phase, 0) + 1
        tool_calls.update(_function_call_names(ev))

    # Inter-event gaps -> phase of the later event.
    for (_prev_ev, prev_ts), (next_ev, next_ts) in zip(timed, timed[1:]):
        phase = phase_of(next_ev.get("author", ""))
        phase_wall_s[phase] = phase_wall_s.get(phase, 0.0) + (next_ts - prev_ts)

    started_at = timed[0][1] if timed else None
    total_wall_s = (timed[-1][1] - timed[0][1]) if len(timed) >= 2 else 0.0

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
