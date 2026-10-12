"""Regenerate the bandit notebook-parity fixture data (tiny horizons, 1 episode).

    uv run python tests/fixtures/bandit_parity/regenerate.py

Runs ``experiments/bandit/notebook_parity.py``'s ``generate`` into a temporary
directory, then writes a trimmed copy here: only the keys ``plot_all`` reads,
floats rounded to 4 significant digits (keeps the fixture ~160 KB). Regenerate
when a plot function reads a new field or a figure/run is added; extend the
key lists below to match.
"""

import importlib.util
import json
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

TOP_KEYS = ("kind", "feature_spec_version", "rows", "aggregate")
AGG_KEYS = (
    "experiment_id",
    "episodes",
    "horizon",
    "checkpoints",
    "policies",
    "curves",
    "totals",
    "arm_share",
    "arms",
)
ROW_KEYS = ("policy", "episode", "pct_optimal", "optimal_avg_reward")
CURVE_KEYS = ("checkpoints", "cum_avg_reward")


def _round(obj: Any) -> Any:
    if isinstance(obj, float):
        return float(f"{obj:.4g}")
    if isinstance(obj, list):
        return [_round(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    return obj


def trim(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: doc[k] for k in TOP_KEYS}
    out["aggregate"] = {k: doc["aggregate"][k] for k in AGG_KEYS}
    out["rows"] = [
        {
            **{k: row[k] for k in ROW_KEYS},
            "curve": {k: row["curve"][k] for k in CURVE_KEYS},
        }
        for row in doc["rows"]
    ]
    return _round(out)


def main() -> None:
    spec = importlib.util.spec_from_file_location(
        "notebook_parity", ROOT / "experiments/bandit/notebook_parity.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp)
        mod.generate(raw, episodes=1, scale=0.001, seed=0)
        for old in HERE.glob("*.json"):
            old.unlink()
        for path in sorted(raw.glob("*.json")):
            doc = trim(json.loads(path.read_text()))
            (HERE / path.name).write_text(json.dumps(doc, separators=(",", ":")) + "\n")
            print(f"wrote {HERE / path.name}")


if __name__ == "__main__":
    main()
