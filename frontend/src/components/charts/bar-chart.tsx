"use client";

import { useId, useState } from "react";
import { formatCompact, niceDomain, niceTicks, scaleLinear } from "@/lib/chart";
import { cn } from "@/lib/utils";

export interface ErrorBar {
  id: string;
  label: string;
  color: string;
  mean: number;
  /** Half-width of the error bar (e.g. one standard deviation). */
  err: number;
}

/**
 * Horizontal bar chart with error bars (mean ± err), one row per item. Row
 * labels sit left of the bars and every bar carries its value, so identity and
 * magnitude never depend on colour alone.
 */
export function BarChart({
  title,
  description,
  bars,
  xLabel,
  format = formatCompact,
  className,
}: {
  title: string;
  description?: string;
  bars: ErrorBar[];
  xLabel: string;
  format?: (v: number) => string;
  className?: string;
}) {
  const uid = useId();
  const [hover, setHover] = useState<string | null>(null);
  const W = 480;
  const ROW = 28;
  const M = { top: 8, right: 84, bottom: 40, left: 184 };
  const H = M.top + bars.length * ROW + M.bottom;
  const plotW = W - M.left - M.right;
  const max = Math.max(0, ...bars.map((b) => b.mean + Math.max(0, b.err)));
  const [, hi] = niceDomain(0, max || 1, 5);
  const x = scaleLinear([0, hi], [M.left, M.left + plotW]);
  const ticks = niceTicks(0, hi, 5);
  const base = M.top + bars.length * ROW;
  const titleId = `${uid}-title`;

  return (
    <figure className={cn("min-w-0", className)}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-labelledby={titleId}
        className="block h-auto tabular-nums"
      >
        <title id={titleId}>{title}</title>
        {description && <desc>{description}</desc>}
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={M.top} y2={base} className="stroke-border" strokeWidth={1} />
            <text
              x={x(t)}
              y={base + 16}
              textAnchor="middle"
              className="fill-muted-foreground text-[11px]"
            >
              {format(t)}
            </text>
          </g>
        ))}
        <text
          x={M.left + plotW / 2}
          y={H - 6}
          textAnchor="middle"
          className="fill-muted-foreground text-[11px] font-medium"
        >
          {xLabel}
        </text>
        {bars.map((b, i) => {
          const cy = M.top + i * ROW + ROW / 2;
          const lo = Math.max(0, b.mean - b.err);
          const hiV = b.mean + b.err;
          const active = hover === b.id;
          return (
            <g
              key={b.id}
              onPointerEnter={() => setHover(b.id)}
              onPointerLeave={() => setHover(null)}
            >
              <rect x={0} y={cy - ROW / 2} width={W} height={ROW} fill="transparent" />
              {active && (
                <rect x={0} y={cy - ROW / 2} width={W} height={ROW} className="fill-muted/60" />
              )}
              <text
                x={M.left - 10}
                y={cy}
                dy="0.32em"
                textAnchor="end"
                className="fill-foreground text-[11px]"
              >
                {b.label}
              </text>
              <rect
                x={M.left}
                y={cy - 7}
                width={Math.max(0, x(b.mean) - M.left)}
                height={14}
                rx={2}
                fill={b.color}
                fillOpacity={0.85}
              />
              {b.err > 0 && (
                <g className="stroke-foreground" strokeWidth={1.25}>
                  <line x1={x(lo)} x2={x(hiV)} y1={cy} y2={cy} />
                  <line x1={x(lo)} x2={x(lo)} y1={cy - 5} y2={cy + 5} />
                  <line x1={x(hiV)} x2={x(hiV)} y1={cy - 5} y2={cy + 5} />
                </g>
              )}
              <text
                x={x(hiV) + 8}
                y={cy}
                dy="0.32em"
                className="fill-foreground text-[11px]"
              >
                {format(b.mean)}
                <tspan className="fill-muted-foreground"> ± {format(b.err)}</tspan>
              </text>
            </g>
          );
        })}
        <line x1={M.left} x2={M.left} y1={M.top} y2={base} className="stroke-muted-foreground/60" />
      </svg>
    </figure>
  );
}
