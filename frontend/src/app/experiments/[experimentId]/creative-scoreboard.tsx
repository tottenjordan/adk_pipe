"use client";

import { useState, type MouseEvent, type PointerEvent } from "react";
import { ChevronRightIcon } from "lucide-react";
import { ExplainPanel } from "@/components/explain";
import { ProofImage } from "@/app/results/[sessionId]/proof-grid";
import { CONDENSED } from "@/app/results/[sessionId]/score-mark";
import { formatInt, formatPercent } from "@/lib/chart";
import { SCOREBOARD_EXPLAIN } from "@/lib/experiment-explain";
import type { ExperimentInsights } from "@/lib/experiment-insights";
import { armImageUrl, segmentLabel, type CreativeSeries } from "@/lib/experiments";
import { evenSplit, periodAt, shareY, sharedShareMax, stripPaths, type Lane, type LaneRegime } from "@/lib/scoreboard";
import type { RunView } from "@/lib/shifts";
import { ShiftResults } from "./shift-results";
import { cn } from "@/lib/utils";

const STRIP_W = 300;
const STRIP_H = 64;

const LANE_GRID =
  "grid grid-cols-[4rem_minmax(0,1fr)] gap-x-4 gap-y-3 lg:grid-cols-[1.25rem_4rem_minmax(0,1fr)_6.5rem_6.5rem_6.5rem_minmax(0,1.35fr)_1.5rem] lg:items-center lg:gap-x-4";

/** The experiment Overview: one interpretation sentence, then one lane per creative. */
export function CreativeScoreboard({
  lanes,
  insights,
  series,
  explain,
  emptyMessage,
  onOpen,
  runView = null,
  laneRegimes = {},
}: {
  /** The shown run's shifts: strip ticks, per-period hover, result cards (contracts §10). */
  runView?: RunView | null;
  /** Latest + before period per creative (click rate, segments won). */
  laneRegimes?: Record<string, LaneRegime>;
  lanes: Lane[];
  insights: ExperimentInsights;
  series: CreativeSeries | null;
  explain: boolean;
  /** Direction shown in place of the headline before there are results. */
  emptyMessage: string;
  /** Open the creative detail drawer for a row. */
  onOpen?: (creativeId: string) => void;
}) {
  const k = lanes.length;
  const yMax = sharedShareMax(lanes, k);
  const even = evenSplit(k);
  const hasResults = insights.verdict !== "empty";
  const windows = series?.windows ?? [];
  const horizon = runView?.horizon ?? series?.horizon ?? null;
  const ticks =
    runView && horizon ? runView.markers.map((m) => ({ frac: m.x / horizon, label: m.label })) : [];
  const anyRegime = Object.values(laneRegimes)[0] ?? null;

  return (
    <section aria-labelledby="scoreboard-heading">
      <div className="max-w-[60rem]">
        {hasResults ? (
          <h2
            id="scoreboard-heading"
            className={cn(
              CONDENSED,
              "text-[2rem] leading-[1.04] tracking-[-0.01em] text-balance text-foreground [word-spacing:0.06em] sm:text-[2.75rem]"
            )}
          >
            {insights.headline}
          </h2>
        ) : (
          <h2 id="scoreboard-heading" className="text-lg font-semibold text-foreground">
            {emptyMessage}
          </h2>
        )}
        {hasResults && insights.detail && (
          <p className="mt-4 max-w-[72ch] text-base leading-snug text-foreground">{insights.detail}</p>
        )}
        {hasResults && insights.support && (
          <p className="mt-1.5 max-w-[72ch] text-sm leading-relaxed text-muted-foreground">{insights.support}</p>
        )}
        {hasResults && insights.why && (
          <p className="mt-3 max-w-[72ch] text-sm leading-relaxed text-foreground">{insights.why}</p>
        )}
        {hasResults && insights.whyExplain && (
          <ExplainPanel open={explain}>
            {insights.whyExplain}
          </ExplainPanel>
        )}
        {hasResults && <ShiftResults cards={insights.shiftCards} ghost={insights.ghost} explain={explain} />}
        {insights.notes.map((note) => (
          <p key={note} className="mt-2 max-w-[72ch] border-l-2 border-foreground/25 pl-3 text-sm leading-snug text-foreground">
            {note}
          </p>
        ))}
      </div>

      <div className="mt-8 rounded-lg border border-border bg-card">
        {/* Column labels for sighted desktop readers; each lane repeats them as sr-only text. */}
        <div
          aria-hidden
          className={cn(
            LANE_GRID,
            "hidden border-b border-border px-4 py-2 text-xs text-muted-foreground lg:grid"
          )}
        >
          <span />
          <span className="col-span-2">Creative</span>
          <span className="text-right">Traffic now</span>
          <span className="text-right">Click rate</span>
          <span className="text-right">Segments won</span>
          <span className="flex flex-wrap justify-between gap-x-3">
            <span>Traffic share over the run{hasResults ? `, 0–${formatPercent(yMax)}` : ""}</span>
            {hasResults && even > 0 && (
              <span className="inline-flex items-center gap-1.5 tabular-nums">
                <span className="h-px w-4 bg-[#9aa5ae]" />
                Even split {formatPercent(even)}
              </span>
            )}
          </span>
          <span />
        </div>

        <ExplainPanel open={explain} className="px-4">
          {SCOREBOARD_EXPLAIN.board}
        </ExplainPanel>
        {hasResults && anyRegime && (
          <p className="border-b border-border px-4 py-2 text-xs text-muted-foreground tabular-nums">
            Click rate and segments won are for the last period ({anyRegime.latest.label.toLowerCase()}, rounds{" "}
            {formatInt(anyRegime.latest.start + 1)}–{formatInt(anyRegime.latest.end)}); the small line under each is{" "}
            {anyRegime.before.label.toLowerCase()} your shifts. Ticks on the strips mark the shifts.
          </p>
        )}

        <ol className="divide-y divide-border">
          {lanes.map((lane) => (
            <li
              key={lane.creativeId}
              // The row's button is the accessible control; a click anywhere else on
              // the row is a mouse shortcut for it (Explain text excepted).
              onClick={onOpen ? (e) => rowClick(e, () => onOpen(lane.creativeId)) : undefined}
              className={cn(
                "relative px-4 py-4",
                onOpen &&
                  "cursor-pointer hover:bg-muted/45 has-[[data-row-open]:focus-visible]:bg-muted/45"
              )}
            >
              <div className={LANE_GRID}>
                {/* Ranks only mean something once traffic has run. */}
                <span
                  aria-label={hasResults ? `Rank ${lane.rank}` : undefined}
                  className={cn(CONDENSED, "hidden text-lg text-muted-foreground tabular-nums lg:block")}
                >
                  {hasResults ? lane.rank : ""}
                </span>
                <ProofImage
                  src={lane.arm ? armImageUrl(lane.arm) : null}
                  alt={lane.name}
                  className="size-16 text-[0px]"
                />
                <div className="min-w-0 self-center pr-8 lg:pr-0">
                  <p className="flex items-center gap-2 text-sm font-semibold text-foreground">
                    <span aria-hidden className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: lane.color }} />
                    <span className="truncate" title={lane.name}>
                      {hasResults && <span className="text-muted-foreground lg:hidden">{lane.rank}. </span>}
                      {lane.name}
                    </span>
                  </p>
                  {lane.headline && (
                    <p className="mt-0.5 line-clamp-2 pl-[1.125rem] text-sm text-muted-foreground">{lane.headline}</p>
                  )}
                </div>

                <div className="col-span-2 grid grid-cols-3 gap-4 lg:contents">
                  <Figure label="Traffic now" value={lane.finalShare === null ? "–" : formatPercent(lane.finalShare)} />
                  {laneRegimes[lane.creativeId] ? (
                    <PeriodFigures r={laneRegimes[lane.creativeId]} />
                  ) : (
                  <>
                  <Figure
                    label="Click rate"
                    value={lane.clickRate === null ? "–" : formatPercent(lane.clickRate, 1)}
                    note={lane.trueCtr === null ? undefined : `true ${formatPercent(lane.trueCtr, 1)}`}
                    title={
                      lane.clicks !== null && lane.impressions !== null
                        ? `${formatInt(lane.clicks)} clicks from ${formatInt(lane.impressions)} impressions`
                        : undefined
                    }
                  />
                  <Figure
                    label="Segments won"
                    value={hasResults ? String(lane.segmentsWon.length) : "–"}
                    note={hasResults && lane.segmentsWon.length ? undefined : hasResults ? "none" : undefined}
                    title={lane.segmentsWon.length ? lane.segmentsWon.map(segmentLabel).join(", ") : undefined}
                  />
                  </>
                  )}
                </div>

                <div className="col-span-2 lg:col-span-1">
                  {hasResults && (
                    <p className="mb-1.5 text-xs text-muted-foreground lg:hidden">
                      Share of traffic over the run, 0–{formatPercent(yMax)}; grey line is an even split
                    </p>
                  )}
                  <ShareStrip
                    lane={lane}
                    yMax={yMax}
                    even={even}
                    windows={windows}
                    ticks={ticks}
                    periods={runView ? { regimes: runView.regimes, labels: runView.labels } : null}
                  />
                </div>

                {onOpen && (
                  <button
                    type="button"
                    data-row-open
                    onClick={(e) => {
                      e.stopPropagation();
                      onOpen(lane.creativeId);
                    }}
                    aria-label={`Details for ${lane.name}`}
                    aria-haspopup="dialog"
                    className="absolute top-4 right-3 inline-flex size-8 items-center justify-center rounded-sm text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 lg:static lg:justify-self-end"
                  >
                    <ChevronRightIcon className="size-5" />
                  </button>
                )}
              </div>

              <div data-explain className="cursor-auto">
                <ExplainPanel
                  open={explain}
                  reading={insights.lanes[lane.creativeId]}
                  className="lg:pl-[7.75rem]"
                />
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

/** Row click → open, unless it landed in the Explain text, on another control, or ended a text selection. */
function rowClick(e: MouseEvent<HTMLLIElement>, open: () => void) {
  const t = e.target as HTMLElement;
  if (t.closest("[data-explain], a, button, input, select, textarea")) return;
  if (window.getSelection()?.toString()) return;
  open();
}

/** "After shift 1, rounds 20,001–22,000: " / "Rounds 1–2,000: " for a strip window. */
function hoverText(
  win: { start: number; end: number } | undefined,
  periods: { regimes: { start: number; end: number }[]; labels: string[] } | null
): string {
  if (!win) return "";
  const rounds = `rounds ${formatInt(win.start + 1)}–${formatInt(win.end)}: `;
  const i = periods ? periodAt(periods.regimes, Math.floor((win.start + win.end - 1) / 2)) : -1;
  return i >= 0 && periods ? `${periods.labels[i]}, ${rounds}` : rounds.charAt(0).toUpperCase() + rounds.slice(1);
}

/** Click rate + segments won for the latest period, each with a "before" line (runs with shifts). */
function PeriodFigures({ r }: { r: LaneRegime }) {
  const rate = (v: number | null) => (v === null ? "–" : formatPercent(v, 1));
  const won = (xs: string[]) => (xs.length ? xs.map(segmentLabel).join(", ") : "none");
  return (
    <>
      <Figure
        label={`Click rate, ${r.latest.label.toLowerCase()}`}
        value={rate(r.latest.clickRate)}
        note={`${r.latest.trueCtr === null ? "" : `true ${formatPercent(r.latest.trueCtr, 1)}\n`}before ${rate(r.before.clickRate)}`}
      />
      <Figure
        label={`Segments won, ${r.latest.label.toLowerCase()}`}
        value={String(r.latest.segmentsWon.length)}
        note={`before ${r.before.segmentsWon.length}`}
        title={`${r.latest.label}: ${won(r.latest.segmentsWon)}. ${r.before.label}: ${won(r.before.segmentsWon)}.`}
      />
    </>
  );
}

function Figure({ label, value, note, title }: { label: string; value: string; note?: string; title?: string }) {
  return (
    <div className="min-w-0 lg:text-right" title={title}>
      <p className="text-xs text-muted-foreground lg:sr-only">{label}</p>
      <p
        className={cn(
          CONDENSED,
          "text-[1.75rem] leading-none tabular-nums",
          value === "–" ? "text-muted-foreground/50" : "text-foreground"
        )}
      >
        {value}
      </p>
      {note && <p className="mt-1 text-xs whitespace-pre-line text-muted-foreground tabular-nums">{note}</p>}
    </div>
  );
}

/**
 * One creative's share of traffic per window: a 2px line over a 10% wash, an end
 * dot, a hairline baseline and a faint even-split reference, all on the y scale
 * shared by every lane. Hover or drag reads out a window.
 */
function ShareStrip({
  lane,
  yMax,
  even,
  windows,
  ticks = [],
  periods = null,
}: {
  lane: Lane;
  yMax: number;
  even: number;
  windows: { start: number; end: number }[];
  /** Shift ticks, as fractions of the run. */
  ticks?: { frac: number; label: string }[];
  /** Periods between shifts, for the hover readout. */
  periods?: { regimes: { start: number; end: number }[]; labels: string[] } | null;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const { line, area, points } = stripPaths(lane.share, yMax, STRIP_W, STRIP_H);
  if (!points.length) {
    return (
      <div
        className="flex h-16 items-center border-b border-border text-xs text-muted-foreground"
        aria-label={`${lane.name}: no traffic yet`}
      >
        No traffic yet
      </div>
    );
  }
  const first = points[0];
  const last = points[points.length - 1];
  const evenY = shareY(even, yMax, STRIP_H);
  const active = hover !== null ? points[hover] : null;
  const win = hover !== null ? windows[hover] : undefined;

  const onMove = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (e.clientX - r.left) / (r.width || 1)));
    setHover(Math.round(f * (points.length - 1)));
  };

  return (
    <div
      className="relative h-16 touch-none"
      onPointerMove={onMove}
      onPointerLeave={() => setHover(null)}
      role="img"
      aria-label={`${lane.name}: ${formatPercent(first.v)} of traffic at the start, ${formatPercent(
        last.v
      )} at the end, against an even split of ${formatPercent(even)}.`}
    >
      <svg
        viewBox={`0 0 ${STRIP_W} ${STRIP_H}`}
        preserveAspectRatio="none"
        className="absolute inset-0 size-full overflow-visible"
        aria-hidden
      >
        <line x1={0} x2={STRIP_W} y1={STRIP_H} y2={STRIP_H} stroke="var(--rule)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
        {even > 0 && (
          <line x1={0} x2={STRIP_W} y1={evenY} y2={evenY} stroke="#9aa5ae" strokeWidth={1} vectorEffect="non-scaling-stroke" />
        )}
        {ticks.map((t) => (
          <line
            key={`${t.frac}-${t.label}`}
            x1={t.frac * STRIP_W}
            x2={t.frac * STRIP_W}
            y1={0}
            y2={STRIP_H}
            stroke="var(--foreground)"
            strokeOpacity={0.45}
            strokeWidth={1}
            strokeDasharray="2 3"
            vectorEffect="non-scaling-stroke"
          />
        ))}
        <path d={area} fill={lane.color} fillOpacity={0.08} />
        <path
          d={line}
          fill="none"
          stroke={lane.color}
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      {/* HTML end dot (stays round however the strip stretches), with a 2px surface ring. */}
      <span
        aria-hidden
        className="absolute size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-card"
        style={{ left: `${(last.x / STRIP_W) * 100}%`, top: `${(last.y / STRIP_H) * 100}%`, backgroundColor: lane.color }}
      />
      {active && (
        <>
          <span
            aria-hidden
            className="absolute inset-y-0 w-px bg-foreground/30"
            style={{ left: `${(active.x / STRIP_W) * 100}%` }}
          />
          <span
            aria-hidden
            className="absolute size-2 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-card"
            style={{ left: `${(active.x / STRIP_W) * 100}%`, top: `${(active.y / STRIP_H) * 100}%`, backgroundColor: lane.color }}
          />
          <span
            className={cn(
              "pointer-events-none absolute -top-7 z-10 rounded-sm border border-border bg-popover px-1.5 py-0.5 text-xs whitespace-nowrap text-foreground tabular-nums shadow-xs",
              active.x / STRIP_W > 0.6 ? "-translate-x-full" : ""
            )}
            style={{ left: `${(active.x / STRIP_W) * 100}%` }}
          >
            {hoverText(win, periods)}
            {formatPercent(active.v)}
          </span>
        </>
      )}
    </div>
  );
}
