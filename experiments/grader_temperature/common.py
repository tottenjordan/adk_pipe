"""Shared paths and pure helpers for the grader temperature harness."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
SELECTION_PATH = DATA_DIR / "selection.json"
STATE_DIR = DATA_DIR / "state"
IMG_DIR = DATA_DIR / "img"
JUDGE_DIR = RESULTS_DIR / "judge"
QA_DIR = RESULTS_DIR / "qa"

APP_NAME = "creative_agent"
# The judge's per-creative failure placeholder (creative_eval/evaluate.py
# _failed_score; creative_eval/agent.py re-judges these too).
EVALUATION_FAILED = "evaluation_failed"


def parse_temps(text: str) -> list[float]:
    """``"0.3,1.0"`` → ``[0.3, 1.0]`` (at least one, no duplicates)."""
    temps = [float(t) for t in text.split(",") if t.strip()]
    if not temps or len(set(temps)) != len(temps):
        raise ValueError(f"need distinct temperatures, got {text!r}")
    return temps


def temp_label(t: float) -> str:
    """Filename label of a temperature: ``0.3`` → ``"0.3"``, ``1`` → ``"1.0"``."""
    return str(float(t))


def run_filename(t: float, repeat: int) -> str:
    return f"{temp_label(t)}-r{repeat}.json"


def parse_run_filename(name: str) -> tuple[float, int] | None:
    """``"1.0-r2.json"`` → ``(1.0, 2)``; None for anything else."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)-r(\d+)\.json", name)
    return (float(m.group(1)), int(m.group(2))) if m else None


def concept_slug(name: str) -> str:
    """A filesystem-safe, collision-resistant directory name for a concept."""
    readable = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "concept"
    return f"{readable}-{hashlib.sha256(name.encode()).hexdigest()[:8]}"


def plan_order(
    session_ids: Sequence[str], temps: Sequence[float], repeats: int
) -> list[tuple[str, float, int]]:
    """Execution order ``(session_id, temperature, repeat)``, arms interleaved.

    Per session, each repeat runs every temperature once; the order alternates
    with ``session index + repeat`` (``0.3,1.0`` then ``1.0,0.3``, …), so
    neither arm always goes first and a warm-up / quota effect cancels.
    """
    order: list[tuple[str, float, int]] = []
    for i, sid in enumerate(session_ids):
        for k in range(repeats):
            arms = list(temps) if (i + k) % 2 == 0 else list(reversed(temps))
            order += [(sid, t, k) for t in arms]
    return order


def write_json(path: Path, data: Any) -> None:
    """Write ``data`` atomically (a crash never leaves a half file to resume past)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def load_selection(path: Path = SELECTION_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_state(sid: str, state_dir: Path = STATE_DIR) -> dict[str, Any] | None:
    path = state_dir / f"{sid}.json"
    return json.loads(path.read_text()) if path.exists() else None


def as_obj(value: Any) -> Any:
    """A state value that may be stored as its JSON string, decoded."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def generated_images(state: Mapping[str, Any]) -> dict[str, Any]:
    images = as_obj(state.get("generated_images"))
    return dict(images) if isinstance(images, Mapping) else {}
