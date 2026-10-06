"""Batch-means interval for one long, non-replicated run (contracts §11).

A continuous traffic run has no independent episodes: its segments are sequential,
autocorrelated and (while the policy learns) trending, so a cross-segment t-interval
is invalid. The standard estimate for a single long run is **batch means after
deleting the warm-up**: treat each post-warm-up segment's value as a batch mean and
form a Student-t interval over them, valid only once the batch means have stopped
trending and are roughly uncorrelated (lag-1 autocorrelation near 0).

Pure Python on purpose: the api image has no numpy.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from runserver.experiments_metrics import mean_std, t_critical

STATUSES = ("ok", "too_few_segments", "still_trending", "autocorrelated")


def lag1_autocorrelation(values: Sequence[float]) -> float | None:
    """The lag-1 sample autocorrelation ``Σ(x_i − x̄)(x_{i+1} − x̄) / Σ(x_i − x̄)²``;
    ``None`` for fewer than 3 values or zero variance."""
    n = len(values)
    if n < 3:
        return None
    mean = math.fsum(values) / n
    dev = [v - mean for v in values]
    denom = math.fsum(d * d for d in dev)
    if denom <= 0.0:
        return None
    return math.fsum(dev[i] * dev[i + 1] for i in range(n - 1)) / denom


def _linear_fit(values: Sequence[float]) -> tuple[float, list[float]]:
    """OLS slope of ``values`` against ``0..m-1`` and the residuals."""
    m = len(values)
    x_mean = (m - 1) / 2.0
    y_mean = math.fsum(values) / m
    sxx = math.fsum((i - x_mean) ** 2 for i in range(m))
    sxy = math.fsum((i - x_mean) * (y - y_mean) for i, y in enumerate(values))
    slope = sxy / sxx
    resid = [y - (y_mean + slope * (i - x_mean)) for i, y in enumerate(values)]
    return slope, resid


def is_trending(values: Sequence[float]) -> bool:
    """Whether an OLS line through ``(i, values[i])`` has a slope significantly
    different from 0 (two-sided 5% t-test, df = m − 2). A perfect fit counts as
    trending exactly when its slope is non-zero. Fewer than 3 values: ``False``."""
    m = len(values)
    if m < 3:
        return False
    slope, resid = _linear_fit(values)
    sse = math.fsum(r * r for r in resid)
    if sse <= 1e-12 * max(1.0, math.fsum(y * y for y in values)):
        return abs(slope) > 1e-12
    x_mean = (m - 1) / 2.0
    sxx = math.fsum((i - x_mean) ** 2 for i in range(m))
    se = math.sqrt(sse / (m - 2) / sxx)
    return abs(slope / se) > t_critical(m - 1)  # t_critical(n) uses df = n - 1


def detrended_lag1(values: Sequence[float]) -> float | None:
    """The lag-1 autocorrelation of the residuals around the OLS line (``None``
    as ``lag1_autocorrelation``). Equal to the plain lag-1 autocorrelation up to
    noise when there is no trend, but unlike it not inflated by a trend, so a
    trending series is not mistaken for an autocorrelated one."""
    if len(values) < 3:
        return None
    return lag1_autocorrelation(_linear_fit(values)[1])


def lag1_threshold(m: int, max_lag1: float) -> float:
    """The ``|lag1|`` above which ``m`` batch means count as autocorrelated:
    ``max(max_lag1, 1.96 / √m)``. The lag-1 estimate of ``m`` independent values
    has a standard error of about ``1 / √m`` (0.22 at 20), so ``max_lag1`` alone
    would flag ~40% of iid runs of 20 segments; requiring the autocorrelation to
    also be significant at about 5% keeps that near 5%, and a strongly
    autocorrelated run (ρ = 0.8) is still caught."""
    return max(max_lag1, 1.96 / math.sqrt(m)) if m > 0 else max_lag1


def batch_means_summary(
    diffs: Sequence[float],
    warmup_frac: float = 0.5,
    min_batches: int = 5,
    max_lag1: float = 0.2,
) -> dict:
    """Delete the warm-up (the first ``floor(warmup_frac · n)`` segments), then
    ``mean ± t · s / √m`` over the ``m`` kept segments (Student t, df = m − 1).

    Returns ``{segments, warmupSegments, mean, lo, hi, lag1, status}``. ``status``
    is checked in this order: ``too_few_segments`` (``m < min_batches``),
    ``autocorrelated`` (``|lag1| > lag1_threshold(m, max_lag1)``, i.e. above
    ``max_lag1`` and significant), ``still_trending`` (a linear
    trend on the kept segments is significant at 5%), else ``ok``. ``lag1`` is
    the lag-1 autocorrelation of the kept segments around their OLS line
    (``detrended_lag1``; ``None`` for fewer than 3 kept or no variance). It is
    checked first because the naive trend t-test fires on most strongly
    autocorrelated series (their wandering looks like a slope), while
    detrending keeps a genuine trend from reading as autocorrelation.
    ``lo`` / ``hi`` are ``None`` unless ``ok``. ``mean`` is over the kept
    segments (over all of them when none are kept; 0.0 without any)."""
    values = [float(d) for d in diffs]
    n = len(values)
    warmup = min(n, max(0, int(n * warmup_frac)))
    kept = values[warmup:]
    m = len(kept)
    mean, std = mean_std(kept if kept else values)
    lag1 = detrended_lag1(kept)
    if m < max(min_batches, 2):
        status = "too_few_segments"
    elif lag1 is not None and abs(lag1) > lag1_threshold(m, max_lag1):
        status = "autocorrelated"
    elif is_trending(kept):
        status = "still_trending"
    else:
        status = "ok"
    lo = hi = None
    if status == "ok":
        half = t_critical(m) * std / math.sqrt(m)
        lo, hi = mean - half, mean + half
    return {
        "segments": n,
        "warmupSegments": warmup,
        "mean": mean,
        "lo": lo,
        "hi": hi,
        "lag1": lag1,
        "status": status,
    }
