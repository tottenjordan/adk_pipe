"""Generate ``frontend/src/lib/scenario-presets.generated.json``.

The results-page Deploy panel's "Advanced" section (contracts §9) fills its
sliders from the scenario presets and runs a noise-free port of
``bandit.environment.build_true_model`` for its live preview
(``frontend/src/lib/scenario-preview.ts``). Both read this JSON, generated from
``bandit/scenarios/*.yaml``, ``bandit.config.BASE_MARGINALS`` /
``OVERRIDE_BOUNDS``, the scripted-shift constants (``SHIFT_KINDS``,
``SHIFT_BOUNDS``, ``MAX_SHIFTS``, ``SHIFT_MIN_WINDOW``; contracts §10) and the
``ctx-v1`` feature spec (``bandit.features``), so the frontend never hand-copies
simulator constants.

    uv run python scripts/gen_scenario_presets.py          # rewrite the JSON
    uv run python scripts/gen_scenario_presets.py --check  # exit 1 if stale

``tests/test_scenario_presets_sync.py`` runs the check in CI. Pure Python (no JAX).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # run as a script: make the flat packages importable
    sys.path.insert(0, str(ROOT))

from bandit import features  # noqa: E402
from bandit.config import (  # noqa: E402
    BASE_MARGINALS,
    LEADER,
    MAX_SHIFTS,
    OVERRIDE_BOUNDS,
    SCENARIOS,
    SHIFT_BOUNDS,
    SHIFT_KINDS,
    SHIFT_MIN_WINDOW,
    load_scenario,
)

OUTPUT = ROOT / "frontend" / "src" / "lib" / "scenario-presets.generated.json"


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _scenario(name: str) -> dict[str, Any]:
    sc = load_scenario(name)
    return {
        "name": sc.name,
        "description": sc.description,
        "armEffect": sc.arm_effect,
        "rankCtrs": list(sc.rank_ctrs),
        "liftPp": sc.lift_pp,
        "targetCtr": dict(sc.target_ctr),
        "kappa": sc.kappa,
        "lam": sc.lam,
        "eta": sc.eta,
        "noiseSd": sc.noise_sd,
        "thetaSd": sc.theta_sd,
        "judgeWrong": sc.judge_wrong,
        "drift": {
            "kind": sc.drift.kind,
            "atFrac": sc.drift.at_frac,
            "widthFrac": sc.drift.width_frac,
        },
        "segments": [
            {
                "name": seg.name,
                "weight": seg.weight,  # renormalised to sum 1 by the loader
                "winnerKey": seg.winner_key,
                "marginals": seg.marginals,  # overrides of baseMarginals
            }
            for seg in sc.segments
        ],
    }


def build_presets() -> dict[str, Any]:
    """The JSON document (deterministic: same inputs, same output)."""
    return {
        "_generated": "by scripts/gen_scenario_presets.py from bandit/; do not edit",
        "featureSpecVersion": features.FEATURE_SPEC_VERSION,
        "contextSpec": [
            {"key": key, "levels": [str(lv).lower() for lv in levels]}
            for key, levels in features.CONTEXT_SPEC
        ],
        "boolKeys": sorted(features.BOOL_KEYS),
        "featureNames": features.feature_names(),
        "baseMarginals": BASE_MARGINALS,
        "overrideBounds": {k: list(v) for k, v in OVERRIDE_BOUNDS.items()},
        # contracts §10; liftPp / dropPp bounds are demo CTR points, scaled by
        # targetCtr[ctrMode] / targetCtr.demo for the run's ctr mode
        "shifts": {
            "kinds": list(SHIFT_KINDS),
            "maxShifts": MAX_SHIFTS,
            "minWindow": SHIFT_MIN_WINDOW,
            "leader": LEADER,
            "bounds": {_camel(k): list(v) for k, v in SHIFT_BOUNDS.items()},
        },
        "scenarios": {name: _scenario(name) for name in SCENARIOS},
    }


def render() -> str:
    return json.dumps(build_presets(), indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the frontend's scenario presets JSON."
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the JSON is stale"
    )
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != text:
            print(f"{OUTPUT.relative_to(ROOT)} is stale; run {Path(__file__).name}")
            return 1
        return 0
    OUTPUT.write_text(text)
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
