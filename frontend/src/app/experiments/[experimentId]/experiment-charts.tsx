"use client";

import type { ReactNode } from "react";
import { InfoTip } from "@/components/ui/info-tip";
import { CHART_HELP, CONTINUOUS_CHART_HELP, SHIFT_HELP } from "@/lib/experiment-help";
import { CHART_EXPLAIN, CONTINUOUS_EXPLAIN, SHIFT_EXPLAIN } from "@/lib/experiment-explain";
import type { ChartReadings } from "@/lib/experiment-insights";
import { ExplainPanel } from "@/components/explain";
import { BarChart } from "@/components/charts/bar-chart";
import { LineChart, Swatch } from "@/components/charts/line-chart";
import { formatCompact, formatInt, formatPercent } from "@/lib/chart";
import {
  armShareSeries,
  armStatRows,
  policyShortLabel,
  curveSeries,
  segmentLabel,
  segmentRows,
  shortId,
  totalBars,
  armColor,
  armName,
  GHOST_POLICY,
  type Arm,
  type ExperimentMetrics,
} from "@/lib/experiments";
import { parseShiftResponse, recoverySpans, type ChartMarker, type RunView } from "@/lib/shifts";

/** A continuous run's shape for the charts (contracts §11). */
export interface ContinuousChartView {
  segments: number;
  /** Rounds per segment (T). */
  segmentRounds: number | null;
  /** Rounds in the whole stream (segments × T). */
  totalRounds: number | null;
  /** Subtle segment-boundary ticks (`segmentMarkers`). */
  markers: ChartMarker[];
}

function ChartPanel({
  title,
  note,
  help,
  helpLabel,
  reading,
  explain,
  explainOpen = false,
  children,
  className,
}: {
  title: string;
  /** One-line interpretation of this chart's data, always visible under it. */
  reading?: string | null;
  /** "How to read this" guidance, revealed by the Explain switch. */
  explain?: string;
  explainOpen?: boolean;
  note?: string;
  /** Plain-language explanation shown in the title's "ⓘ" popover. */
  help: string;
  /** Accessible name for the "ⓘ" trigger, e.g. "About cumulative regret". */
  helpLabel: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border border-border bg-card p-4 ${className ?? ""}`}>
      <div className="flex items-start gap-1.5">
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        <InfoTip label={helpLabel} className="mt-0.5">
          {help}
        </InfoTip>
      </div>
      {note && <p className="mt-0.5 text-xs text-muted-foreground">{note}</p>}
      <div className="mt-3">{children}</div>
      {reading && (
        <p className="mt-3 border-t border-border pt-2.5 text-sm leading-snug text-muted-foreground">{reading}</p>
      )}
      {explain && <ExplainPanel open={explainOpen}>{explain}</ExplainPanel>}
    </section>
  );
}


const segmentName = segmentLabel;

/** All experiment charts and tables for one metrics payload (non-empty). */
export function ExperimentCharts({
  metrics,
  arms,
  rewardMode,
  readings,
  explain = false,
  runView = null,
  continuous = null,
}: {
  metrics: ExperimentMetrics;
  arms: Arm[];
  rewardMode: string;
  readings?: ChartReadings;
  /** Explain mode: reveal each panel's "How to read this". */
  explain?: boolean;
  /** The shown run's shifts (contracts §10): markers, linear rounds, ghost, per-period tables. */
  runView?: RunView | null;
  /** The shown run kept learning (§11): one stitched stream on global rounds, linear x, no bands, segment ticks. */
  continuous?: ContinuousChartView | null;
}) {
  const shifted = Boolean(runView);
  const linear = shifted || Boolean(continuous);
  const roundLabel = continuous ? "Round (whole run)" : shifted ? "Round" : "Round (log scale)";
  const markers = [...(continuous?.markers ?? []), ...(runView?.markers ?? [])];
  const spans = shifted
    ? recoverySpans(parseShiftResponse(metrics.shiftResponse), runView?.horizon ?? metrics.horizon)
    : [];
  const help = (key: keyof typeof CHART_HELP) =>
    (continuous ? CONTINUOUS_CHART_HELP[key] : undefined) ?? CHART_HELP[key];
  const explainFor = (base: string) =>
    [base, shifted ? SHIFT_EXPLAIN.markers : ""].filter(Boolean).join(" ");
  const hasGhost = Boolean(metrics.curves?.[GHOST_POLICY]);
  const ghostNote = hasGhost ? " The dashed blue line is Linear TS on the same readers without your shifts." : "";
  const clickReward = rewardMode !== "engaged";
  const formatReward = (v: number) => (clickReward ? formatPercent(v, 1) : `${formatCompact(v)} s`);
  const rewardAxis = clickReward ? "Click rate so far" : "Engaged seconds per round so far";
  const pct = (v: number) => formatPercent(v);

  const avg = curveSeries(metrics, "cumAvgReward", { includeOracle: true, continuous: Boolean(continuous) });
  const regret = curveSeries(metrics, "cumRegret", { bands: true, continuous: Boolean(continuous) });
  const optimal = curveSeries(metrics, "pctOptimal", { bands: true, continuous: Boolean(continuous) });
  // Drop the tiny windows merged in around a shift (their shares are noise): 1% of an episode, or of a segment.
  const windowBase = continuous ? continuous.segmentRounds : metrics.horizon;
  const share = armShareSeries(metrics, arms, linear && windowBase ? { minWindow: windowBase * 0.01 } : {});
  const segments = segmentRows(metrics, arms);
  const totals = totalBars(metrics);
  const armRows = armStatRows(metrics, arms);
  const ep = continuous
    ? `${metrics.episodes} ${metrics.episodes === 1 ? "segment" : "segments"}`
    : `${metrics.episodes} ${metrics.episodes === 1 ? "episode" : "episodes"}`;
  const segmentRounds = continuous ? continuous.segmentRounds : metrics.horizon;
  const noBands = "No bands: one continuous stream has no independent repeats to build them from.";
  const mr = runView?.metricRegimes ?? [];
  const periodRows = mr.length >= 2 ? mr : null;
  const latestTrue =
    mr.length >= 2
      ? {
          label: runView?.labels[runView.regimes.findIndex((g) => g.start === mr[mr.length - 1].start)] ??
            "latest period",
          now: mr[mr.length - 1].trueCtr,
          before: mr[0].trueCtr,
        }
      : null;

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <p className="text-sm text-muted-foreground lg:col-span-2">
        In the first three charts each line is a <span className="text-foreground">strategy</span> for
        choosing which creative to show: <span className="text-foreground">Linear TS</span> is your live
        endpoint, the others are baselines replayed on the same simulated readers, and the oracle always
        knows the best creative. The traffic chart and the table below show the{" "}
        <span className="text-foreground">creatives</span> themselves, matched to the coloured dots on the
        creative cards.
      </p>
      <ChartPanel
        title="Cumulative average reward against the optimum"
        help={hasGhost ? `${help("avgReward")} ${SHIFT_HELP.ghost}` : help("avgReward")}
        reading={readings?.avgReward}
        explain={explainFor(continuous ? `${CHART_EXPLAIN.avgReward} ${CONTINUOUS_EXPLAIN.stream}` : CHART_EXPLAIN.avgReward)}
        explainOpen={explain}
        helpLabel="About cumulative average reward"
        note={`${continuous ? `One stream of ${ep}; the ticks on the axis mark where each starts.` : `Mean of ${ep}.`} The black dashed line is the oracle, which always shows the best creative for the reader.${ghostNote}`}
      >
        <LineChart
          title="Cumulative average reward by strategy, with the oracle as reference"
          series={avg}
          xLabel={roundLabel}
          yLabel={rewardAxis}
          logX={!linear}
          markers={markers}
          minX={linear ? 1000 : 100}
          yZero={false}
          formatY={formatReward}
        />
      </ChartPanel>

      <ChartPanel
        title="Cumulative regret"
        help={help("regret")}
        reading={readings?.regret}
        explain={explainFor(continuous ? CONTINUOUS_EXPLAIN.regret : CHART_EXPLAIN.regret)}
        explainOpen={explain}
        helpLabel="About cumulative regret"
        note={`Reward lost against the oracle; flatter is better. ${continuous ? noBands : "Bands are 95% intervals across episodes."}${
          spans.length ? " Shaded: rounds until the endpoint's best-creative rate was back to 80% of its pre-shift level." : ""
        }`}
      >
        <LineChart
          title={
            continuous
              ? "Cumulative pseudo-regret by strategy over the whole run"
              : "Cumulative pseudo-regret by strategy, with 95% confidence bands"
          }
          series={regret}
          xLabel={roundLabel}
          yLabel={clickReward ? "Clicks lost" : "Seconds lost"}
          logX={!linear}
          markers={markers}
          spans={spans}
          minX={100}
        />
      </ChartPanel>

      <ChartPanel
        title="Share of rounds on the best creative"
        help={hasGhost ? `${help("optimalShare")} ${SHIFT_HELP.ghost}` : help("optimalShare")}
        reading={readings?.optimal}
        explain={explainFor(CHART_EXPLAIN.optimalShare)}
        explainOpen={explain}
        helpLabel="About share of rounds on the best creative"
        note={`How often each strategy showed the reader's optimal creative. ${
          continuous ? "Rates so far over the whole stream, without bands." : "Bands are 95% intervals."
        }${ghostNote}`}
      >
        <LineChart
          title="Percent of rounds choosing the optimal creative, by strategy"
          series={optimal}
          xLabel={roundLabel}
          yLabel="Optimal choices"
          logX={!linear}
          markers={markers}
          minX={linear ? 1000 : 100}
          yDomain={[0, 1]}
          formatY={pct}
        />
      </ChartPanel>

      <ChartPanel
        title="Where the endpoint sends traffic"
        help={CHART_HELP.trafficShare}
        reading={readings?.share}
        explain={CHART_EXPLAIN.trafficShare}
        explainOpen={explain}
        helpLabel="About where the endpoint sends traffic"
        note="Each line is one creative: its share of the live endpoint's impressions over time."
      >
        {share.length ? (
          <LineChart
            title="Linear Thompson sampling: share of impressions per creative over time"
            series={share}
            xLabel={roundLabel}
            yLabel="Share of impressions"
            logX={!linear}
            markers={markers}
            minX={100}
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
        help={CHART_HELP.segments}
        reading={readings?.segments}
        explain={CHART_EXPLAIN.segments}
        explainOpen={explain}
        helpLabel="About winners by reader segment"
        note={
          periodRows
            ? "Each period's optimal creative per segment, and how often Linear TS and the best baseline found it then."
            : "Each segment's optimal creative, and how often each strategy found it."
        }
        className={periodRows ? "lg:col-span-2" : undefined}
      >
        {periodRows && runView ? (
          <PeriodWinners rows={periodRows} labels={runView.labels} arms={arms} />
        ) : segments.length ? (
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
        title={continuous ? "Expected total reward per segment" : "Expected total reward per episode"}
        help={help("totals")}
        reading={readings?.totals}
        explain={CHART_EXPLAIN.totals}
        explainOpen={explain}
        helpLabel={continuous ? "About expected total reward per segment" : "About expected total reward per episode"}
        note={`Mean ± one standard deviation across ${ep}${segmentRounds ? ` of ${formatInt(segmentRounds)} rounds` : ""}.${
          continuous ? " Early segments include the learning, so the spread is not an error bar." : ""
        }`}
      >
        <BarChart
          title={`Expected total reward per ${continuous ? "segment" : "episode"} by strategy, mean plus or minus one standard deviation`}
          bars={totals.map((b) => ({
            id: b.id,
            label: policyShortLabel(b.id),
            // The ghost is the endpoint's counterfactual: the same hue, lighter.
            color: b.id === GHOST_POLICY ? "#9cc0ec" : b.color,
            mean: b.mean,
            err: b.std,
          }))}
          xLabel={`${clickReward ? "Total clicks" : "Total engaged seconds"} per ${continuous ? "segment" : "episode"}`}
          format={formatInt}
        />
      </ChartPanel>

      <ChartPanel
        title="Impressions and click rates by creative"
        help={CHART_HELP.armTable}
        explain={CHART_EXPLAIN.armTable}
        explainOpen={explain}
        helpLabel="About impressions and click rates"
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
                <th scope="col" className="py-1.5 text-right font-medium">
                  {latestTrue ? `True click rate, ${latestTrue.label.toLowerCase()}` : "True click rate"}
                </th>
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
                  <td className="py-2 text-right text-muted-foreground">
                    {latestTrue && typeof latestTrue.now[r.creativeId] === "number" ? (
                      <>
                        <span className="text-foreground">{formatPercent(latestTrue.now[r.creativeId], 2)}</span>
                        {typeof latestTrue.before[r.creativeId] === "number" && (
                          <span className="block text-xs">before {formatPercent(latestTrue.before[r.creativeId], 2)}</span>
                        )}
                      </>
                    ) : (
                      formatPercent(r.trueCtr, 2)
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ChartPanel>
    </div>
  );
}

/**
 * Winners by reader segment, one column per period between shifts: the best
 * creative then, and how often Linear TS and the best baseline found it.
 */
function PeriodWinners({
  rows,
  labels,
  arms,
}: {
  rows: NonNullable<RunView["metricRegimes"]>;
  labels: string[];
  arms: Arm[];
}) {
  const segs = [...new Set(rows.flatMap((r) => Object.keys(r.perSegment)))].sort();
  const name = (id: string) => {
    const a = arms.find((x) => x.creativeId === id);
    return a ? armName(a) : shortId(id);
  };
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[44rem] text-sm tabular-nums">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            <th scope="col" className="py-1.5 pr-3 font-medium">
              Segment
            </th>
            {rows.map((r, i) => (
              <th key={r.start} scope="col" className="py-1.5 pr-3 font-medium">
                <span className="block text-foreground">{labels[i] ?? `Period ${i + 1}`}</span>
                <span className="font-normal">
                  rounds {formatInt(r.start + 1)}–{formatInt(r.end)}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {segs.map((seg) => (
            <tr key={seg}>
              <th scope="row" className="py-2 pr-3 text-left align-top font-medium text-foreground">
                {segmentName(seg)}
              </th>
              {rows.map((r, i) => {
                const cell = r.perSegment[seg];
                if (!cell) {
                  return (
                    <td key={r.start} className="py-2 pr-3 text-muted-foreground">
                      –
                    </td>
                  );
                }
                const changed = i > 0 && rows[i - 1].perSegment[seg]?.optimalArm !== cell.optimalArm;
                const lin = cell.policies.linear_ts?.pctOptimal;
                let best: { p: string; v: number } | null = null;
                for (const [p, v] of Object.entries(cell.policies)) {
                  if (p === "linear_ts" || p === "oracle" || p === GHOST_POLICY) continue;
                  if (!best || v.pctOptimal > best.v) best = { p, v: v.pctOptimal };
                }
                return (
                  <td key={r.start} className="max-w-[13rem] py-2 pr-3 align-top">
                    <span className={`flex min-w-0 items-center gap-1.5 ${changed ? "font-semibold" : ""} text-foreground`}>
                      <span
                        aria-hidden
                        className="size-2 shrink-0 rounded-full"
                        style={{ backgroundColor: armColor(arms, cell.optimalArm) }}
                      />
                      <span className="truncate" title={name(cell.optimalArm)}>
                        {name(cell.optimalArm)}
                      </span>
                      {changed && <span className="sr-only"> (new best in this period)</span>}
                    </span>
                    <span className="mt-0.5 block text-xs text-muted-foreground">
                      Linear TS {typeof lin === "number" ? formatPercent(lin) : "–"}
                      {best ? `, ${policyShortLabel(best.p)} ${formatPercent(best.v)}` : ""}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted-foreground">Bold: the best creative changed in that period.</p>
    </div>
  );
}
