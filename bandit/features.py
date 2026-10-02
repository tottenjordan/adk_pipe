"""Context feature encoding (``ctx-v1``), contracts §1 / §4.

The decision context is a coarse, synthetic OpenRTB-style dict (contracts §4).
Each group is one-hot encoded with its **first level dropped as the reference**,
and a leading bias column is added, giving ``DIM == 19`` features:

=========================  =======================================  =======
group                      levels (first = reference, dropped)      columns
=========================  =======================================  =======
devicetype                 mobile, desktop, tablet                  2
os                         ios, android, other                      2
connectiontype             wifi, cellular                           1
region                     northeast, midwest, south, west          3
age_bucket                 21-34, 35-54, 55+  (adults only)         2
daypart                    morning, afternoon, evening, night       3
weekend                    False, True                              1
topic_matches_trend        False, True                              1
interest_matches_product   False, True                              1
freq_24h                   0, 1, 2+                                 2
=========================  =======================================  =======

Every context must carry exactly these ten keys: missing keys, unknown keys and
unknown levels raise ``ValueError``, and identifying/sensitive keys (IDs, IP,
lat/long, ZIP, city, protected categories) are rejected with a dedicated
"sensitive" message. Integer ``freq_24h`` values are accepted (``>= 2`` maps to
``"2+"``). This module is numpy-only so the serving image can import it
without pulling in the simulator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

FEATURE_SPEC_VERSION = "ctx-v1"

CONTEXT_SPEC: tuple[tuple[str, tuple[str | bool, ...]], ...] = (
    ("devicetype", ("mobile", "desktop", "tablet")),
    ("os", ("ios", "android", "other")),
    ("connectiontype", ("wifi", "cellular")),
    ("region", ("northeast", "midwest", "south", "west")),
    ("age_bucket", ("21-34", "35-54", "55+")),
    ("daypart", ("morning", "afternoon", "evening", "night")),
    ("weekend", (False, True)),
    ("topic_matches_trend", (False, True)),
    ("interest_matches_product", (False, True)),
    ("freq_24h", ("0", "1", "2+")),
)
CONTEXT_KEYS: tuple[str, ...] = tuple(key for key, _ in CONTEXT_SPEC)
BOOL_KEYS: frozenset[str] = frozenset(
    key for key, levels in CONTEXT_SPEC if levels == (False, True)
)

#: Keys that must never appear in a context (no IDs, location, protected data).
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "user_id",
        "userid",
        "uid",
        "device_id",
        "deviceid",
        "ifa",
        "idfa",
        "gaid",
        "cookie",
        "cookie_id",
        "email",
        "phone",
        "name",
        "ip",
        "ipv4",
        "ipv6",
        "lat",
        "lon",
        "lng",
        "latitude",
        "longitude",
        "geo",
        "zip",
        "zipcode",
        "postal_code",
        "postcode",
        "city",
        "address",
        "dob",
        "birthdate",
        "age",
        "gender",
        "sex",
        "race",
        "ethnicity",
        "religion",
        "health",
        "sexual_orientation",
        "political",
        "income",
    }
)


def _column_names() -> list[str]:
    names = ["bias"]
    for key, levels in CONTEXT_SPEC:
        if key in BOOL_KEYS:
            names.append(key)
        else:
            names.extend(f"{key}={level}" for level in levels[1:])
    return names


_NAMES = _column_names()
DIM = len(_NAMES)

#: ``COLUMN_TABLE[g, level]`` = feature column of that level, or ``DIM`` for the
#: dropped reference level (a sentinel column sliced away after one-hot).
_MAX_LEVELS = max(len(levels) for _, levels in CONTEXT_SPEC)
COLUMN_TABLE = np.full((len(CONTEXT_SPEC), _MAX_LEVELS), DIM, dtype=np.int32)
_col = 1
for _g, (_key, _levels) in enumerate(CONTEXT_SPEC):
    for _level_idx in range(1, len(_levels)):
        COLUMN_TABLE[_g, _level_idx] = _col
        _col += 1
assert _col == DIM


def feature_names() -> list[str]:
    """Feature column names, bias first (length ``DIM``)."""
    return list(_NAMES)


def _level_index(key: str, levels: tuple[str | bool, ...], value: object) -> int:
    if key in BOOL_KEYS:
        if not isinstance(value, bool):
            raise ValueError(f"context field {key!r} must be a bool, got {value!r}")
        return int(value)
    if key == "freq_24h" and isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ValueError(f"context field 'freq_24h' must be >= 0, got {value}")
        value = "2+" if value >= 2 else str(value)
    if value not in levels:
        raise ValueError(
            f"context field {key!r} has unknown level {value!r}; allowed {levels}"
        )
    return levels.index(value)


def context_levels(ctx: Mapping[str, object]) -> np.ndarray:
    """Validate ``ctx`` and return its per-group level indices, shape ``(G,)``."""
    keys = set(ctx)
    sensitive = sorted(k for k in keys if k.lower() in SENSITIVE_KEYS)
    if sensitive:
        raise ValueError(f"sensitive context fields are not allowed: {sensitive}")
    unknown = sorted(keys - set(CONTEXT_KEYS))
    if unknown:
        raise ValueError(f"unknown context fields: {unknown}")
    missing = [k for k in CONTEXT_KEYS if k not in keys]
    if missing:
        raise ValueError(f"missing context fields: {missing}")
    return np.array(
        [_level_index(key, levels, ctx[key]) for key, levels in CONTEXT_SPEC],
        dtype=np.int32,
    )


def levels_to_matrix(levels: np.ndarray) -> np.ndarray:
    """Level indices ``(n, G)`` -> feature matrix ``(n, DIM)`` (float32)."""
    levels = np.asarray(levels, dtype=np.int32)
    n = levels.shape[0]
    cols = COLUMN_TABLE[np.arange(len(CONTEXT_SPEC))[None, :], levels]  # (n, G)
    X = np.zeros((n, DIM + 1), dtype=np.float32)
    np.put_along_axis(X, cols, 1.0, axis=1)
    X = X[:, :DIM]
    X[:, 0] = 1.0
    return X


def encode_context(ctx: Mapping[str, object]) -> np.ndarray:
    """Encode one context dict to a ``(DIM,)`` float32 vector (bias first)."""
    return levels_to_matrix(context_levels(ctx)[None, :])[0]


def encode_batch(ctxs: Sequence[Mapping[str, object]]) -> np.ndarray:
    """Encode a list of context dicts to a ``(n, DIM)`` float32 matrix."""
    if not ctxs:
        return np.zeros((0, DIM), dtype=np.float32)
    return levels_to_matrix(np.stack([context_levels(c) for c in ctxs]))


def decode_levels(levels: np.ndarray) -> list[dict[str, object]]:
    """Level indices ``(n, G)`` -> contract §4 context dicts (plain Python types)."""
    out: list[dict[str, object]] = []
    for row in np.asarray(levels):
        ctx: dict[str, object] = {}
        for (key, lv), idx in zip(CONTEXT_SPEC, row, strict=True):
            value = lv[int(idx)]
            ctx[key] = bool(value) if key in BOOL_KEYS else value
        out.append(ctx)
    return out
