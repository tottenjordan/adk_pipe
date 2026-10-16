"use client";

import { useMemo, useState } from "react";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import { XIcon } from "lucide-react";
import { ExplainPanel } from "@/components/explain";
import { Button } from "@/components/ui/button";
import { CONDENSED } from "@/app/results/[sessionId]/score-mark";
import { formatInt, formatPercent } from "@/lib/chart";
import {
  buildCreativeDetail,
  formatSignedPercent,
  periodSeries,
  type CreativeDetail,
} from "@/lib/creative-detail";
import { CREATIVE_DETAIL_EXPLAIN, SHIFT_EXPLAIN } from "@/lib/experiment-explain";
import { SegmentedControl } from "@/components/segmented-control";
import type { ChartMarker, RunView } from "@/lib/shifts";
import type { CreativeSeries } from "@/lib/experiments";
import type { Lane } from "@/lib/scoreboard";
import { ArmImage } from "./arm-image";
import { CtrOverTime, PeriodBars, SegmentBars } from "./creative-detail-charts";
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
  continuous = false,
}: {
  /** The open creative, or null when the drawer is closed. */
  lane: Lane | null;
  lanes: Lane[];
  series: CreativeSeries | null;
  explain: boolean;
  onClose: () => void;
  /** The shown run's shifts (contracts §10): markers, per-period reading and segment bars. */
  runView?: RunView | null;
  /** The shown run kept learning (contracts §11): the series is one stream, so its totals are whole-run. */
  continuous?: boolean;
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
          className="fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-border bg-card text-card-foreground shadow-xl outline-hidden sm:w-[min(40rem,calc(100vw-3rem))]"
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
                    continuous={continuous}
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
      <ArmImage
        arm={lane.arm}
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
  continuous = false,
}: {
  detail: CreativeDetail;
  lane: Lane;
  lanes: Lane[];
  explain: boolean;
  markers?: ChartMarker[];
  period?: PeriodView | null;
  continuous?: boolean;
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
              label={continuous ? "Missed clicks over the run" : "Missed clicks per episode"}
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
