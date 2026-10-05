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
    apply_scenario_overrides,
    load_scenario,
)
from bandit.environment import build_true_model  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = (
    ROOT
    / "frontend"
    / "src"
    / "__tests__"
    / "fixtures"
    / "scenario-preview-golden.json"
)
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


def expected_matrix(case: dict) -> dict:
    """Exact pre-drift expected CTR per creative × segment from bandit's model."""
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
        horizon=sc.horizon[case["ctrMode"]],
    )
    env = build_true_model(
        cfg,
        jax.random.PRNGKey(0),
        scenario=sc,
        calibration_samples=CALIBRATION_SAMPLES,
    )
    m = env.model
    levels = _all_levels()
    X = features.levels_to_matrix(levels).astype(np.float64)
    level_logp = np.asarray(m.level_logits, np.float64)  # (S, G, Lmax)
    theta = np.asarray(m.theta, np.float64)
    base = float(m.alpha) + np.asarray(m.arm_base, np.float64)
    seg_aff = np.asarray(m.seg_aff, np.float64)
    g_idx = np.arange(levels.shape[1])[None, :]
    rows = []
    for s in range(seg_aff.shape[0]):
        w = np.exp(level_logp[s][g_idx, levels].sum(axis=1))  # P(combo | s)
        logits = base[None, :] + seg_aff[s][None, :] + X @ theta.T
        p = 1.0 / (1.0 + np.exp(-logits))
        rows.append((w[:, None] * p).sum(0) / w.sum())
    by_seg = np.stack(rows)  # (S, K)
    weights = np.array([seg.weight for seg in sc.segments])
    overall = weights @ by_seg
    return {
        "segments": list(env.segment_names),
        # creative × segment, as the TS preview returns it
        "ctr": [[round(float(v), 10) for v in by_seg[:, k]] for k in range(len(arms))],
        "oracle": [int(np.argmax(by_seg[s])) for s in range(by_seg.shape[0])],
        "overall": [round(float(v), 10) for v in overall],
        "alpha": round(float(m.alpha), 8),
    }


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
    if os.environ.get("UPDATE_PREVIEW_GOLDEN") == "1" or not FIXTURE.exists():
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(fresh, indent=1) + "\n")
    committed = json.loads(FIXTURE.read_text())
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
