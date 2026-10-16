"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { CONDENSED } from "@/app/results/[sessionId]/score-mark";
import { formatInt, formatPercent, linePath, niceDomain, niceTicks, scaleLinear } from "@/lib/chart";
import type { CreativeDetail, DetailSegment } from "@/lib/creative-detail";
import type { ChartMarker } from "@/lib/shifts";
import { segmentLabel } from "@/lib/experiments";
import type { Lane } from "@/lib/scoreboard";
import { cn } from "@/lib/utils";

/** The creative-detail drawer's charts: per-period bars, per-segment bars and click rate over time. */

/** Axis label: whole percents without decimals (2%), others with one (2.5%). */
const tickPercent = (t: number) => formatPercent(t, Math.abs(t * 100 - Math.round(t * 100)) < 1e-6 ? 0 : 1);

/**
 * This creative's observed click rate in each period between shifts, as bars
 * on one scale, with a dark tick at the period's true rate where known. The
 * selected period is solid; the others a lighter tint.
 */
export function PeriodBars({
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

/**
 * Click rate per segment as thick bars on one scale (shared by every creative):
 * solid where this creative is the best pick, a lighter tint where another
 * creative is, with a dark tick at the true rate.
 */
export function SegmentBars({
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
export function CtrOverTime({
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
