"use client";

import { useId, useState, type PointerEvent } from "react";
import {
  bandPath,
  extent,
  formatCompact,
  linePath,
  logTicks,
  nearestIndex,
  niceDomain,
  niceTicks,
  scaleLinear,
  scaleLog,
  spreadLabels,
} from "@/lib/chart";
import { cn } from "@/lib/utils";

export interface LineSeries {
  id: string;
  /** Legend / readout name. */
  label: string;
  /** Direct label at the line end (defaults to `label`). */
  shortLabel?: string;
  color: string;
  dash?: string;
  /** A quiet reference line (oracle / optimum): thinner, no band. */
  reference?: boolean;
  points: { x: number; y: number }[];
  band?: { x: number; lo: number; hi: number }[];
}

export interface LineChartProps {
  /** Accessible name (the visible heading lives outside the chart). */
  title: string;
  /** One-sentence summary for screen readers. */
  description?: string;
  series: LineSeries[];
  xLabel: string;
  yLabel: string;
  logX?: boolean;
  /** Fixed y domain; otherwise a nice domain around the data (and bands). */
  yDomain?: [number, number];
  /** Include 0 in the auto y domain (default true). */
  yZero?: boolean;
  formatX?: (v: number) => string;
  formatY?: (v: number) => string;
  /** Horizontal dashed reference lines, e.g. an optimum. */
  referenceLines?: { y: number; label: string }[];
  /** Label each line at its right end (keep to ≤ 6 series). */
  directLabels?: boolean;
  className?: string;
}

// Sized so a half-width panel renders near 1:1 (text stays ~11px).
const W = 480;
const H = 280;
const M = { top: 14, right: 12, bottom: 42, left: 50 };
const LABEL_GUTTER = 92;

/**
 * Multi-series SVG line chart: optional log x, CI bands (low-opacity fills),
 * dashed reference lines, direct end labels, a toggleable legend and a
 * nearest-checkpoint hover readout. Responsive via viewBox; no animation.
 */
export function LineChart({
  title,
  description,
  series,
  xLabel,
  yLabel,
  logX = false,
  yDomain,
  yZero = true,
  formatX = formatCompact,
  formatY = formatCompact,
  referenceLines = [],
  directLabels = true,
  className,
}: LineChartProps) {
  const uid = useId();
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [hoverX, setHoverX] = useState<number | null>(null);

  const visible = series.filter((s) => !hidden.has(s.id));
  const right = M.right + (directLabels ? LABEL_GUTTER : 0);
  const plotW = W - M.left - right;
  const plotH = H - M.top - M.bottom;

  const geom = (() => {
    const allX = series.flatMap((s) => s.points.map((p) => p.x)).filter((x) => !logX || x > 0);
    const xe = extent(allX) ?? [logX ? 1 : 0, logX ? 10 : 1];
    const ys = visible.flatMap((s) => [
      ...s.points.map((p) => p.y),
      ...(s.band ?? []).flatMap((b) => [b.lo, b.hi]),
    ]);
    ys.push(...referenceLines.map((r) => r.y));
    const ye = extent(ys) ?? [0, 1];
    const yd: [number, number] =
      yDomain ?? niceDomain(yZero ? Math.min(0, ye[0]) : ye[0], yZero ? Math.max(0, ye[1]) : ye[1], 5);
    const x = (logX ? scaleLog : scaleLinear)(xe as [number, number], [M.left, M.left + plotW]);
    const y = scaleLinear(yd, [M.top + plotH, M.top]);
    const xTicks = logX
      ? logTicks(xe[0], xe[1])
      : niceTicks(xe[0], xe[1], 6).map((value) => ({ value, major: true }));
    const yTicks = niceTicks(yd[0], yd[1], 5);
    const xs = [...new Set(allX)].sort((a, b) => a - b);
    return { x, y, xTicks, yTicks, xs, yd };
  })();

  const { x, y, xTicks, yTicks, xs } = geom;

  const ends = visible
    .map((s) => ({ s, last: [...s.points].reverse().find((p) => Number.isFinite(p.y)) }))
    .filter((e): e is { s: LineSeries; last: { x: number; y: number } } => Boolean(e.last));
  const labelYs = spreadLabels(
    ends.map((e) => y(e.last.y)),
    13,
    M.top + 4,
    M.top + plotH
  );

  const hoverIdx = hoverX === null ? -1 : nearestIndex(xs, hoverX);
  const hoverValue = hoverIdx >= 0 ? xs[hoverIdx] : null;

  const onMove = (e: PointerEvent<SVGRectElement>) => {
    const svg = e.currentTarget.ownerSVGElement;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    // Invert the x scale by searching the known checkpoints for the closest screen x.
    let best: number | null = null;
    let bestD = Infinity;
    for (const v of xs) {
      const d = Math.abs(x(v) - px);
      if (d < bestD) {
        bestD = d;
        best = v;
      }
    }
    setHoverX(best);
  };

  const toggle = (id: string) =>
    setHidden((h) => {
      const next = new Set(h);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const titleId = `${uid}-title`;
  const descId = `${uid}-desc`;

  return (
    <figure className={cn("min-w-0", className)}>
      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          role="img"
          aria-labelledby={titleId}
          aria-describedby={description ? descId : undefined}
          className="block h-auto overflow-visible tabular-nums"
        >
          <title id={titleId}>{title}</title>
          {description && <desc id={descId}>{description}</desc>}

          {/* Recessive grid + y ticks */}
          {yTicks.map((t) => (
            <g key={`y${t}`}>
              <line
                x1={M.left}
                x2={M.left + plotW}
                y1={y(t)}
                y2={y(t)}
                className="stroke-border"
                strokeWidth={1}
              />
              <text
                x={M.left - 8}
                y={y(t)}
                dy="0.32em"
                textAnchor="end"
                className="fill-muted-foreground text-[11px]"
              >
                {formatY(t)}
              </text>
            </g>
          ))}

          {/* x ticks */}
          {xTicks.map((t) => (
            <g key={`x${t.value}`}>
              <line
                x1={x(t.value)}
                x2={x(t.value)}
                y1={M.top + plotH}
                y2={M.top + plotH + (t.major ? 5 : 3)}
                className="stroke-muted-foreground/60"
                strokeWidth={1}
              />
              {t.major && (
                <text
                  x={x(t.value)}
                  y={M.top + plotH + 18}
                  textAnchor="middle"
                  className="fill-muted-foreground text-[11px]"
                >
                  {formatX(t.value)}
                </text>
              )}
            </g>
          ))}
          <line
            x1={M.left}
            x2={M.left + plotW}
            y1={M.top + plotH}
            y2={M.top + plotH}
            className="stroke-muted-foreground/60"
            strokeWidth={1}
          />

          {/* Axis titles (sentence case) */}
          <text
            x={M.left + plotW / 2}
            y={H - 6}
            textAnchor="middle"
            className="fill-muted-foreground text-[11px] font-medium"
          >
            {xLabel}
          </text>
          <text
            transform={`translate(14 ${M.top + plotH / 2}) rotate(-90)`}
            textAnchor="middle"
            className="fill-muted-foreground text-[11px] font-medium"
          >
            {yLabel}
          </text>

          {/* CI bands under the lines */}
          {visible.map(
            (s) =>
              s.band && (
                <path
                  key={`band-${s.id}`}
                  d={bandPath(s.band.map((b) => ({ x: x(b.x), lo: y(b.lo), hi: y(b.hi) })))}
                  fill={s.color}
                  fillOpacity={0.12}
                  stroke="none"
                />
              )
          )}

          {referenceLines.map((r) => (
            <g key={`ref-${r.label}`}>
              <line
                x1={M.left}
                x2={M.left + plotW}
                y1={y(r.y)}
                y2={y(r.y)}
                className="stroke-foreground"
                strokeWidth={1.25}
                strokeDasharray="6 4"
              />
              <text
                x={M.left + plotW - 4}
                y={y(r.y) - 5}
                textAnchor="end"
                className="fill-foreground text-[11px]"
              >
                {r.label}
              </text>
            </g>
          ))}

          {/* Lines: references first so policies draw on top */}
          {[...visible.filter((s) => s.reference), ...visible.filter((s) => !s.reference)].map((s) => (
            <path
              key={`line-${s.id}`}
              d={linePath(s.points.map((p) => ({ x: x(p.x), y: y(p.y) })))}
              fill="none"
              stroke={s.color}
              strokeWidth={s.reference ? 1.5 : 2}
              strokeDasharray={s.dash}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          ))}

          {/* Direct labels at line ends */}
          {directLabels &&
            ends.map((e, i) => (
              <g key={`lab-${e.s.id}`}>
                <circle cx={x(e.last.x)} cy={y(e.last.y)} r={2.5} fill={e.s.color} />
                <text
                  x={M.left + plotW + 8}
                  y={labelYs[i]}
                  dy="0.32em"
                  className="fill-foreground text-[11px]"
                >
                  {e.s.shortLabel ?? e.s.label}
                </text>
              </g>
            ))}

          {/* Hover crosshair */}
          {hoverValue !== null && (
            <g pointerEvents="none">
              <line
                x1={x(hoverValue)}
                x2={x(hoverValue)}
                y1={M.top}
                y2={M.top + plotH}
                className="stroke-foreground/40"
                strokeWidth={1}
              />
              {visible.map((s) => {
                const p = s.points.find((q) => q.x === hoverValue);
                return p && Number.isFinite(p.y) ? (
                  <circle
                    key={`h-${s.id}`}
                    cx={x(p.x)}
                    cy={y(p.y)}
                    r={4}
                    fill={s.color}
                    className="stroke-card"
                    strokeWidth={2}
                  />
                ) : null;
              })}
            </g>
          )}
          <rect
            x={M.left}
            y={M.top}
            width={plotW}
            height={plotH}
            fill="transparent"
            onPointerMove={onMove}
            onPointerLeave={() => setHoverX(null)}
          />
        </svg>

        {hoverValue !== null && (
          <div
            className="pointer-events-none absolute top-2 rounded-sm border border-border bg-card px-2.5 py-2 text-xs shadow-sm tabular-nums"
            style={
              x(hoverValue) > M.left + plotW / 2
                ? { left: `${(M.left / W) * 100 + 1}%` }
                : { right: `${(right / W) * 100 + 1}%` }
            }
          >
            <p className="mb-1 font-medium text-foreground">
              {xLabel}: {formatX(hoverValue)}
            </p>
            <ul className="space-y-0.5">
              {visible.map((s) => {
                const p = s.points.find((q) => q.x === hoverValue);
                return (
                  <li key={s.id} className="flex items-center gap-2 text-muted-foreground">
                    <Swatch color={s.color} dash={s.dash} />
                    <span className="min-w-0 flex-1 truncate">{s.label}</span>
                    <span className="text-foreground">{p ? formatY(p.y) : "–"}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>

      {series.length > 1 && (
        <figcaption className="mt-2">
          <ul aria-label={`${title}: series`} className="flex flex-wrap gap-x-1 gap-y-1">
            {series.map((s) => {
              const off = hidden.has(s.id);
              return (
                <li key={s.id}>
                  <button
                    type="button"
                    aria-pressed={!off}
                    onClick={() => toggle(s.id)}
                    title={off ? `Show ${s.label}` : `Hide ${s.label}`}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-sm px-1.5 py-0.5 text-xs transition-colors hover:bg-muted",
                      off ? "text-muted-foreground/60 line-through" : "text-foreground"
                    )}
                  >
                    <Swatch color={s.color} dash={s.dash} faded={off} />
                    {s.label}
                  </button>
                </li>
              );
            })}
          </ul>
        </figcaption>
      )}
    </figure>
  );
}

/** A short line sample in the series colour (dash pattern included). */
export function Swatch({ color, dash, faded }: { color: string; dash?: string; faded?: boolean }) {
  return (
    <svg width="18" height="8" aria-hidden className={cn("shrink-0", faded && "opacity-40")}>
      <line
        x1="1"
        x2="17"
        y1="4"
        y2="4"
        stroke={color}
        strokeWidth={2.5}
        strokeDasharray={dash}
        strokeLinecap="round"
      />
    </svg>
  );
}
