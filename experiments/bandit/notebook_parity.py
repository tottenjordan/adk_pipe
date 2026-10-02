"""Notebook-parity figures for the offline bandit simulator.

Reproduces the reference notebooks' plots (toy UCB notebook + "more realistic
models" notebook) from ``bandit.cli`` simulation output JSON:

    # run every simulation (demo CTRs) and draw all figures
    uv run python experiments/bandit/notebook_parity.py --generate \\
        --data-dir /tmp/bandit_parity --fig-dir experiments/bandit/figures

    # redraw from existing JSON only
    uv run python experiments/bandit/notebook_parity.py \\
        --data-dir /tmp/bandit_parity --fig-dir experiments/bandit/figures

Each run is one ``bandit.cli.simulate`` call written to ``<data-dir>/<run>.json``
(the same document ``python -m bandit.cli simulate --out`` writes). See
docs/experiments/bandit-simulation.md for what each figure shows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

ALL = [
    "linear_ts",
    "ucb1",
    "epsilon_greedy",
    "beta_bernoulli_ts",
    "uniform",
    "oracle",
]

# Fixed categorical order (validated reference palette); colour follows the
# policy, never its rank. The oracle is a neutral reference line.
COLORS = {
    "linear_ts": "#2a78d6",
    "ucb1": "#eb6834",
    "epsilon_greedy": "#1baf7a",
    "beta_bernoulli_ts": "#eda100",
    "uniform": "#e87ba4",
    "linear_ts:discount=0.98": "#4a3aa7",
    "ucb1:c=0.01": "#008300",
    "ucb1:c=0.25": "#eb6834",
}
STYLES = {"uniform": ":", "epsilon_greedy": "-.", "ucb1:c=0.01": "--"}
ORACLE = "#52514e"
INK, MUTED = "#0b0b0b", "#52514e"
LABELS = {
    "linear_ts": "LinTS",
    "linear_ts:discount=0.98": "LinTS (discount 0.98)",
    "ucb1": "UCB1 (c=0.25)",
    "ucb1:c=0.25": "UCB1 (c=0.25)",
    "ucb1:c=0.01": "UCB1 (c=0.01)",
    "epsilon_greedy": "ε-greedy (0.1)",
    "beta_bernoulli_ts": "Beta-Bernoulli TS",
    "uniform": "Uniform",
    "oracle": "Oracle",
}


def runs(scale: float) -> dict[str, dict[str, Any]]:
    """The simulations behind the figures (demo CTRs, click reward)."""

    def h(t: int) -> int:
        return max(int(t * scale) // 100 * 100, 400)

    inj_t = h(30000)
    return {
        "clear_winner": {
            "scenario": "clear_winner",
            "policies": ALL,
            "horizon": h(20000),
        },
        "segment_winners": {
            "scenario": "segment_winners",
            "policies": ALL,
            "horizon": h(40000),
        },
        "drift": {
            "scenario": "drift",
            "policies": [
                "linear_ts",
                "linear_ts:discount=0.98",
                "ucb1",
                "beta_bernoulli_ts",
                "oracle",
            ],
            "horizon": h(40000),
            "num_checkpoints": 100,
            "checkpoint_spacing": "linear",
        },
        "ucb_small_c": {
            "scenario": "clear_winner",
            "policies": ["ucb1:c=0.01", "ucb1:c=0.25", "oracle"],
            "horizon": h(20000),
        },
        **{
            f"users_{name}": {
                "scenario": "segment_winners",
                "policies": ["linear_ts", "beta_bernoulli_ts", "oracle"],
                "horizon": h(20000),
                "segment_mix": mix,
            }
            for name, mix in (
                ("1_0", [1, 0, 0, 0]),
                ("0_1", [0, 1, 0, 0]),
                ("half_half", [0.5, 0.5, 0, 0]),
            )
        },
        "injection": {
            "scenario": "clear_winner",
            "policies": ["linear_ts", "ucb1", "beta_bernoulli_ts", "oracle"],
            "horizon": inj_t,
            "num_arms": 4,
            # synthetic-a (best) is injected and synthetic-d expires at T/2
            "arm_schedule": [[0, [1, 2, 3]], [inj_t // 2, [0, 1, 2]]],
            "num_checkpoints": 100,
            "checkpoint_spacing": "linear",
        },
    }


def generate(data_dir: Path, episodes: int, scale: float, seed: int) -> None:
    from bandit.cli import simulate

    data_dir.mkdir(parents=True, exist_ok=True)
    plan = runs(scale)
    for name, kw in plan.items():
        doc = simulate(
            episodes=episodes, seed=seed, log_propensity=False, experiment_id=name, **kw
        )
        (data_dir / f"{name}.json").write_text(json.dumps(doc))
        print(f"{name}: {doc['runtime_s']}s", file=sys.stderr)


# ----------------------------------------------------------------- plotting


def _style(ax: Any, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, loc="left", fontsize=10, color=INK)
    ax.set_xlabel(xlabel, fontsize=9, color=MUTED)
    ax.set_ylabel(ylabel, fontsize=9, color=MUTED)
    ax.tick_params(labelsize=8, colors=MUTED)
    ax.grid(True, color="#e6e5e0", linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c9c8c2")


def _zoom(ax: Any, opt: float, log: bool = True) -> None:
    """Log x from round 10 and y in [0, 1.6 x optimum]: the first rounds are
    single Bernoulli draws and would otherwise swamp the axis."""
    if log:
        ax.set_xscale("log")
        ax.set_xlim(left=10)
    ax.set_ylim(0, 1.6 * opt)


def _optimum(doc: dict[str, Any]) -> float:
    rows = [r for r in doc["rows"] if r["policy"] == "oracle"] or doc["rows"]
    return float(np.mean([r["optimal_avg_reward"] for r in rows]))


def _band(ax: Any, x: Any, band: dict[str, list[float]], policy: str) -> None:
    color = COLORS.get(policy, ORACLE)
    ax.plot(
        x,
        band["mean"],
        color=color,
        linewidth=2,
        linestyle=STYLES.get(policy, "-"),
        label=LABELS.get(policy, policy),
    )
    ax.fill_between(x, band["lo"], band["hi"], color=color, alpha=0.15, linewidth=0)


def _save(fig: Any, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print(f"wrote {path} ({path.stat().st_size // 1024} KB)", file=sys.stderr)


def _scenario_panels(
    docs: dict[str, Any],
    key: str,
    ylabel: str,
    fig_path: Path,
    log_x: bool,
    title: str,
    optimum: bool = False,
) -> None:
    names = ["clear_winner", "segment_winners", "drift"]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8))
    for ax, name in zip(axes, names, strict=True):
        doc = docs[name]
        agg = doc["aggregate"]
        x = agg["checkpoints"]
        for p in agg["policies"]:
            if p == "oracle" and optimum:
                continue
            _band(ax, x, agg["curves"][p][key], p)
        if optimum:
            ax.axhline(
                _optimum(doc),
                color=ORACLE,
                linestyle="--",
                linewidth=1.5,
                label="Optimum (oracle)",
            )
        # linear-checkpoint runs (drift) stay on a linear x axis
        is_log = log_x and x[0] == 1
        if is_log:
            ax.set_xscale("log")
        if optimum:
            _zoom(ax, _optimum(doc), log=is_log)
        _style(
            ax,
            f"{name} (T={agg['horizon']:,}, {agg['episodes']} episodes)",
            "round",
            ylabel,
        )
    axes[0].legend(
        fontsize=7, loc="best", facecolor="white", edgecolor="none", framealpha=0.9
    )
    axes[2].legend(
        fontsize=7, loc="best", facecolor="white", edgecolor="none", framealpha=0.9
    )
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK)
    _save(fig, fig_path)


def fig_ucb_small_c(doc: dict[str, Any], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7, 4))
    for p in ("ucb1:c=0.25", "ucb1:c=0.01"):
        rows = [r for r in doc["rows"] if r["policy"] == p]
        stuck = sum(r["pct_optimal"] < 0.5 for r in rows)
        for i, r in enumerate(rows):
            ax.plot(
                r["curve"]["checkpoints"],
                r["curve"]["cum_avg_reward"],
                color=COLORS[p],
                linestyle=STYLES.get(p, "-"),
                linewidth=1,
                alpha=0.55,
                label=f"{LABELS[p]}: {stuck}/{len(rows)} episodes <50% optimal"
                if i == 0
                else None,
            )
    ax.axhline(
        _optimum(doc), color=ORACLE, linestyle="--", linewidth=1.5, label="Optimum"
    )
    _zoom(ax, _optimum(doc))
    _style(
        ax,
        "Too-small UCB multiplier settles on a suboptimal arm (one line per episode)",
        "round",
        "cumulative average reward",
    )
    ax.legend(fontsize=8, frameon=False)
    _save(fig, path)


def fig_users(docs: dict[str, Any], path: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), sharey=True)
    titles = {
        "users_1_0": "User 1: segment weights (1, 0)",
        "users_0_1": "User 2: segment weights (0, 1)",
        "users_half_half": "User 3: segment weights (0.5, 0.5)",
    }
    for ax, (name, title) in zip(axes, titles.items(), strict=True):
        doc = docs[name]
        agg = doc["aggregate"]
        for p in agg["policies"]:
            if p != "oracle":
                _band(ax, agg["checkpoints"], agg["curves"][p]["cum_avg_reward"], p)
        ax.axhline(
            _optimum(doc),
            color=ORACLE,
            linestyle="--",
            linewidth=1.5,
            label="Segment optimum",
        )
        _zoom(ax, _optimum(doc))
        _style(ax, title, "round", "cumulative average reward")
    axes[0].legend(fontsize=8, frameon=False)
    fig.suptitle(
        "Per-segment users (segment_winners: mobile_scrollers / trend_followers)",
        x=0.01,
        ha="left",
        fontsize=11,
        color=INK,
    )
    _save(fig, path)


def fig_totals(docs: dict[str, Any], path: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8))
    for ax, name in zip(
        axes, ["clear_winner", "segment_winners", "drift"], strict=True
    ):
        agg = docs[name]["aggregate"]
        pols = agg["policies"]
        for i, p in enumerate(pols):
            t = agg["totals"][p]
            ax.errorbar(
                [i],
                [t["mean"]],
                yerr=[t["std"]],
                fmt="o",
                markersize=8,
                elinewidth=3,
                capsize=0,
                color=COLORS.get(p, ORACLE),
            )
        ax.set_xticks(range(len(pols)))
        ax.set_xticklabels(
            [LABELS.get(p, p) for p in pols], rotation=30, ha="right", fontsize=7
        )
        _style(ax, name, "", "expected total reward ± std")
    fig.suptitle(
        "Expected total reward per episode (mean ± std across episodes)",
        x=0.01,
        ha="left",
        fontsize=11,
        color=INK,
    )
    _save(fig, path)


def fig_arm_stats(docs: dict[str, Any], path: Path) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(11, 6.5))
    for row, name in enumerate(["clear_winner", "segment_winners"]):
        agg = docs[name]["aggregate"]
        arms = agg["arms"]
        n_ep = agg["episodes"]  # aggregate impressions are summed over episodes
        ids = [a["creative_id"].replace("synthetic-", "") for a in arms]
        x = np.arange(len(arms))
        ax = axes[row, 0]
        ax.bar(
            x,
            [a["impressions"] / n_ep for a in arms],
            color=COLORS["linear_ts"],
            width=0.6,
        )
        for xi, a in zip(x, arms, strict=True):
            ax.text(
                xi,
                a["impressions"] / n_ep,
                f"{a['impressions'] / n_ep:,.0f}",
                ha="center",
                va="bottom",
                fontsize=8,
                color=INK,
            )
        ax.set_xticks(x, ids)
        _style(
            ax,
            f"{name}: LinTS impressions per arm (mean per episode)",
            "arm",
            "impressions",
        )
        ax = axes[row, 1]
        w = 0.38
        ax.bar(
            x - w / 2,
            [100 * a["estimated_ctr"] for a in arms],
            width=w,
            color=COLORS["linear_ts"],
            label="estimated CTR (clicks / impressions)",
        )
        ax.bar(
            x + w / 2,
            [100 * a["true_ctr"] for a in arms],
            width=w,
            color="#b8b7b1",
            label="true CTR (traffic-weighted)",
        )
        ax.set_xticks(x, ids)
        top = max(max(a["estimated_ctr"], a["true_ctr"]) for a in arms)
        ax.set_ylim(0, 130 * top)  # headroom for the legend
        _style(ax, f"{name}: estimated vs true CTR", "arm", "CTR (%)")
        ax.legend(fontsize=7, frameon=False)
    _save(fig, path)


def fig_drift(doc: dict[str, Any], path: Path) -> None:
    agg = doc["aggregate"]
    cps = np.array(agg["checkpoints"])
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    for p in agg["policies"]:
        if p == "oracle":
            continue
        cum = np.array(agg["curves"][p]["pct_optimal"]["mean"])
        hits = cum * cps
        window = np.diff(np.r_[0, hits]) / np.diff(np.r_[0, cps])
        axes[0].plot(
            cps,
            window,
            color=COLORS.get(p, ORACLE),
            linewidth=2,
            linestyle=STYLES.get(p, "-"),
            label=LABELS.get(p, p),
        )
        _band(axes[1], cps, agg["curves"][p]["cum_regret"], p)
    for ax in axes:
        ax.axvline(agg["horizon"] / 2, color=MUTED, linewidth=1, linestyle=":")
    _style(
        axes[0],
        "Share of optimal pulls per window (best arm becomes worst at T/2)",
        "round",
        "% optimal in window",
    )
    _style(axes[1], "Cumulative regret ± 95% CI", "round", "pseudo-regret")
    axes[0].legend(fontsize=8, frameon=False)
    _save(fig, path)


def fig_injection(doc: dict[str, Any], path: Path) -> None:
    agg = doc["aggregate"]
    cps = np.array(agg["checkpoints"])
    half = agg["horizon"] // 2
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    for p in agg["policies"]:
        if p != "oracle":
            _band(axes[0], cps, agg["curves"][p]["cum_avg_reward"], p)
    orows = [r for r in doc["rows"] if r["policy"] == "oracle"]
    ocurve = np.mean([r["curve"]["cum_avg_reward"] for r in orows], axis=0)
    axes[0].plot(
        cps,
        ocurve,
        color=ORACLE,
        linestyle="--",
        linewidth=1.5,
        label="Oracle (cumulative)",
    )
    axes[0].axvline(half, color=MUTED, linewidth=1, linestyle=":")
    _style(
        axes[0],
        "Arm injection at T/2: new best arm 'a' replaces 'd'",
        "round",
        "cumulative average reward",
    )
    axes[0].legend(fontsize=8, frameon=False)
    share = agg["arm_share"]
    bottom = np.zeros(len(cps))
    arm_colors = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
    for color, (cid, vals) in zip(arm_colors, share.items(), strict=False):
        vals = np.array(vals)
        axes[1].fill_between(
            cps,
            bottom,
            bottom + vals,
            color=color,
            alpha=0.85,
            linewidth=0,
            label=cid.replace("synthetic-", "arm "),
        )
        bottom += vals
    axes[1].axvline(half, color=INK, linewidth=1, linestyle=":")
    _style(axes[1], "LinTS arm share per window", "round", "share of pulls")
    axes[1].legend(fontsize=8, frameon=False, loc="upper left")
    _save(fig, path)


def plot_all(data_dir: Path, fig_dir: Path) -> None:
    fig_dir.mkdir(parents=True, exist_ok=True)
    docs = {p.stem: json.loads(p.read_text()) for p in sorted(data_dir.glob("*.json"))}
    _scenario_panels(
        docs,
        "cum_avg_reward",
        "cumulative average reward",
        fig_dir / "01_cum_avg_reward.png",
        log_x=True,
        title="Cumulative average reward vs optimum (mean ± 95% CI)",
        optimum=True,
    )
    fig_ucb_small_c(docs["ucb_small_c"], fig_dir / "02_ucb_small_multiplier.png")
    fig_users(docs, fig_dir / "03_segment_users.png")
    fig_totals(docs, fig_dir / "04_expected_total_reward.png")
    fig_arm_stats(docs, fig_dir / "05_arm_impressions_ctr.png")
    _scenario_panels(
        docs,
        "cum_regret",
        "pseudo-regret",
        fig_dir / "06_cum_regret.png",
        log_x=False,
        title="Cumulative regret (mean ± 95% CI)",
    )
    _scenario_panels(
        docs,
        "pct_optimal",
        "cumulative % optimal",
        fig_dir / "07_pct_optimal.png",
        log_x=True,
        title="Share of optimal decisions (cumulative, mean ± 95% CI)",
    )
    fig_drift(docs["drift"], fig_dir / "08_drift_recovery.png")
    fig_injection(docs["injection"], fig_dir / "09_arm_injection.png")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--generate", action="store_true", help="run the simulations first")
    ap.add_argument("--data-dir", type=Path, default=Path("/tmp/bandit_parity"))
    ap.add_argument("--fig-dir", type=Path, default=REPO / "experiments/bandit/figures")
    ap.add_argument("--episodes", type=int, default=20)
    ap.add_argument(
        "--horizon-scale",
        type=float,
        default=1.0,
        help="multiply every preset horizon (tests use a tiny value)",
    )
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    if args.generate:
        generate(args.data_dir, args.episodes, args.horizon_scale, args.seed)
    plot_all(args.data_dir, args.fig_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
