"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import { XIcon } from "lucide-react";
import { ExplainPanel } from "@/components/explain";
import { Button } from "@/components/ui/button";
import { ProofImage } from "@/app/results/[sessionId]/proof-grid";
import { CONDENSED } from "@/app/results/[sessionId]/score-mark";
import { formatInt, formatPercent, linePath, niceDomain, niceTicks, scaleLinear } from "@/lib/chart";
import {
  buildCreativeDetail,
  formatSignedPercent,
  periodSeries,
  type CreativeDetail,
  type DetailSegment,
} from "@/lib/creative-detail";
import { CREATIVE_DETAIL_EXPLAIN, SHIFT_EXPLAIN } from "@/lib/experiment-explain";
import { SegmentedControl } from "@/components/segmented-control";
import type { ChartMarker, RunView } from "@/lib/shifts";
import { armImageUrl, segmentLabel, type CreativeSeries } from "@/lib/experiments";
import type { Lane } from "@/lib/scoreboard";
import { cn } from "@/lib/utils";

/**
 * Right-side sheet with one creative's observed results: a reading sentence, the
 * key numbers, click rate by audience segment (the drawer's main view) and click
 * rate over time. Esc, the overlay and the close button dismiss it; focus is
 * trapped while open and returns to the row that opened it.
 */
export function CreativeDetailDrawer({
  lane,
  lanes,
  series,
  explain,
  onClose,
  runView = null,
}: {
  /** The open creative, or null when the drawer is closed. */
  lane: Lane | null;
  lanes: Lane[];
  series: CreativeSeries | null;
  explain: boolean;
  onClose: () => void;
  /** The shown run's shifts (contracts §10): markers, per-period reading and segment bars. */
  runView?: RunView | null;
}) {
  const item = lane ? (series?.creatives.find((c) => c.creativeId === lane.creativeId) ?? null) : null;
  const detail = useMemo(() => buildCreativeDetail(item, series), [item, series]);
  const regimes = useMemo(() => runView?.seriesRegimes ?? [], [runView]);
  const [picked, setPicked] = useState<number | null>(null);
  const sel = regimes.length >= 2 ? Math.min(picked ?? regimes.length - 1, regimes.length - 1) : -1;
  const pSeries = useMemo(() => (sel >= 0 ? periodSeries(series, regimes[sel]) : null), [series, regimes, sel]);
  const pDetail = useMemo(() => {
    if (!pSeries || !lane) return null;
    return buildCreativeDetail(pSeries.creatives.find((c) => c.creativeId === lane.creativeId) ?? null, pSeries);
  }, [pSeries, lane]);
  const period =
    sel >= 0 && runView
      ? {
          labels: runView.labels,
          sel,
          onPick: setPicked,
          detail: pDetail,
          bars: regimes.map((g, i) => {
            const c = g.creatives.find((x) => x.creativeId === lane?.creativeId);
            return { label: runView.labels[i] ?? `Period ${i + 1}`, ctr: c?.ctr ?? null, trueCtr: c?.trueCtr ?? null };
          }),
        }
      : null;

  return (
    <DialogPrimitive.Root open={lane !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Backdrop className="fixed inset-0 isolate z-50 bg-foreground/40" />
        <DialogPrimitive.Popup
          className="fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-border bg-card text-card-foreground shadow-xl outline-none sm:w-[min(40rem,calc(100vw-3rem))]"
        >
          {lane && (
            <>
              <DrawerHeader lane={lane} />
              <div className="min-h-0 flex-1 overflow-y-auto px-6 pt-5 pb-10">
                {detail ? (
                  <DrawerBody
                    detail={detail}
                    lane={lane}
                    lanes={lanes}
                    explain={explain}
                    markers={runView?.markers ?? []}
                    period={period}
                  />
                ) : (
                  <p className="text-sm text-muted-foreground">
                    The per-creative numbers aren&apos;t available for this experiment yet. They appear once traffic
                    has run and the results service reports them.
                  </p>
                )}
              </div>
            </>
          )}
        </DialogPrimitive.Popup>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

function DrawerHeader({ lane }: { lane: Lane }) {
  return (
    <div className="flex items-start gap-4 border-b border-border px-6 py-5 pr-14">
      <ProofImage
        src={lane.arm ? armImageUrl(lane.arm) : null}
        alt={lane.name}
        className="size-20 shrink-0 rounded-sm text-[0px]"
      />
      <div className="min-w-0 self-center">
        <DialogPrimitive.Title
          className={cn(CONDENSED, "flex items-start gap-2.5 text-[1.75rem] leading-[1.05] text-foreground")}
        >
          <span aria-hidden className="mt-[0.35em] size-3 shrink-0 rounded-full" style={{ backgroundColor: lane.color }} />
          <span className="min-w-0">{lane.name}</span>
        </DialogPrimitive.Title>
        {lane.headline && (
          <DialogPrimitive.Description className="mt-1 pl-[1.375rem] text-sm text-muted-foreground">
            {lane.headline}
          </DialogPrimitive.Description>
        )}
      </div>
      <DialogPrimitive.Close
        render={<Button variant="ghost" size="icon-sm" className="absolute top-4 right-4" />}
      >
        <XIcon />
        <span className="sr-only">Close</span>
      </DialogPrimitive.Close>
    </div>
  );
}

interface PeriodView {
  labels: string[];
  sel: number;
  onPick: (i: number) => void;
  /** The creative's detail within the selected period (null without per-period cells). */
  detail: CreativeDetail | null;
  bars: { label: string; ctr: number | null; trueCtr: number | null }[];
}

function DrawerBody({
  detail,
  lane,
  lanes,
  explain,
  markers = [],
  period = null,
}: {
  detail: CreativeDetail;
  lane: Lane;
  lanes: Lane[];
  explain: boolean;
  markers?: ChartMarker[];
  period?: PeriodView | null;
}) {
  const others = new Map(lanes.map((l) => [l.creativeId, l]));
  const segDetail = period?.detail ?? detail;
  const showSegments = segDetail.segments.length > 0 && segDetail.segmentMax > 0;
  return (
    <>
      {period && (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <SegmentedControl
            label="Period of the run"
            options={period.labels.map((l, i) => ({ value: String(i), label: l }))}
            value={String(period.sel)}
            onChange={(v) => period.onPick(Number(v))}
          />
        </div>
      )}
      <p className="max-w-[56ch] text-lg leading-snug text-foreground text-pretty">
        {period?.detail ? `${period.labels[period.sel]}: ${lowerFirst(period.detail.reading)}` : detail.reading}
      </p>

      <section aria-labelledby="detail-numbers" className="mt-6">
        <h3 id="detail-numbers" className="sr-only">
          Key numbers
        </h3>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-3">
          <Figure label="Impressions" value={detail.impressions === null ? "–" : formatInt(detail.impressions)} />
          <Figure label="Clicks" value={detail.clicks === null ? "–" : formatInt(detail.clicks)} />
          <Figure
            label="Click rate"
            value={detail.clickRate === null ? "–" : formatPercent(detail.clickRate, 1)}
            note={detail.trueCtr === null ? undefined : `true ${formatPercent(detail.trueCtr, 1)}`}
          />
          {detail.lift !== null && (
            <Figure label="Lift vs the set" value={formatSignedPercent(detail.lift)} note="against all creatives pooled" />
          )}
          {detail.trafficShare !== null && detail.clickShare !== null && (
            <Figure
              label="Traffic share, click share"
              value={`${formatPercent(detail.trafficShare)} → ${formatPercent(detail.clickShare)}`}
              note="of all impressions, of all clicks"
            />
          )}
          {detail.missedClicks !== null && (
            <Figure
              label="Missed clicks per episode"
              value={formatMissed(detail.missedClicks)}
              note={
                detail.clicksPerEpisode === null
                  ? "vs the best creative per reader"
                  : `against ${formatInt(detail.clicksPerEpisode)} earned`
              }
            />
          )}
          {detail.engagedSecondsPer1k !== null && (
            <Figure
              label="Engaged seconds per 1k"
              value={formatInt(detail.engagedSecondsPer1k)}
              note="per 1,000 impressions"
            />
          )}
        </dl>
        <ExplainPanel open={explain}>{CREATIVE_DETAIL_EXPLAIN.numbers}</ExplainPanel>
      </section>

      {period && period.bars.some((b) => b.ctr !== null) && (
        <section aria-labelledby="detail-periods" className="mt-9">
          <PeriodBars bars={period.bars} color={lane.color} sel={period.sel} />
          <ExplainPanel open={explain}>{SHIFT_EXPLAIN.periods}</ExplainPanel>
        </section>
      )}

      {showSegments && (
        <section aria-labelledby="detail-segments" className="mt-9">
          <SegmentBars
            detail={segDetail}
            lane={lane}
            others={others}
            title={period?.detail ? `Click rate by audience segment, ${period.labels[period.sel].toLowerCase()}` : undefined}
          />
          <ExplainPanel open={explain}>{CREATIVE_DETAIL_EXPLAIN.segments}</ExplainPanel>
        </section>
      )}

      {detail.ctrPoints.length >= 2 && (
        <section aria-labelledby="detail-over-time" className="mt-9">
          <h3 id="detail-over-time" className="text-base font-semibold text-foreground">
            Click rate over time
          </h3>
          <CtrOverTime detail={detail} color={lane.color} name={lane.name} markers={markers} />
          <ExplainPanel open={explain}>
            {markers.length ? `${CREATIVE_DETAIL_EXPLAIN.overTime} ${SHIFT_EXPLAIN.markers}` : CREATIVE_DETAIL_EXPLAIN.overTime}
          </ExplainPanel>
        </section>
      )}
    </>
  );
}

const formatMissed = (v: number) => (v >= 10 ? formatInt(v) : v.toFixed(1));
const lowerFirst = (t: string) => t.charAt(0).toLowerCase() + t.slice(1);

/**
 * This creative's observed click rate in each period between shifts, as bars
 * on one scale, with a dark tick at the period's true rate where known. The
 * selected period is solid; the others a lighter tint.
 */
function PeriodBars({
  bars,
  color,
  sel,
}: {
  bars: { label: string; ctr: number | null; trueCtr: number | null }[];
  color: string;
  sel: number;
}) {
  const max = Math.max(...bars.flatMap((b) => [b.ctr ?? 0, b.trueCtr ?? 0])) * 1.1 || 1;
  const at = (v: number) => `${(Math.max(0, Math.min(v, max)) / max) * 100}%`;
  return (
    <>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 id="detail-periods" className="text-base font-semibold text-foreground">
          Click rate by period
        </h3>
        <p className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <span aria-hidden className="h-3 w-0.5 bg-foreground" />
          True rate
        </p>
      </div>
      <ul className="mt-2">
        {bars.map((b, i) => (
          <li
            key={b.label}
            className="grid grid-cols-[9.5rem_minmax(0,1fr)_4rem] items-center border-t border-border py-2.5 first:border-t-0"
          >
            <span className={cn("truncate pr-3 text-sm", i === sel ? "font-semibold text-foreground" : "text-muted-foreground")}>
              {b.label}
            </span>
            <span aria-hidden className="relative h-5">
              {b.ctr !== null && (
                <span
                  className="absolute inset-y-0 left-0 rounded-r-[2px]"
                  style={{
                    width: at(b.ctr),
                    backgroundColor: i === sel ? color : `color-mix(in srgb, ${color} 34%, var(--card))`,
                  }}
                />
              )}
              {b.trueCtr !== null && (
                <span
                  className="absolute -inset-y-1 w-0.5 -translate-x-1/2 bg-foreground ring-2 ring-card"
                  style={{ left: at(b.trueCtr) }}
                />
              )}
            </span>
            <span className={cn(CONDENSED, "text-right text-[1.375rem] leading-none text-foreground tabular-nums")}>
              {b.ctr === null ? "–" : formatPercent(b.ctr, 1)}
              <span className="sr-only">{b.trueCtr === null ? "" : `, true rate ${formatPercent(b.trueCtr, 1)}`}</span>
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

/** Axis label: whole percents without decimals (2%), others with one (2.5%). */
const tickPercent = (t: number) => formatPercent(t, Math.abs(t * 100 - Math.round(t * 100)) < 1e-6 ? 0 : 1);

function Figure({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn(CONDENSED, "mt-1 text-[1.75rem] leading-none whitespace-nowrap text-foreground tabular-nums")}>
        {value}
      </dd>
      {note && <dd className="mt-1 text-xs leading-snug text-muted-foreground tabular-nums">{note}</dd>}
    </div>
  );
}

/**
 * Click rate per segment as thick bars on one scale (shared by every creative):
 * solid where this creative is the best pick, a lighter tint where another
 * creative is, with a dark tick at the true rate.
 */
function SegmentBars({
  detail,
  lane,
  others,
  title,
}: {
  detail: CreativeDetail;
  lane: Lane;
  others: Map<string, Lane>;
  title?: string;
}) {
  const max = detail.segmentMax;
  const ticks = niceTicks(0, max, 4).filter((t) => t <= max + 1e-9);
  const at = (v: number) => `${(Math.max(0, Math.min(v, max)) / max) * 100}%`;
  return (
    <>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 id="detail-segments" className="text-base font-semibold text-foreground">
          {title ?? "Click rate by audience segment"}
        </h3>
        <p className={cn("inline-flex items-center gap-1.5 text-xs text-muted-foreground", !detail.segments.some((x) => x.trueCtr !== null) && "invisible")}>
          <span aria-hidden className="h-3 w-0.5 bg-foreground" />
          True rate
        </p>
      </div>

      {/* Scale: shared by every creative and every segment. */}
      <div aria-hidden className="relative mt-4 mr-16 hidden h-4 sm:ml-[9.5rem] sm:block text-[11px] text-muted-foreground tabular-nums">
        {ticks.map((t, i) => (
          <span
            key={t}
            className={cn("absolute top-0", i === 0 ? "" : i === ticks.length - 1 && t >= max - 1e-9 ? "-translate-x-full" : "-translate-x-1/2")}
            style={{ left: at(t) }}
          >
            {tickPercent(t)}
          </span>
        ))}
      </div>

      <ul className="mt-1">
        {detail.segments.map((s) => (
          <SegmentRow key={s.segment} s={s} lane={lane} others={others} at={at} ticks={ticks} />
        ))}
      </ul>
    </>
  );
}

function SegmentRow({
  s,
  lane,
  others,
  at,
  ticks,
}: {
  s: DetailSegment;
  lane: Lane;
  others: Map<string, Lane>;
  at: (v: number) => string;
  ticks: number[];
}) {
  const best = s.bestCreativeId && !s.isBest ? others.get(s.bestCreativeId) : null;
  const label = segmentLabel(s.segment);
  const summary = `${label}: ${s.ctr === null ? "no impressions" : `${formatPercent(s.ctr, 1)} click rate`} from ${formatInt(
    s.impressions
  )} impressions${s.trueCtr === null ? "" : `, true rate ${formatPercent(s.trueCtr, 1)}`}${
    s.isBest ? ". Best for this segment." : best ? `. ${best.name} is best for this segment.` : "."
  }`;
  return (
    <li className="grid grid-cols-[minmax(0,1fr)_4rem] items-center border-t border-border py-3 first:border-t-0 sm:grid-cols-[9.5rem_minmax(0,1fr)_4rem]">
      <span className="sr-only">{summary}</span>
      {/* Narrow screens: the label sits on its own line above the bar. */}
      <div aria-hidden className="col-span-2 mb-2 flex min-w-0 items-baseline gap-2 sm:col-span-1 sm:mb-0 sm:block sm:pr-3">
        <p className="truncate text-sm font-semibold text-foreground" title={label}>
          {label}
        </p>
        <p className="text-xs text-muted-foreground tabular-nums sm:mt-0.5">{formatInt(s.impressions)} impr.</p>
      </div>

      <div aria-hidden className="min-w-0">
        <div className="relative h-7">
          {ticks.slice(1).map((t) => (
            <span key={t} className="absolute inset-y-0 w-px bg-border" style={{ left: at(t) }} />
          ))}
          {s.ctr !== null && (
            <span
              className="absolute inset-y-0 left-0 rounded-r-[2px]"
              style={{
                width: at(s.ctr),
                // Opaque tint (not opacity) so the gridlines stay behind the bar.
                backgroundColor: s.isBest ? lane.color : `color-mix(in srgb, ${lane.color} 34%, var(--card))`,
              }}
            />
          )}
          {s.trueCtr !== null && (
            <span
              className="absolute -inset-y-1 w-0.5 -translate-x-1/2 bg-foreground ring-2 ring-card"
              style={{ left: at(s.trueCtr) }}
            />
          )}
        </div>
        <p className="mt-1.5 flex min-h-4 items-center gap-1.5 text-xs">
          {s.isBest ? (
            <span className="font-semibold text-foreground">Best for this segment</span>
          ) : best ? (
            <span className="inline-flex min-w-0 items-center gap-1.5 text-muted-foreground">
              <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: best.color }} />
              <span className="truncate">Best here: {best.name}</span>
            </span>
          ) : null}
        </p>
      </div>

      <p
        aria-hidden
        className={cn(
          CONDENSED,
          "self-start pt-0.5 text-right text-[1.625rem] leading-none tabular-nums",
          s.ctr === null ? "text-muted-foreground/50" : "text-foreground"
        )}
      >
        {s.ctr === null ? "–" : formatPercent(s.ctr, 1)}
      </p>
    </li>
  );
}

const CH = 150;
const CM = { top: 10, right: 72, bottom: 24, left: 40 };

/**
 * Observed click rate per window (solid, the creative's colour) against the true
 * rate (dashed). The viewBox follows the rendered width, so text stays 11px from
 * a phone to the full drawer.
 */
function CtrOverTime({
  detail,
  color,
  name,
  markers = [],
}: {
  detail: CreativeDetail;
  color: string;
  name: string;
  markers?: ChartMarker[];
}) {
  const [CW, setWidth] = useState(560);
  const ref = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const w = Math.round(entry.contentRect.width);
      if (w > 0) setWidth(Math.max(280, w));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const pts = detail.ctrPoints;
  const ys = pts.map((p) => p.y);
  if (detail.trueCtr !== null) ys.push(detail.trueCtr);
  const yd = niceDomain(0, Math.max(...ys), 3);
  const x0 = pts[0].x;
  const x1 = pts[pts.length - 1].x;
  const x = scaleLinear([x0, x1 === x0 ? x0 + 1 : x1], [CM.left, CW - CM.right]);
  const y = scaleLinear(yd, [CH - CM.bottom, CM.top]);
  const yTicks = niceTicks(yd[0], yd[1], 3);
  const last = pts[pts.length - 1];
  const lo = Math.min(...pts.map((p) => p.y));
  const hi = Math.max(...pts.map((p) => p.y));
  return (
    <figure ref={ref} className="mt-3">
      <svg
        viewBox={`0 0 ${CW} ${CH}`}
        className="w-full overflow-visible"
        role="img"
        aria-label={`${name}: click rate per slice of the run, between ${formatPercent(lo, 1)} and ${formatPercent(
          hi,
          1
        )}, ending at ${formatPercent(last.y, 1)}${
          detail.trueCtr === null ? "" : `, against a true rate of ${formatPercent(detail.trueCtr, 1)}`
        }.`}
      >
        {yTicks.map((t) => (
          <g key={t}>
            <line x1={CM.left} x2={CW - CM.right} y1={y(t)} y2={y(t)} stroke="var(--border)" strokeWidth={1} />
            <text x={CM.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="fill-muted-foreground text-[11px] tabular-nums">
              {tickPercent(t)}
            </text>
          </g>
        ))}
        {[x0, x1].map((v, i) => (
          <text
            key={i}
            x={x(v)}
            y={CH - 6}
            textAnchor={i === 0 ? "start" : "end"}
            className="fill-muted-foreground text-[11px] tabular-nums"
          >
            {i === 0 ? "Start of run" : "End of run"}
          </text>
        ))}
        {markers
          .filter((m) => m.x >= x0 && m.x <= x1)
          .map((m, i) => (
            <g key={`${m.x}-${m.label}`}>
              <line
                x1={x(m.x)}
                x2={x(m.x)}
                y1={CM.top}
                y2={CH - CM.bottom}
                stroke="var(--foreground)"
                strokeOpacity={0.55}
                strokeDasharray="3 3"
              />
              <text
                x={x(m.x) + 4}
                y={CM.top + 9 + (i % 2) * 12}
                className="fill-foreground text-[10.5px] font-medium"
                stroke="var(--card)"
                strokeWidth={3}
                paintOrder="stroke"
              >
                {m.label}
              </text>
            </g>
          ))}
        {detail.trueCtr !== null && (
          <>
            <line
              x1={CM.left}
              x2={CW - CM.right}
              y1={y(detail.trueCtr)}
              y2={y(detail.trueCtr)}
              stroke="var(--foreground)"
              strokeOpacity={0.6}
              strokeWidth={1.25}
              strokeDasharray="4 3"
            />
            <text
              x={CW - CM.right + 8}
              y={y(detail.trueCtr)}
              dy={y(detail.trueCtr) > y(last.y) ? "0.9em" : "-0.1em"}
              className="fill-muted-foreground text-[11px] tabular-nums"
            >
              true {formatPercent(detail.trueCtr, 1)}
            </text>
          </>
        )}
        <path
          d={linePath(pts.map((p) => ({ x: x(p.x), y: y(p.y) })))}
          fill="none"
          stroke={color}
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <circle cx={x(last.x)} cy={y(last.y)} r={3.5} fill={color} stroke="var(--card)" strokeWidth={2} />
        <text
          x={CW - CM.right + 8}
          y={y(last.y)}
          dy={detail.trueCtr !== null && y(detail.trueCtr) > y(last.y) ? "-0.1em" : "0.9em"}
          className="fill-foreground text-[11px] font-semibold tabular-nums"
        >
          {formatPercent(last.y, 1)}
        </text>
      </svg>
    </figure>
  );
}
