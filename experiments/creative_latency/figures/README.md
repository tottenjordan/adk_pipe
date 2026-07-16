# Figures

Static PNG exports of the creative_latency results.

- `latency_phases.png` — stacked per-phase wall-clock by config (total + %-vs-baseline annotated)
- `latency_totals.png` — total wall-clock by config alongside the 429/503 rate-limit counts

Regenerate anywhere (no browser needed) with the matplotlib renderer:

```bash
PYTHONPATH="$PWD" uv run python -m experiments.creative_latency.render_static
```

The interactive `report.html` (one level up) is built by `plot.py`; its own PNG
export goes through kaleido/headless-Chrome and is best-effort. `render_static.py`
exists so static figures regenerate in browserless environments too.
