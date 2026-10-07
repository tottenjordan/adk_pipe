"""creative_eval.dimensions: readable labels, mirrored from the frontend map."""

import pathlib
import re

from creative_eval.dimensions import (
    DIMENSION_LABELS,
    dimension_label,
    dimension_labels_csv,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TS_MAP = REPO_ROOT / "frontend" / "src" / "lib" / "eval-dimensions.ts"


def test_known_and_fallback_labels():
    assert dimension_label("trend_visual_connection") == "Trend connection"
    assert dimension_label("brand_product_representation") == "Brand & product"
    assert dimension_label("  weird__New_dim ") == "Weird new dim"


def test_csv_join():
    assert (
        dimension_labels_csv(["copy_quality", "stopping_power"])
        == "Copy quality, Stopping power"
    )
    assert dimension_labels_csv([]) == ""


def test_matches_frontend_map():
    ts = TS_MAP.read_text()
    # Scope to the DIMENSION_LABELS object literal so only its entries are captured.
    block = re.search(r"DIMENSION_LABELS[^=]*=\s*\{(.*?)\n\};", ts, re.S)
    assert block is not None
    pairs = dict(re.findall(r'^\s*(\w+):\s*"([^"]+)",?\s*$', block.group(1), re.M))
    assert len(pairs) == 12
    assert pairs == DIMENSION_LABELS


def test_gate_labels_match_frontend_map():
    from creative_eval.dimensions import (
        AD_COPY_GATES,
        GATE_LABELS,
        NO_GATES_GATE,
        VISUAL_GATES,
    )

    ts = TS_MAP.read_text()
    block = re.search(r"GATE_LABELS[^=]*=\s*\{(.*?)\n\};", ts, re.S)
    assert block is not None
    pairs = dict(re.findall(r'^\s*(\w+):\s*"([^"]+)",?\s*$', block.group(1), re.M))
    assert pairs == GATE_LABELS
    assert set(GATE_LABELS) == set(AD_COPY_GATES) | set(VISUAL_GATES) | {NO_GATES_GATE}


def test_gate_label_fallback():
    from creative_eval.dimensions import gate_label

    assert gate_label("product_named") == "Product named"
    assert gate_label("new_gate") == "New gate"
