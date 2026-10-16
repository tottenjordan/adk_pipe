"""The judge's gate and verdict rules (pure, no genai).

Reduces the judge's raw gates / verdicts to the expected sets
(:func:`normalize_gates`, :func:`normalize_verdicts`), decides the gates set
in code (no brief, no person cast, no trend, not timely, no safer angle),
computes ``gates_passed`` / the aggregate score (:func:`score_from_judge`)
and counts the gates the judge left out (:func:`unreported_gates_warning`).
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from .config import EvalConfig
from .dimensions import (
    ADVISORY_GATES,
    BRIEF_GATES,
    NO_GATES_GATE,
    PERSON_GATES,
    SAFER_ANGLE_GATES,
    TIMELY_GATES,
    TREND_GATES,
    VISUAL_GATES,
)
from .schemas import (
    AdCopyEvaluation,
    CreativeScore,
    EvalVerdict,
    GateResult,
    GateResultIn,
    JudgeScore,
    VisualConceptEvaluation,
)

logger = logging.getLogger(__name__)


NO_BRIEF_NOTE = "no brief"
# An advisory gate the judge left out: recorded as passed (it never blocks).
NOT_REPORTED_NOTE = "not checked (the judge did not report it)"
# A blocking gate the judge had to decide but left out: failed (unverified).
NOT_CHECKED_NOTE = "not checked"
NO_GATES_NOTE = "judge returned no gates"
NO_PERSON_CAST_NOTE = "no person cast"
# Note on a TREND_GATES gate when the run has no target trend.
NO_TREND_NOTE = "no trend"
# Note on a TIMELY_GATES gate for a creative that does not reference a
# validated timely moment.
NO_TIMELY_NOTE = "not timely"
# Note on a SAFER_ANGLE_GATES gate on a run without a safer angle.
NO_SAFER_ANGLE_NOTE = "no safer angle"


def safer_angle_active(state: Mapping[str, Any]) -> bool:
    """True on a safer-angle run: ``state["safer_angle_active"] == "yes"`` (pure).

    The user deliberately picked a sensitive trend and opted in to a
    respectful, positive angle on its subject; anything else is inactive.
    """
    return state.get("safer_angle_active") == "yes"


def safer_angle_reason(state: Mapping[str, Any]) -> str:
    """The run's ``safer_angle_sensitive_reason`` ("" unless a safer-angle run)."""
    if not safer_angle_active(state):
        return ""
    reason = state.get("safer_angle_sensitive_reason")
    return reason.strip() if isinstance(reason, str) else ""


def normalize_gates(
    raw: Sequence[GateResultIn | GateResult],
    expected: tuple[str, ...],
    *,
    brief_used: bool,
    person_cast: bool = False,
    trend_given: bool = True,
    timely: bool = False,
    safer_angle: bool = False,
) -> list[GateResult]:
    """The judge's gates, reduced to exactly ``expected`` in order (pure).

    Names are matched leniently (case, spaces and hyphens ignored); unknown
    names are dropped and duplicates keep the first. Only the gates the judge
    HAD to decide count as reported: blocking (not ``ADVISORY_GATES``) and not
    decided in code (below). When NONE of those was reported the judge
    skipped the checks: the result is a single failed ``NO_GATES_GATE`` gate
    (note "judge returned no gates"), so the creative cannot pass unverified
    (an advisory or code-decided gate alone never counts). A partial omission
    of such a gate fails it as "not checked" (unverified, counted into a
    report warning by :func:`unreported_gates_warning`); an omitted advisory
    gate passes with a "not checked (the judge did not report it)" note. Without a
    brief the brief-dependent gates pass with note "no brief", and unless
    ``person_cast`` the ``PERSON_GATES`` pass with note "no person cast";
    without a target trend (``trend_given`` False) the ``TREND_GATES`` pass
    with note "no trend" (decided in code, never the judge; they need no
    brief, so a missing brief never passes them). Unless ``timely`` (the
    creative references a validated timely moment, :func:`creative_is_timely`)
    the ``TIMELY_GATES`` pass with note "not timely", and unless
    ``safer_angle`` (a safer-angle run, :func:`safer_angle_active`) the
    ``SAFER_ANGLE_GATES`` pass with note "no safer angle". A person gate
    omitted for a cast concept, a timely gate omitted for a timely creative,
    or a safer-angle gate omitted on a safer-angle run, fails as "not checked"
    (unverified) like any other blocking gate. ``advisory`` is set from
    ``ADVISORY_GATES``, never the judge.
    """
    reported: dict[str, GateResultIn | GateResult] = {}
    for gate in raw:
        reported.setdefault(_norm_name(gate.gate), gate)

    def code_note(name: str) -> str | None:
        """The note of a gate decided in code (passed), else None."""
        if name in BRIEF_GATES and not brief_used:
            return NO_BRIEF_NOTE
        if name in PERSON_GATES and not person_cast:
            return NO_PERSON_CAST_NOTE
        if name in TREND_GATES and not trend_given:
            return NO_TREND_NOTE
        if name in TIMELY_GATES and not timely:
            return NO_TIMELY_NOTE
        if name in SAFER_ANGLE_GATES and not safer_angle:
            return NO_SAFER_ANGLE_NOTE
        return None

    required = [
        name
        for name in expected
        if name not in ADVISORY_GATES and code_note(name) is None
    ]
    if required and not any(name in reported for name in required):
        logger.warning("judge reported none of the required gates %s", required)
        return [GateResult(gate=NO_GATES_GATE, passed=False, note=NO_GATES_NOTE)]
    out = []
    for name in expected:
        advisory = name in ADVISORY_GATES
        note = code_note(name)
        if note is not None:
            out.append(GateResult(gate=name, passed=True, note=note))
        elif name in reported:
            gate = reported[name]
            out.append(
                GateResult(
                    gate=name,
                    passed=gate.passed,
                    note=" ".join(gate.note.split()),
                    advisory=advisory,
                )
            )
        elif advisory:
            logger.warning("judge did not report advisory gate %r", name)
            out.append(
                GateResult(
                    gate=name, passed=True, note=NOT_REPORTED_NOTE, advisory=True
                )
            )
        else:
            # A gate the judge had to decide: an omission fails (unverified).
            logger.warning("judge did not report required gate %r", name)
            out.append(GateResult(gate=name, passed=False, note=NOT_CHECKED_NOTE))
    return out


def _norm_name(name: str) -> str:
    """A judge-reported gate/dimension name, normalised for lenient matching."""
    return "_".join(name.strip().lower().replace("-", " ").split())


def normalize_verdicts(
    verdicts: Sequence[EvalVerdict], expected: Sequence[str]
) -> tuple[list[EvalVerdict], list[str]]:
    """The judge's verdicts reduced to ``expected`` in order, plus the missing (pure).

    Mirrors :func:`normalize_gates`: names are matched leniently (case,
    spaces and hyphens ignored) and rewritten to the expected name, unknown
    dimensions are dropped and duplicates keep the first. Returns
    ``(verdicts, missing)``; any missing dimension makes the score unverified
    (:func:`score_from_judge` fails ``passed``).
    """
    reported: dict[str, EvalVerdict] = {}
    for verdict in verdicts:
        reported.setdefault(_norm_name(verdict.dimension), verdict)
    kept = [
        reported[name].model_copy(update={"dimension": name})
        for name in expected
        if name in reported
    ]
    missing = [name for name in expected if name not in reported]
    if missing:
        logger.warning("judge did not score dimensions %s", missing)
    return kept, missing


def score_from_judge(
    judged: JudgeScore,
    expected_gates: tuple[str, ...],
    threshold: float,
    *,
    expected_dimensions: Sequence[str],
    brief_used: bool,
    person_cast: bool = False,
    trend_given: bool = True,
    timely: bool = False,
    safer_angle: bool = False,
) -> CreativeScore:
    """Map the judge's raw score to the report's ``CreativeScore`` (pure).

    The model's strengths/improvements text is kept; overall_score, gates,
    gates_passed and passed are computed here from its verdicts reduced to
    ``expected_dimensions`` (:func:`normalize_verdicts`) and its gates
    (:func:`normalize_gates`; the judge schema does not even carry them). A
    dimension the judge did not score is listed in ``unscored_dimensions``
    and the creative does not pass (the mean covers only the scored ones).
    """
    gates = normalize_gates(
        judged.gates,
        expected_gates,
        brief_used=brief_used,
        person_cast=person_cast,
        trend_given=trend_given,
        timely=timely,
        safer_angle=safer_angle,
    )
    verdicts, missing = normalize_verdicts(judged.verdicts, expected_dimensions)
    score = _score_from_verdicts(verdicts, threshold, gates)
    score.strengths = list(judged.strengths)
    score.improvements = list(judged.improvements)
    if missing:
        score.unscored_dimensions = missing
        score.passed = False
    return score


def moment_is_timely(trend_situation: Any) -> bool:
    """True when ``state["trend_situation"]`` is a validated timely moment (pure).

    ``timely_ok is True``: the same rule as the engine's guards
    (``creative_agent.trend_situation.timely_allowed``, mirrored with plain
    dict keys so creative_eval never imports creative_agent; parity-tested
    via :func:`run_is_timely`). A missing ``valid_until`` does not switch the
    check off: a creative the guards treated as timely is always judged for
    ``timely_claim_supported`` (the validator always sets one; the block then
    just shows it empty). Pass the run's moment via :func:`run_trend_situation`.
    """
    return (
        isinstance(trend_situation, Mapping)
        and trend_situation.get("timely_ok") is True
    )


def run_trend_situation(state: Mapping[str, Any]) -> Any:
    """The run's ``trend_situation`` for the judge, or None (pure).

    None when trend_scout flagged the trend sensitive
    (``state["trend_sensitive_hint"] == "yes"``): like the engine's
    ``timely_allowed``, such a run never has a timely moment.
    """
    if state.get("trend_sensitive_hint") == "yes":
        return None
    return state.get("trend_situation")


def run_is_timely(state: Mapping[str, Any]) -> bool:
    """True when the run has a timely moment (:func:`moment_is_timely` of
    :func:`run_trend_situation`); parity-tested against
    ``creative_agent.trend_situation.timely_allowed(state, 1)``."""
    return moment_is_timely(run_trend_situation(state))


def creative_is_timely(
    creative: Any, trend_situation: Any, *, paired_copy_timely: bool = False
) -> bool:
    """True when the judge must check this creative's timely claim (pure).

    The creative's own ``uses_trend_state is True`` (or, for a visual concept,
    its paired ad copy's: ``paired_copy_timely``) AND a validated timely moment
    (:func:`moment_is_timely`).
    """
    flagged = isinstance(creative, Mapping) and creative.get("uses_trend_state") is True
    return (flagged or paired_copy_timely) and moment_is_timely(trend_situation)


def timely_copy_ids(ad_copies: Sequence[Any]) -> frozenset[str]:
    """``original_id`` (as str) of every ad copy flagged ``uses_trend_state``.

    A visual concept pairs with its copy via ``ad_copy_id == original_id``
    (compared as strings, like ``concept_guard``).
    """
    return frozenset(
        str(ac.get("original_id"))
        for ac in ad_copies
        if isinstance(ac, Mapping)
        and ac.get("uses_trend_state") is True
        and ac.get("original_id") is not None
    )


def paired_copy_timely(concept: Any, copy_ids: frozenset[str]) -> bool:
    """True when ``concept``'s paired copy is in :func:`timely_copy_ids` (pure)."""
    if not isinstance(concept, Mapping) or concept.get("ad_copy_id") is None:
        return False
    return str(concept.get("ad_copy_id")) in copy_ids


def gates_passed(gates: list[GateResult]) -> bool:
    """True when every non-advisory gate passed (vacuously True with none).

    Computed in code — the judge's own aggregate is never trusted.
    """
    return all(g.passed for g in gates if not g.advisory)


def _score_from_verdicts(
    verdicts: list[EvalVerdict],
    threshold: float,
    gates: list[GateResult] | None = None,
) -> CreativeScore:
    """Compute an aggregate CreativeScore from individual verdicts and gates.

    ``passed`` = mean score (0-1) >= ``threshold`` AND ``gates_passed``.
    ``strengths`` / ``improvements`` stay empty: :func:`score_from_judge`
    fills them with the judge's own text.
    """
    gates = list(gates or [])
    gates_ok = gates_passed(gates)
    avg_score = (
        sum(v.score for v in verdicts) / (len(verdicts) * 10) if verdicts else 0.0
    )
    return CreativeScore(
        overall_score=round(avg_score, 3),
        passed=bool(verdicts) and avg_score >= threshold and gates_ok,
        verdicts=verdicts,
        strengths=[],
        improvements=[],
        gates=gates,
        gates_passed=gates_ok,
        judge_failed=False,
    )


def _trend_given(campaign_context: Mapping[str, Any]) -> bool:
    """True when the run has a target trend (else ``TREND_GATES`` pass in code)."""
    return bool(str(campaign_context.get("target_search_trend") or "").strip())


def _score_visual(
    judged: JudgeScore,
    config: EvalConfig,
    *,
    brief_used: bool,
    trend_given: bool,
    person_cast: bool,
    timely: bool,
    safer_angle: bool,
) -> CreativeScore:
    """The visual judge's raw score → the report's :class:`CreativeScore`."""
    return score_from_judge(
        judged,
        VISUAL_GATES,
        config.passing_threshold,
        expected_dimensions=config.visual_dimensions,
        brief_used=brief_used,
        trend_given=trend_given,
        person_cast=person_cast,
        timely=timely,
        safer_angle=safer_angle,
    )


def _count_line(count: int, kind: str, outcome: str) -> str:
    noun = "check" if count == 1 else "checks"
    return f"{count} {kind}{noun} not reported by the judge ({outcome} as not checked)"


def unreported_gates_warning(
    ad_evals: list[AdCopyEvaluation],
    visual_evals: list[VisualConceptEvaluation],
) -> list[str]:
    """The report ``warnings`` entries counting gates the judge left out ([] if none).

    An omitted advisory gate passes as "not checked" (see
    :func:`normalize_gates`); the first entry keeps that visible. An omitted
    gate the judge had to decide fails as "not checked": person, timely and
    safer-angle gates get their own entries, every other gate one shared
    entry.
    """
    gates = [g for e in [*ad_evals, *visual_evals] for g in e.score.gates]
    special = PERSON_GATES | TIMELY_GATES | SAFER_ANGLE_GATES
    unchecked = [g.gate for g in gates if g.note == NOT_CHECKED_NOTE and not g.passed]
    counts = [
        ("", "passed", sum(g.note == NOT_REPORTED_NOTE for g in gates)),
        ("", "failed", sum(name not in special for name in unchecked)),
        *(
            (kind, "failed", sum(name in names for name in unchecked))
            for kind, names in (
                ("person ", PERSON_GATES),
                ("timely ", TIMELY_GATES),
                ("safer-angle ", SAFER_ANGLE_GATES),
            )
        ),
    ]
    return [_count_line(n, kind, outcome) for kind, outcome, n in counts if n]
