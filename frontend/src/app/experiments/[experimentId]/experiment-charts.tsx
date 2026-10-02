"use client";

import type { ReactNode } from "react";
import { BarChart } from "@/components/charts/bar-chart";
import { LineChart, Swatch } from "@/components/charts/line-chart";
import { formatCompact, formatInt, formatPercent } from "@/lib/chart";
import {
  armShareSeries,
  armStatRows,
  policyShortLabel,
  curveSeries,
  segmentRows,
  shortId,
  totalBars,
  type Arm,
  type ExperimentMetrics,
} from "@/lib/experiments";

function ChartPanel({
  title,
  note,
  children,
  className,
}: {
  title: string;
  note?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border border-border bg-card p-4 ${className ?? ""}`}>
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {note && <p className="mt-0.5 text-xs text-muted-foreground">{note}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

const roundLabel = "Round (log scale)";

/** Segment keys arrive snake_case ("mobile_young"); show them as words. */
const segmentName = (s: string) => {
  const t = s.replace(/[_-]+/g, " ").trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
};

/** All experiment charts and tables for one metrics payload (non-empty). */
export function ExperimentCharts({
  metrics,
  arms,
  rewardMode,
}: {
  metrics: ExperimentMetrics;
  arms: Arm[];
  rewardMode: string;
}) {
  const clickReward = rewardMode !== "engaged";
  const formatReward = (v: number) => (clickReward ? formatPercent(v, 1) : `${formatCompact(v)} s`);
  const rewardAxis = clickReward ? "Click rate so far" : "Engaged seconds per round so far";
  const pct = (v: number) => formatPercent(v);

  const avg = curveSeries(metrics, "cumAvgReward", { includeOracle: true });
  const regret = curveSeries(metrics, "cumRegret", { bands: true });
  const optimal = curveSeries(metrics, "pctOptimal", { bands: true });
  const share = armShareSeries(metrics, arms);
  const segments = segmentRows(metrics, arms);
  const totals = totalBars(metrics);
  const armRows = armStatRows(metrics, arms);
  const ep = `${metrics.episodes} ${metrics.episodes === 1 ? "episode" : "episodes"}`;

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <ChartPanel
        title="Cumulative average reward against the optimum"
        note={`Mean of ${ep}. The dashed line is the oracle, which always shows the best creative for the reader.`}
      >
        <LineChart
          title="Cumulative average reward by policy, with the oracle as reference"
          series={avg}
          xLabel={roundLabel}
          yLabel={rewardAxis}
          logX
          yZero={false}
          formatY={formatReward}
        />
      </ChartPanel>

      <ChartPanel
        title="Cumulative regret"
        note="Reward lost against the oracle; flatter is better. Bands are 95% intervals across episodes."
      >
        <LineChart
          title="Cumulative pseudo-regret by policy, with 95% confidence bands"
          series={regret}
          xLabel={roundLabel}
          yLabel={clickReward ? "Clicks lost" : "Seconds lost"}
          logX
        />
      </ChartPanel>

      <ChartPanel
        title="Share of rounds on the best creative"
        note="How often each policy showed the reader's optimal creative. Bands are 95% intervals."
      >
        <LineChart
          title="Percent of rounds choosing the optimal creative, by policy"
          series={optimal}
          xLabel={roundLabel}
          yLabel="Optimal choices"
          logX
          yDomain={[0, 1]}
          formatY={pct}
        />
      </ChartPanel>

      <ChartPanel
        title="Where the endpoint sends traffic"
        note="Share of each checkpoint window's impressions per creative, for linear Thompson sampling."
      >
        {share.length ? (
          <LineChart
            title="Linear Thompson sampling: share of impressions per creative over time"
            series={share}
            xLabel={roundLabel}
            yLabel="Share of impressions"
            logX
            yDomain={[0, 1]}
            formatY={pct}
            directLabels={false}
          />
        ) : (
          <p className="text-sm text-muted-foreground">No arm share recorded yet.</p>
        )}
      </ChartPanel>

      <ChartPanel
        title="Winners by reader segment"
        note="Each segment's optimal creative, and how often each policy found it."
      >
        {segments.length ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabular-nums">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th scope="col" className="py-1.5 pr-3 font-medium">Segment</th>
                  <th scope="col" className="py-1.5 pr-3 font-medium">Best creative</th>
                  <th scope="col" className="py-1.5 pr-3 text-right font-medium">Linear TS</th>
                  <th scope="col" className="py-1.5 text-right font-medium">Best baseline</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {segments.map((s) => {
                  const ahead =
                    s.linearTs !== null && s.bestBaseline !== null && s.linearTs >= s.bestBaseline.pctOptimal;
                  return (
                    <tr key={s.segment}>
                      <th scope="row" className="py-2 pr-3 text-left font-medium text-foreground">
                        {segmentName(s.segment)}
                      </th>
                      <td className="max-w-[12rem] truncate py-2 pr-3 text-foreground/80" title={s.optimalArm}>
                        {s.optimalArm}
                      </td>
                      <td className={`py-2 pr-3 text-right ${ahead ? "font-semibold text-foreground" : "text-foreground"}`}>
                        {s.linearTs === null ? "–" : pct(s.linearTs)}
                      </td>
                      <td className="py-2 text-right text-muted-foreground">
                        {s.bestBaseline ? (
                          <>
                            {pct(s.bestBaseline.pctOptimal)}{" "}
                            <span className="text-xs">({s.bestBaseline.label})</span>
                          </>
                        ) : (
                          "–"
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No segment breakdown yet.</p>
        )}
      </ChartPanel>

      <ChartPanel
        title="Expected total reward per episode"
        note={`Mean ± one standard deviation across ${ep}${
          metrics.horizon ? ` of ${formatInt(metrics.horizon)} rounds` : ""
        }.`}
      >
        <BarChart
          title="Expected total reward per episode by policy, mean plus or minus one standard deviation"
          bars={totals.map((b) => ({ id: b.id, label: policyShortLabel(b.id), color: b.color, mean: b.mean, err: b.std }))}
          xLabel={clickReward ? "Total clicks per episode" : "Total engaged seconds per episode"}
          format={formatInt}
        />
      </ChartPanel>

      <ChartPanel
        title="Impressions and click rates by creative"
        note="Linear Thompson sampling's estimate against the simulator's true click rate."
        className="lg:col-span-2"
      >
        <div className="overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th scope="col" className="py-1.5 pr-3 font-medium">Creative</th>
                <th scope="col" className="py-1.5 pr-3 font-medium">Id</th>
                <th scope="col" className="py-1.5 pr-3 text-right font-medium">Impressions</th>
                <th scope="col" className="py-1.5 pr-3 text-right font-medium">Estimated click rate</th>
                <th scope="col" className="py-1.5 text-right font-medium">True click rate</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {armRows.map((r) => (
                <tr key={r.creativeId}>
                  <th scope="row" className="py-2 pr-3 text-left font-medium text-foreground">
                    <span className="inline-flex items-center gap-2">
                      <Swatch color={r.color} />
                      {r.name}
                    </span>
                  </th>
                  <td className="py-2 pr-3 font-mono text-xs text-muted-foreground">{shortId(r.creativeId)}</td>
                  <td className="py-2 pr-3 text-right text-foreground">{formatInt(r.impressions)}</td>
                  <td className="py-2 pr-3 text-right text-foreground">{formatPercent(r.estimatedCtr, 2)}</td>
                  <td className="py-2 text-right text-muted-foreground">{formatPercent(r.trueCtr, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ChartPanel>
    </div>
  );
}
