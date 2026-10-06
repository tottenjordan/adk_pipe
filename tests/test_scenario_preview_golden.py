"""Golden fixture for the Deploy panel's live preview (dev group: imports JAX).

``frontend/src/lib/scenario-preview.ts`` is a noise-free TypeScript port of
``bandit.environment.build_true_model``. This test builds the real ground truth
with ``bandit`` (``noise_scale=0`` through ``apply_scenario_overrides``) for fixed
arm scores × the three scenarios × several override combinations, takes the
**exact** expected click rate per creative × segment (enumerating every ``ctx-v1``
context level combination with the segment's level probabilities, so the only
Monte Carlo left is ``build_true_model``'s own α calibration sample), and
compares it with the committed fixture
``frontend/src/__tests__/fixtures/scenario-preview-golden.json``. The Vitest
suite (``scenario-preview.test.ts``) then asserts the TS port reproduces the
fixture.

**Scripted shifts (contracts §10)** get their own fixture,
``frontend/src/__tests__/fixtures/scenario-shifts-golden.json``, for the
preview's ``applyShifts`` port (PR D). Format::

    {"_generated": str, "calibrationSamples": int,
     "cases": [{
       "name", "scenario", "ctrMode", "horizon", "arms", "overrides",  # as above
       "shifts": [...],             # REST camelCase (§10), creativeId = "arm-<i>" | "leader"
       "expected": {
         "segments": [str],         # scenario segment names
         "alpha": float,            # never recalibrated by shifts
         "resolved": [{             # time order (stable on atFrac)
           "index": int,            # position in "shifts"
           "kind": str, "round": int, "endRound": int | null,
           "creativeIndex": int | null,   # concrete arm ("leader" resolved)
           "segment": str | null,
           "segmentWeights": [float] | null,  # mix only (renormalised)
           "targets": [{"segment", "ctrBefore", "ctrAfter",
                        "bestOtherCtr"?, "logitOffset"?}]  # segment-level σ(V)
         }],
         "regimes": [{              # [start, end) between every shift / shock-end round
           "start": int, "end": int,
           "active": [int],         # "shifts" indices in effect (shocks only inside their window)
           "segmentWeights": [float],
           "ctr": [[float]],        # creative × segment exact expected CTR
           "oracle": [int],         # best creative per segment
           "overall": [float]       # segmentWeights-weighted
         }]}}]}

Resolution happens at the segment level (σ of the logit at the segment's mean
context, see ``bandit.environment._resolve_shifts``); ``ctr`` is the exact
expectation with the resolved offsets / multipliers applied. Drift is not
covered: the preview shows the pre-drift truth.

Regenerate after a deliberate model change::

    UPDATE_PREVIEW_GOLDEN=1 GOOGLE_CLOUD_PROJECT=test-project \
        uv run pytest tests/test_scenario_preview_golden.py -q
"""

from __future__ import annotations

import functools
import itertools
import json
import os
from pathlib import Path

import numpy as np
import pytest

jax = pytest.importorskip("jax")

from bandit import features  # noqa: E402
from bandit.config import (  # noqa: E402
    ArmSpec,
    ExperimentConfig,
    ScenarioOverrides,
    ShiftSpec,
    apply_scenario_overrides,
    load_scenario,
    shifts_from_dict,
)
from bandit.environment import (  # noqa: E402
    P_MAX,
    build_true_model,
    resolved_shifts,
)

ROOT = Path(__file__).resolve().parent.parent
# Every test here builds (or reuses the cached) exact-expectation fixtures.
pytestmark = pytest.mark.slow

FIXTURE_DIR = ROOT / "frontend" / "src" / "__tests__" / "fixtures"
FIXTURE = FIXTURE_DIR / "scenario-preview-golden.json"
SHIFT_FIXTURE = FIXTURE_DIR / "scenario-shifts-golden.json"
CALIBRATION_SAMPLES = 400_000
#: CTR tolerance between a fresh bandit run and the committed fixture (JAX's
#: float32 sampling is deterministic per key; this only absorbs platform ulps).
REPRO_TOL = 1e-6

# Arm scores shaped like the api's §5 arms (snapshot_arms): judge dimensions in
# [0, 1] plus ad_copy_overall / visual_overall (``overall`` = their mean).
ARM_SETS: dict[str, list[dict[str, float]]] = {
    "three": [
        {
            "ad_copy_overall": 0.86,
            "visual_overall": 0.78,
            "trend_authenticity": 0.9,
            "audience_fit": 0.7,
            "stopping_power": 0.8,
            "clarity": 0.9,
        },
        {
            "ad_copy_overall": 0.72,
            "visual_overall": 0.8,
            "trend_authenticity": 0.6,
            "audience_fit": 0.9,
            "stopping_power": 0.6,
        },
        {
            "ad_copy_overall": 0.64,
            "visual_overall": 0.7,
            "trend_authenticity": 0.7,
            "audience_fit": 0.5,
            "stopping_power": 0.9,
        },
    ],
    "four": [
        {"ad_copy_overall": 0.82, "visual_overall": 0.8, "trend_authenticity": 0.9},
        {"ad_copy_overall": 0.74, "visual_overall": 0.7, "audience_fit": 0.95},
        {"ad_copy_overall": 0.7, "visual_overall": 0.62, "stopping_power": 1.0},
        {"ad_copy_overall": 0.66, "visual_overall": 0.75},
    ],
    "two": [
        {"ad_copy_overall": 0.8, "visual_overall": 0.7, "stopping_power": 0.6},
        {"ad_copy_overall": 0.7, "visual_overall": 0.76, "stopping_power": 0.9},
    ],
}

# (label, camelCase overrides as the frontend sends them)
OVERRIDE_COMBOS: list[tuple[str, dict]] = [
    ("preset", {}),
    ("judge_backwards_gap_wide", {"judgeWrong": 1.0, "gapScale": 2.0}),
    ("judge_partial_gap_subtle", {"judgeWrong": 0.3, "gapScale": 0.25}),
]
SKEWED_MIX = {3: [0.7, 0.2, 0.1], 4: [0.55, 0.05, 0.3, 0.1]}


def _cases() -> list[dict]:
    cases = []
    for scenario in ("clear_winner", "segment_winners", "drift"):
        n_seg = len(load_scenario(scenario).segments)
        for (arm_set, arms), (label, ov) in itertools.product(
            ARM_SETS.items(), OVERRIDE_COMBOS
        ):
            if label != "preset" and (arm_set == "two" or scenario == "drift"):
                continue  # drift's pre-drift truth is clear_winner's
            cases.append(
                {
                    "name": f"{scenario}/{arm_set}/{label}",
                    "scenario": scenario,
                    "ctrMode": "demo",
                    "arms": arms,
                    "overrides": ov,
                }
            )
        if scenario != "drift":  # flip == 0: ties broken by creative order
            cases.append(
                {
                    "name": f"{scenario}/three/judge_uninformative",
                    "scenario": scenario,
                    "ctrMode": "demo",
                    "arms": ARM_SETS["three"],
                    "overrides": {"judgeWrong": 0.5},
                }
            )
        mix_ov = {"segmentMix": SKEWED_MIX[n_seg], "judgeWrong": 0.8}
        if scenario == "drift":
            mix_ov["driftAtFrac"] = 0.3
        cases.append(
            {
                "name": f"{scenario}/three/skewed_mix_realistic",
                "scenario": scenario,
                "ctrMode": "realistic",
                "arms": ARM_SETS["three"],
                "overrides": mix_ov,
            }
        )
    return cases


def _to_snake(ov: dict) -> ScenarioOverrides:
    return ScenarioOverrides(
        segment_mix=tuple(ov["segmentMix"]) if "segmentMix" in ov else None,
        gap_scale=ov.get("gapScale"),
        judge_wrong=ov.get("judgeWrong"),
        noise_scale=0.0,  # the preview is "before random variation"
        drift_at_frac=ov.get("driftAtFrac"),
    )


def _all_levels() -> np.ndarray:
    """Every ctx-v1 level combination, (N, G)."""
    sizes = [len(levels) for _, levels in features.CONTEXT_SPEC]
    return np.array(list(itertools.product(*(range(n) for n in sizes))), np.int32)


def _build_env(case: dict, shifts: tuple[ShiftSpec, ...] = ()):
    sc = apply_scenario_overrides(
        load_scenario(case["scenario"]), _to_snake(case["overrides"])
    )
    arms = tuple(
        ArmSpec(f"arm-{i}", f"Arm {i}", dict(s)) for i, s in enumerate(case["arms"])
    )
    cfg = ExperimentConfig(
        experiment_id="golden",
        arms=arms,
        scenario=case["scenario"],
        ctr_mode=case["ctrMode"],
        horizon=case.get("horizon", sc.horizon[case["ctrMode"]]),
    )
    env = build_true_model(
        cfg,
        jax.random.PRNGKey(0),
        scenario=sc,
        shifts=shifts,
        calibration_samples=CALIBRATION_SAMPLES,
    )
    return sc, env


def _exact_by_segment(m, offset=None, mult=None) -> np.ndarray:
    """Exact expected CTR (S, K): enumerate every ctx-v1 level combination with
    the segment's level probabilities. ``offset`` / ``mult`` (S, K) are the
    active shift logit offsets / shock multipliers (pre-drift truth)."""
    levels = _all_levels()
    X = features.levels_to_matrix(levels).astype(np.float64)
    level_logp = np.asarray(m.level_logits, np.float64)  # (S, G, Lmax)
    theta = np.asarray(m.theta, np.float64)
    base = float(m.alpha) + np.asarray(m.arm_base, np.float64)
    seg_aff = np.asarray(m.seg_aff, np.float64)
    if offset is None:
        offset = np.zeros_like(seg_aff)
    if mult is None:
        mult = np.ones_like(seg_aff)
    g_idx = np.arange(levels.shape[1])[None, :]
    rows = []
    for s in range(seg_aff.shape[0]):
        w = np.exp(level_logp[s][g_idx, levels].sum(axis=1))  # P(combo | s)
        logits = base[None, :] + seg_aff[s][None, :] + offset[s] + X @ theta.T
        p = np.minimum(mult[s] / (1.0 + np.exp(-logits)), P_MAX)
        rows.append((w[:, None] * p).sum(0) / w.sum())
    return np.stack(rows)  # (S, K)


def expected_matrix(case: dict) -> dict:
    """Exact pre-drift expected CTR per creative × segment from bandit's model."""
    sc, env = _build_env(case)
    m = env.model
    by_seg = _exact_by_segment(m)
    weights = np.array([seg.weight for seg in sc.segments])
    overall = weights @ by_seg
    return {
        "segments": list(env.segment_names),
        # creative × segment, as the TS preview returns it
        "ctr": [
            [round(float(v), 10) for v in by_seg[:, k]] for k in range(by_seg.shape[1])
        ],
        "oracle": [int(np.argmax(by_seg[s])) for s in range(by_seg.shape[0])],
        "overall": [round(float(v), 10) for v in overall],
        "alpha": round(float(m.alpha), 8),
    }


def _load_or_update_golden(path: Path, fresh: dict) -> dict:
    """Return the committed golden at ``path``.

    Only ``UPDATE_PREVIEW_GOLDEN=1`` (re)writes it; a missing fixture fails
    rather than being silently generated, since the frontend tests read it.
    """
    if os.environ.get("UPDATE_PREVIEW_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fresh, indent=1) + "\n")
    elif not path.exists():
        pytest.fail(
            f"golden fixture {path.relative_to(ROOT)} is missing; "
            "run with UPDATE_PREVIEW_GOLDEN=1 to generate it"
        )
    return json.loads(path.read_text())


@functools.cache
def _build_fixture() -> dict:
    return {
        "_generated": "by tests/test_scenario_preview_golden.py (bandit, noise_scale=0); "
        "set UPDATE_PREVIEW_GOLDEN=1 to rewrite",
        "calibrationSamples": CALIBRATION_SAMPLES,
        "cases": [{**c, "expected": expected_matrix(c)} for c in _cases()],
    }


def test_golden_fixture_matches_bandit():
    fresh = _build_fixture()
    committed = _load_or_update_golden(FIXTURE, fresh)
    assert [c["name"] for c in committed["cases"]] == [
        c["name"] for c in fresh["cases"]
    ]
    for old, new in zip(committed["cases"], fresh["cases"], strict=True):
        assert old["arms"] == new["arms"] and old["overrides"] == new["overrides"]
        assert old["expected"]["oracle"] == new["expected"]["oracle"], new["name"]
        np.testing.assert_allclose(
            old["expected"]["ctr"],
            new["expected"]["ctr"],
            atol=REPRO_TOL,
            err_msg=new["name"],
        )


def test_overrides_move_the_matrix_the_way_the_preview_claims():
    """Sanity on the fixture itself: a wider gap widens it; judge_wrong=1 flips
    clear_winner's ranking."""
    by_name = {c["name"]: c["expected"] for c in _build_fixture()["cases"]}
    preset = by_name["clear_winner/three/preset"]["overall"]
    flipped = by_name["clear_winner/three/judge_backwards_gap_wide"]["overall"]
    assert int(np.argmax(preset)) == int(np.argmin(flipped))
    assert max(flipped) - min(flipped) > max(preset) - min(preset)


# ------------------------------------------------ scripted shifts (contracts §10)

_CAMEL = {
    "kind": "kind",
    "atFrac": "at_frac",
    "untilFrac": "until_frac",
    "segment": "segment",
    "creativeId": "creative_id",
    "liftPp": "lift_pp",
    "dropPp": "drop_pp",
    "segmentMix": "segment_mix",
    "ctrMultiplier": "ctr_multiplier",
}

# (name, scenario, ctr mode, arm set, camelCase overrides, REST camelCase shifts)
SHIFT_COMBOS: list[tuple[str, str, str, str, dict, list[dict]]] = [
    (
        "promote_one_segment",
        "segment_winners",
        "demo",
        "four",
        {},
        [
            {
                "kind": "promote",
                "atFrac": 0.4,
                "segment": "mobile_scrollers",
                "creativeId": "arm-3",
                "liftPp": 0.015,
            }
        ],
    ),
    (
        "demote_leader_then_shock",
        "segment_winners",
        "demo",
        "four",
        {},
        [
            {
                "kind": "shock",
                "atFrac": 0.6,
                "untilFrac": 0.7,
                "segment": None,
                "creativeId": "arm-1",
                "ctrMultiplier": 0.6,
            },
            {
                "kind": "demote",
                "atFrac": 0.5,
                "segment": None,
                "creativeId": "leader",
                "dropPp": 0.015,
            },
        ],
    ),
    (
        "mix_then_demote_leader",
        "segment_winners",
        "demo",
        "four",
        {},
        [
            {"kind": "mix", "atFrac": 0.3, "segmentMix": [0.1, 0.1, 0.7, 0.1]},
            {
                "kind": "demote",
                "atFrac": 0.5,
                "segment": None,
                "creativeId": "leader",
                "dropPp": 0.01,
            },
        ],
    ),
    (
        "promote_everyone_judge_backwards",
        "clear_winner",
        "demo",
        "three",
        {"judgeWrong": 1.0},
        [
            {
                "kind": "promote",
                "atFrac": 0.3,
                "segment": None,
                "creativeId": "arm-0",
                "liftPp": 0.01,
            },
            {
                "kind": "demote",
                "atFrac": 0.6,
                "segment": "commuters",
                "creativeId": "leader",
                "dropPp": 0.02,
            },
            {
                "kind": "shock",
                "atFrac": 0.8,
                "untilFrac": 1.0,
                "segment": "desk_researchers",
                "creativeId": "arm-2",
                "ctrMultiplier": 1.8,
            },
        ],
    ),
    (  # PR D's "Ad fatigue on the leader 60–75%" preset, after a promote
        "promote_then_shock_on_leader",
        "segment_winners",
        "demo",
        "four",
        {},
        [
            {
                "kind": "shock",
                "atFrac": 0.6,
                "untilFrac": 0.75,
                "segment": None,
                "creativeId": "leader",
                "ctrMultiplier": 0.6,
            },
            {
                "kind": "promote",
                "atFrac": 0.3,
                "segment": None,
                "creativeId": "arm-2",
                "liftPp": 0.005,
            },
        ],
    ),
    (
        "realistic_promote_and_boost",
        "segment_winners",
        "realistic",
        "three",
        {"segmentMix": [0.55, 0.05, 0.3, 0.1]},
        [
            {
                "kind": "promote",
                "atFrac": 0.25,
                "segment": "trend_followers",
                "creativeId": "arm-2",
                "liftPp": 0.003,
            },
            {
                "kind": "shock",
                "atFrac": 0.5,
                "untilFrac": 0.8,
                "segment": None,
                "creativeId": "arm-0",
                "ctrMultiplier": 1.5,
            },
        ],
    ),
]


def _shift_cases() -> list[dict]:
    return [
        {
            "name": f"{scenario}/{arm_set}/{label}",
            "scenario": scenario,
            "ctrMode": ctr_mode,
            "horizon": load_scenario(scenario).horizon[ctr_mode],
            "arms": ARM_SETS[arm_set],
            "overrides": ov,
            "shifts": shifts,
        }
        for label, scenario, ctr_mode, arm_set, ov, shifts in SHIFT_COMBOS
    ]


def _shifts_to_snake(shifts: list[dict]) -> tuple[ShiftSpec, ...]:
    return shifts_from_dict([{_CAMEL[k]: v for k, v in s.items()} for s in shifts])


def _r(x: float, nd: int = 10) -> float:
    return round(float(x), nd)


def expected_shift_matrices(case: dict) -> dict:
    """Resolved shifts + the exact creative × segment CTR in every regime."""
    sc, env = _build_env(case, _shifts_to_snake(case["shifts"]))
    m = env.model
    starts = np.asarray(m.shift_start, np.float64)
    ends = np.asarray(m.shift_end, np.float64)
    logit = np.asarray(m.shift_logit, np.float64)
    mult = np.asarray(m.shift_mult, np.float64)
    seg_logits = np.asarray(m.shift_seg_logits, np.float64)
    is_mix = np.asarray(m.shift_is_mix)
    recs = resolved_shifts(env)
    horizon = case["horizon"]
    edges = sorted(
        {0, horizon}
        | {int(v) for v in np.concatenate([starts, ends]) if 0 < v < horizon}
    )
    base_w = np.array([seg.weight for seg in sc.segments])
    regimes = []
    for start, end in zip(edges[:-1], edges[1:], strict=True):
        started = starts <= start
        window = started & (start < ends)
        offset = np.einsum("p,psk->sk", started.astype(float), logit)
        mul = np.prod(np.where(window[:, None, None], mult, 1.0), axis=0)
        mixes = np.flatnonzero(started & is_mix)
        weights = np.exp(seg_logits[mixes[-1]]) if len(mixes) else base_w
        weights = weights / weights.sum()
        by_seg = _exact_by_segment(m, offset, mul)
        active = [
            rec["index"]
            for j, rec in enumerate(recs)
            if started[j] and (rec["kind"] != "shock" or window[j])
        ]
        regimes.append(
            {
                "start": start,
                "end": end,
                "active": sorted(active),
                "segmentWeights": [_r(w) for w in weights],
                "ctr": [[_r(v) for v in by_seg[:, k]] for k in range(by_seg.shape[1])],
                "oracle": [int(np.argmax(row)) for row in by_seg],
                "overall": [_r(v) for v in weights @ by_seg],
            }
        )
    arm_ids = env.arm_ids
    resolved = []
    for rec in recs:
        cid = rec.get("creative_id")
        resolved.append(
            {
                "index": rec["index"],
                "kind": rec["kind"],
                "round": rec["round"],
                "endRound": rec["end_round"],
                "creativeIndex": arm_ids.index(cid) if cid else None,
                "segment": rec.get("segment"),
                "segmentWeights": [_r(w) for w in rec["segment_weights"]]
                if "segment_weights" in rec
                else None,
                "targets": [
                    {
                        {
                            "ctr_before": "ctrBefore",
                            "ctr_after": "ctrAfter",
                            "best_other_ctr": "bestOtherCtr",
                            "logit_offset": "logitOffset",
                        }.get(k, k): v if isinstance(v, str) else _r(v)
                        for k, v in t.items()
                    }
                    for t in rec.get("targets", [])
                ],
            }
        )
    return {
        "segments": list(env.segment_names),
        "alpha": round(float(m.alpha), 8),
        "resolved": resolved,
        "regimes": regimes,
    }


@functools.cache
def _build_shift_fixture() -> dict:
    return {
        "_generated": "by tests/test_scenario_preview_golden.py (bandit, noise_scale=0; "
        "contracts §10 shifts); set UPDATE_PREVIEW_GOLDEN=1 to rewrite",
        "calibrationSamples": CALIBRATION_SAMPLES,
        "cases": [
            {**c, "expected": expected_shift_matrices(c)} for c in _shift_cases()
        ],
    }


def test_shift_golden_fixture_matches_bandit():
    fresh = _build_shift_fixture()
    committed = _load_or_update_golden(SHIFT_FIXTURE, fresh)
    assert [c["name"] for c in committed["cases"]] == [
        c["name"] for c in fresh["cases"]
    ]
    for old, new in zip(committed["cases"], fresh["cases"], strict=True):
        assert old["shifts"] == new["shifts"] and old["arms"] == new["arms"]
        o, n = old["expected"], new["expected"]
        assert [
            (r["index"], r["kind"], r["round"], r["endRound"], r["creativeIndex"])
            for r in o["resolved"]
        ] == [
            (r["index"], r["kind"], r["round"], r["endRound"], r["creativeIndex"])
            for r in n["resolved"]
        ], new["name"]
        assert len(o["regimes"]) == len(n["regimes"]), new["name"]
        for og, ng in zip(o["regimes"], n["regimes"], strict=True):
            assert (og["start"], og["end"], og["active"], og["oracle"]) == (
                ng["start"],
                ng["end"],
                ng["active"],
                ng["oracle"],
            ), new["name"]
            np.testing.assert_allclose(
                og["ctr"], ng["ctr"], atol=REPRO_TOL, err_msg=new["name"]
            )


def test_shift_golden_regimes_tell_the_story():
    """Sanity on the shift fixture: the first regime is the unshifted preview,
    promote/demote/mix/shock move the matrix the way §10 says."""
    by_name = {c["name"]: c for c in _build_shift_fixture()["cases"]}
    plain = {c["name"]: c["expected"] for c in _build_fixture()["cases"]}

    promote = by_name["segment_winners/four/promote_one_segment"]["expected"]
    before, after = promote["regimes"]
    np.testing.assert_allclose(
        before["ctr"], plain["segment_winners/four/preset"]["ctr"], atol=1e-9
    )
    assert before["oracle"][0] != 3 and after["oracle"][0] == 3
    assert after["oracle"][1:] == before["oracle"][1:]
    tgt = promote["resolved"][0]["targets"][0]
    assert tgt["ctrAfter"] - tgt["bestOtherCtr"] == pytest.approx(0.015, abs=1e-8)

    demote = by_name["segment_winners/four/demote_leader_then_shock"]["expected"]
    assert [r["kind"] for r in demote["resolved"]] == ["demote", "shock"]
    pre, dem, shock, post = demote["regimes"]
    leader = demote["resolved"][0]["creativeIndex"]
    assert leader == int(np.argmax(pre["overall"]))
    assert leader not in dem["oracle"]
    assert shock["active"] == [0, 1] and post["active"] == [1]
    np.testing.assert_allclose(
        np.array(shock["ctr"][1]), 0.6 * np.array(post["ctr"][1]), atol=1e-9
    )
    assert post["ctr"] == dem["ctr"]  # the shock recovers

    mix = by_name["segment_winners/four/mix_then_demote_leader"]["expected"]
    assert mix["regimes"][1]["segmentWeights"] == pytest.approx([0.1, 0.1, 0.7, 0.1])
    pooled_leader = int(np.argmax(mix["regimes"][1]["overall"]))
    assert mix["resolved"][1]["creativeIndex"] == pooled_leader

    fatigue = by_name["segment_winners/four/promote_then_shock_on_leader"]["expected"]
    promo, shock_rec = fatigue["resolved"]
    assert shock_rec["kind"] == "shock" and shock_rec["creativeIndex"] == 2
    _, promoted, shocked, recovered = fatigue["regimes"]
    assert promoted["oracle"] == [2, 2, 2, 2]
    np.testing.assert_allclose(
        np.array(shocked["ctr"][2]), 0.6 * np.array(promoted["ctr"][2]), atol=1e-9
    )
    assert recovered["ctr"] == promoted["ctr"]

    realistic = by_name["segment_winners/three/realistic_promote_and_boost"]
    tgt = realistic["expected"]["resolved"][0]["targets"][0]
    assert tgt["ctrAfter"] - tgt["bestOtherCtr"] == pytest.approx(0.003, abs=1e-8)
