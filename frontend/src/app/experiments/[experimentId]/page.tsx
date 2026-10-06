"use client";

import { use, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ExplainSwitch } from "@/components/explain";
import { Button, buttonVariants } from "@/components/ui/button";
import { FieldLabel } from "@/components/field-label";
import { InfoTip } from "@/components/ui/info-tip";
import { ExperimentStatusLabel, StatusHelp } from "@/components/experiment-status";
import { CustomBadge, OverridesDisclosure } from "@/components/scenario-overrides";
import { ProofImage } from "@/app/results/[sessionId]/proof-grid";
import { formatInt } from "@/lib/chart";
import {
  armColor,
  armImageUrl,
  armName,
  canStop,
  continuousLimitError,
  ctrModeLabel,
  defaultHorizon,
  EPISODE_OPTIONS,
  getCreativeSeries,
  getExperimentMetrics,
  hasMetrics,
  InvalidShiftsError,
  isContinuousMetrics,
  isContinuousRun,
  MAX_CONTINUOUS_ROUNDS,
  parseContinuousSummary,
  pollExperiment,
  PollWaker,
  rewardModeLabel,
  scenarioLabel,
  shortId,
  startTraffic,
  statusLooksStale,
  stopExperiment,
  ttlText,
  wakeOnPageReturn,
  type CreativeSeries,
  type ExperimentMetrics,
  type ExperimentSummary,
  type LearningMode,
} from "@/lib/experiments";
import { SegmentedControl } from "@/components/segmented-control";
import { buildInsights } from "@/lib/experiment-insights";
import { buildLanes, laneRegimes } from "@/lib/scoreboard";
import {
  buildRunView,
  defaultForget,
  parseRunParam,
  runRounds,
  segmentMarkers,
  selectedRun,
  trafficRuns,
  urlForRun,
  validateShifts,
  type EditorShift,
  type ShiftContext,
} from "@/lib/shifts";
import { valuesFromOverrides } from "@/lib/scenario-preview";
import type { Scenario, CtrMode } from "@/lib/experiments";
import {
  parseCreativeParam,
  parseView,
  urlForCreative,
  urlForView,
  useExplainPref,
  type ExperimentView,
} from "@/lib/experiment-view";
import { segmentGrid } from "@/lib/creative-detail";
import { CARD_HELP, CONTROL_HELP, STOP_CONFIRM } from "@/lib/experiment-help";
import { ExperimentCharts } from "./experiment-charts";
import { CreativeScoreboard } from "./creative-scoreboard";
import { CreativeDetailDrawer } from "./creative-detail";
import { SegmentGridView } from "./segment-grid";
import { ShiftTimeline } from "./shift-timeline";
import { RunSelector } from "./run-selector";

const METRICS_INTERVAL_MS = 10_000;

const LEARNING_OPTIONS: readonly { value: LearningMode; label: string }[] = [
  { value: "per_episode", label: "Reset each episode" },
  { value: "continuous", label: "Keep learning" },
];

/** "20 and 50" / "5, 20 and 50". */
function joinCounts(ns: readonly number[]): string {
  const s = ns.map(String);
  return s.length <= 1 ? (s[0] ?? "") : `${s.slice(0, -1).join(", ")} and ${s[s.length - 1]}`;
}

export default function ExperimentPage({
  params,
}: {
  params: Promise<{ experimentId: string }>;
}) {
  const { experimentId } = use(params);
  const [exp, setExp] = useState<ExperimentSummary | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<ExperimentMetrics | null>(null);
  const [series, setSeries] = useState<CreativeSeries | null>(null);
  const searchParams = useSearchParams();
  const [view, setViewState] = useState<ExperimentView>(() => parseView(searchParams.get("view")));
  const [explain, setExplain] = useExplainPref();
  const setView = useCallback((v: ExperimentView) => {
    setViewState(v);
    window.history.replaceState(null, "", urlForView(window.location.href, v));
  }, []);
  // The open creative detail drawer (deep-linkable as `?creative=<id>`).
  const [openCreative, setOpenCreativeState] = useState<string | null>(() =>
    parseCreativeParam(searchParams.get("creative"))
  );
  const setOpenCreative = useCallback((id: string | null) => {
    setOpenCreativeState(id);
    window.history.replaceState(null, "", urlForCreative(window.location.href, id));
  }, []);
  // The traffic run the results show (`?run=N`, contracts §10); null = the latest.
  const [runParam, setRunParamState] = useState<number | null>(() => parseRunParam(searchParams.get("run")));
  const setRunParam = useCallback((run: number | null) => {
    setRunParamState(run);
    window.history.replaceState(null, "", urlForRun(window.location.href, run));
  }, []);
  // The shift editor's script for the next Start traffic, and the forgetting toggle (null = default).
  const [draftShifts, setDraftShifts] = useState<EditorShift[]>([]);
  const [forgetChoice, setForgetChoice] = useState<boolean | null>(null);
  const [shiftError, setShiftError] = useState<string | null>(null);
  const [pollKey, setPollKey] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [episodes, setEpisodes] = useState<number>(20);
  const [learning, setLearning] = useState<LearningMode>("per_episode");
  const [busy, setBusy] = useState<"traffic" | "stop" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);

  // "Poll now": tab visible again, window focus, or metrics showing the run is done.
  const [waker] = useState(() => new PollWaker());
  useEffect(() => wakeOnPageReturn(waker), [waker]);

  // Follow the experiment's status until it settles; restarted (pollKey) after an
  // action. Transient failures retry with backoff (surfaced after a few in a row);
  // only a hard error such as a 404 ends the poll.
  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      try {
        for await (const s of pollExperiment(experimentId, {
          signal: ctrl.signal,
          waker,
          onError: (err) => setLoadError(err.message),
        })) {
          setExp(s);
          setLoadError(null);
        }
      } catch (err) {
        if (ctrl.signal.aborted) return;
        setLoadError(err instanceof Error ? err.message : "Couldn't load the experiment.");
      }
    })();
    return () => ctrl.abort();
  }, [experimentId, pollKey, waker]);

  const status = exp?.status;
  const runs = useMemo(() => trafficRuns(exp?.trafficRuns), [exp?.trafficRuns]);
  const shownRun = selectedRun(runs, runParam);
  // Only ask for a specific run when the user picked an older one; the latest is the api's default.
  const fetchRun = runParam !== null && shownRun?.run === runParam && runs.length > 0 && runParam !== runs[runs.length - 1].run ? runParam : null;

  // Metrics + the per-creative series: once per status change, and on an
  // interval while traffic runs. Each keeps its last good value on failure (the
  // status poll surfaces real failures; the series is optional, contracts §8).
  const loadMetrics = useCallback(
    async (signal?: AbortSignal) => {
      const [m, c] = await Promise.allSettled([
        getExperimentMetrics(experimentId, { signal, run: fetchRun }),
        getCreativeSeries(experimentId, { signal, run: fetchRun }),
      ]);
      if (signal?.aborted) return;
      if (m.status === "fulfilled") setMetrics(m.value);
      if (c.status === "fulfilled") setSeries(c.value.creatives.length ? c.value : null);
    },
    [experimentId, fetchRun]
  );
  // A different run: drop the previous run's numbers rather than show them under the new label.
  useEffect(() => {
    setMetrics(null);
    setSeries(null);
  }, [fetchRun]);
  useEffect(() => {
    if (!status) return;
    const ctrl = new AbortController();
    loadMetrics(ctrl.signal);
    const t =
      status === "running_traffic"
        ? setInterval(() => loadMetrics(ctrl.signal), METRICS_INTERVAL_MS)
        : undefined;
    return () => {
      ctrl.abort();
      if (t) clearInterval(t);
    };
  }, [status, loadMetrics]);

  // Every episode has landed but the status still says running: ask again now.
  const stale = statusLooksStale(exp, metrics);
  useEffect(() => {
    if (stale) waker.wake();
  }, [stale, waker]);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const arms = useMemo(() => [...(exp?.arms ?? [])].sort((a, b) => a.index - b.index), [exp?.arms]);
  const runView = useMemo(
    () => (hasMetrics(metrics) ? buildRunView(shownRun, metrics, series) : null),
    [shownRun, metrics, series]
  );
  // The shown run kept learning (contracts §11): one stitched stream, no bands, batch means.
  const continuous =
    isContinuousRun(shownRun) ||
    (hasMetrics(metrics) &&
      (isContinuousMetrics(metrics) || parseContinuousSummary(metrics.continuousSummary) !== null));
  const continuousView = useMemo(() => {
    if (!continuous) return null;
    // The run record has the plan (segments × rounds); the metrics, what has landed so far
    // (for a continuous run `horizon` is the rounds covered, `segmentHorizon` the segment length).
    const run = shownRun && isContinuousRun(shownRun) ? shownRun : null;
    const segmentRounds = run?.horizon ?? metrics?.segmentHorizon ?? null;
    const segments = run?.episodes ?? metrics?.episodes ?? 0;
    const shape = { episodes: segments, horizon: segmentRounds, learning: "continuous" as const };
    return {
      segments,
      segmentRounds,
      totalRounds: runRounds(shape) ?? metrics?.horizon ?? null,
      markers: segmentMarkers(shape, { starts: metrics?.segmentStarts }),
    };
  }, [continuous, shownRun, metrics?.episodes, metrics?.horizon, metrics?.segmentHorizon, metrics?.segmentStarts]);
  const insights = useMemo(
    () =>
      buildInsights({
        continuous: continuousView
          ? { segments: continuousView.segments, totalRounds: continuousView.totalRounds }
          : null,
        shifts: runView?.shifts,
        forget: runView?.forget,
        metrics: hasMetrics(metrics) ? metrics : null,
        series,
        arms,
        rewardMode: exp?.rewardMode,
        ctrMode: exp?.ctrMode,
        scenario: exp?.scenario,
        scenarioOverrides: exp?.scenarioOverrides,
        policyDiscount: exp?.policyDiscount,
      }),
    [
      metrics,
      series,
      arms,
      exp?.rewardMode,
      exp?.ctrMode,
      exp?.scenario,
      exp?.scenarioOverrides,
      exp?.policyDiscount,
      runView,
      continuousView,
    ]
  );
  const lanes = useMemo(
    () => buildLanes(arms, hasMetrics(metrics) ? metrics : null, hasMetrics(metrics) ? series : null),
    [arms, metrics, series]
  );
  const liveSeries = hasMetrics(metrics) ? series : null;
  const grid = useMemo(
    () => segmentGrid(liveSeries, lanes.map((l) => l.creativeId)),
    [liveSeries, lanes]
  );
  const openLane = lanes.find((l) => l.creativeId === openCreative) ?? null;
  const regimesByLane = useMemo(
    () => (runView ? laneRegimes(lanes, runView.seriesRegimes, runView.metricRegimes, runView.labels) : {}),
    [runView, lanes]
  );
  const shiftCtx = useMemo<ShiftContext | null>(() => {
    if (!exp || arms.length < 2 || !["clear_winner", "segment_winners", "drift"].includes(exp.scenario)) return null;
    const scenario = exp.scenario as Scenario;
    const ctrMode = (exp.ctrMode === "realistic" ? "realistic" : "demo") as CtrMode;
    return {
      scenario,
      ctrMode,
      horizon: defaultHorizon(exp.scenario, exp.ctrMode),
      creatives: arms.map((a) => ({
        creativeId: a.creativeId,
        name: armName(a),
        color: armColor(arms, a.creativeId),
        scores: a.scores ?? {},
      })),
      values: valuesFromOverrides(scenario, exp.scenarioOverrides),
    };
  }, [exp, arms]);
  const forget = forgetChoice ?? defaultForget(draftShifts.length);

  if (!exp) {
    return (
      <div className="mx-auto w-full max-w-6xl px-6 py-8">
        {loadError ? (
          <div className="rounded-lg border border-border bg-card p-6">
            <h1 className="text-lg font-semibold text-foreground">This experiment didn&apos;t load</h1>
            <p className="mt-1 text-sm text-mark-fail">{loadError}</p>
            <p className="mt-2 text-sm text-muted-foreground">
              Check the link, or open it again from your experiments.
            </p>
            <Link href="/experiments" className={`${buttonVariants({ variant: "outline" })} mt-4`}>
              Open experiments
            </Link>
          </div>
        ) : (
          <p className="py-16 text-center text-sm text-muted-foreground" role="status">
            Loading experiment…
          </p>
        )}
      </div>
    );
  }

  const horizon = defaultHorizon(exp.scenario, exp.ctrMode);
  const ttl = canStop(exp.status) ? ttlText(exp.ttlExpiresAt, now) : "";
  const keepLearning = learning === "continuous";
  const limitError = keepLearning ? continuousLimitError(episodes, horizon) : null;
  const blockedCounts = keepLearning
    ? EPISODE_OPTIONS.filter((n) => continuousLimitError(n, horizon) !== null)
    : [];
  // Switching to Keep learning moves an over-long choice down to the longest stream that fits.
  const changeLearning = (mode: LearningMode) => {
    setLearning(mode);
    if (mode === "continuous" && continuousLimitError(episodes, horizon) !== null) {
      const fits = EPISODE_OPTIONS.filter((n) => continuousLimitError(n, horizon) === null);
      if (fits.length) setEpisodes(fits[fits.length - 1]);
    }
  };

  const onStartTraffic = async () => {
    setActionError(null);
    setShiftError(null);
    if (shiftCtx && validateShifts(draftShifts, shiftCtx).length) {
      setShiftError("Fix the highlighted shifts before starting traffic.");
      return;
    }
    setBusy("traffic");
    try {
      await startTraffic(experimentId, episodes, horizon, { shifts: draftShifts, forget, learning });
      setExp((e) => (e ? { ...e, status: "running_traffic" } : e));
      // The new run becomes the latest: follow it.
      setRunParam(null);
      setPollKey((k) => k + 1);
    } catch (err) {
      if (err instanceof InvalidShiftsError) setShiftError(err.message);
      else setActionError(err instanceof Error ? err.message : "Couldn't start traffic.");
    } finally {
      setBusy(null);
    }
  };

  const onStop = async () => {
    setBusy("stop");
    setActionError(null);
    try {
      const res = await stopExperiment(experimentId);
      setExp((e) => (e ? { ...e, status: res.status } : e));
      setConfirmStop(false);
      setPollKey((k) => k + 1);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Couldn't stop the experiment.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mx-auto w-full max-w-6xl px-6 py-8">
      <Link
        href="/experiments"
        className="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
      >
        Experiments
      </Link>

      {/* Header */}
      <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold text-foreground">
            {scenarioLabel(exp.scenario)} <CustomBadge overrides={exp.scenarioOverrides} />
          </h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {ctrModeLabel(exp.ctrMode)}, {rewardModeLabel(exp.rewardMode).toLowerCase()},{" "}
            {arms.length} creatives
          </p>
          <p className="mt-1 font-mono text-xs text-muted-foreground">{exp.experimentId}</p>
          <OverridesDisclosure scenario={exp.scenario} overrides={exp.scenarioOverrides} />
        </div>
        <div className="flex flex-col items-end gap-1">
          <span className="inline-flex items-center gap-1">
            <ExperimentStatusLabel status={exp.status} className="text-sm" />
            <InfoTip label="About experiment status" align="end">
              <StatusHelp current={exp.status} />
            </InfoTip>
          </span>
          {ttl && (
            <span className="inline-flex items-center gap-1">
              <span className="text-xs text-muted-foreground tabular-nums">Endpoint: {ttl}</span>
              <InfoTip label="About the endpoint timer" align="end">
                {CONTROL_HELP.ttl}
              </InfoTip>
            </span>
          )}
        </div>
      </div>

      {/* Status + controls */}
      <section aria-label="Experiment controls" className="mt-4 rounded-lg border border-border bg-card p-4">
        <StatusNote exp={exp} continuous={isContinuousRun(runs[runs.length - 1])} />

        {shiftCtx && (exp.status === "ready" || exp.status === "deploying" || exp.status === "running_traffic") && (
          <div className="mt-4">
            <ShiftTimeline
              ctx={shiftCtx}
              shifts={draftShifts}
              onChange={(next) => {
                setDraftShifts(next);
                setShiftError(null);
              }}
              forget={forget}
              onForgetChange={setForgetChoice}
              disabled={exp.status !== "ready" || busy !== null}
              serverError={shiftError}
            />
          </div>
        )}

        <div className="mt-3 flex flex-wrap items-end gap-x-6 gap-y-3">
          <div>
            <div className="mb-1.5 flex items-center gap-1">
              <FieldLabel id="traffic-learning-label">Learning</FieldLabel>
              <InfoTip label="About learning" align="start">
                {CONTROL_HELP.learning}
              </InfoTip>
            </div>
            <SegmentedControl
              labelledBy="traffic-learning-label"
              options={LEARNING_OPTIONS}
              value={learning}
              onChange={changeLearning}
              disabled={exp.status !== "ready" || busy !== null}
            />
          </div>
          <div>
            <div className="mb-1.5 flex items-center gap-1">
              <FieldLabel as="label" htmlFor="traffic-episodes">
                {keepLearning ? "Segments" : "Episodes"}
              </FieldLabel>
              <InfoTip label={keepLearning ? "About segments" : "About episodes"}>
                {keepLearning ? CONTROL_HELP.segments : CONTROL_HELP.episodes}
              </InfoTip>
            </div>
            <select
              id="traffic-episodes"
              value={episodes}
              onChange={(e) => setEpisodes(Number(e.target.value))}
              disabled={exp.status !== "ready"}
              aria-describedby={keepLearning ? "traffic-stream-total" : undefined}
              className="h-8 rounded-sm border border-input bg-card px-2 text-sm tabular-nums disabled:opacity-50"
            >
              {EPISODE_OPTIONS.map((n) => (
                <option key={n} value={n} disabled={keepLearning && continuousLimitError(n, horizon) !== null}>
                  {n}
                </option>
              ))}
            </select>
          </div>
          <div>
            <div className="mb-1.5 flex items-center gap-1">
              <FieldLabel>{keepLearning ? "Rounds per segment" : "Rounds per episode"}</FieldLabel>
              <InfoTip label={keepLearning ? "About rounds per segment" : "About rounds per episode"}>
                {keepLearning ? CONTROL_HELP.segmentRounds : CONTROL_HELP.rounds}
              </InfoTip>
            </div>
            <p className="h-8 text-sm leading-8 text-foreground tabular-nums">{formatInt(horizon)}</p>
          </div>
          {keepLearning && (
            <div id="traffic-stream-total" className="min-w-0 max-w-[22rem]">
              <p className="h-8 text-sm leading-8 font-medium text-foreground tabular-nums">
                {limitError ? "Too long for one stream" : `${formatInt(episodes * horizon)} rounds in one stream`}
              </p>
            </div>
          )}
          <div className="flex items-center gap-1.5">
            <Button
              onClick={onStartTraffic}
              disabled={exp.status !== "ready" || busy !== null || limitError !== null}
            >
              {busy === "traffic"
                ? "Starting…"
                : draftShifts.length
                  ? `Start traffic with ${draftShifts.length} ${draftShifts.length === 1 ? "shift" : "shifts"}`
                  : "Start traffic"}
            </Button>
            <InfoTip label="About start traffic">{CONTROL_HELP.startTraffic}</InfoTip>
          </div>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {confirmStop ? (
              <div
                role="group"
                aria-labelledby="stop-confirm-title"
                aria-describedby="stop-confirm-body"
                className="flex flex-wrap items-center gap-x-3 gap-y-2"
              >
                <div className="max-w-sm">
                  <p id="stop-confirm-title" className="text-sm font-medium text-foreground">
                    {STOP_CONFIRM.title}
                  </p>
                  <p id="stop-confirm-body" className="text-xs text-muted-foreground">
                    {STOP_CONFIRM.body}
                  </p>
                </div>
                <Button variant="destructive" onClick={onStop} disabled={busy !== null}>
                  {busy === "stop" ? "Stopping…" : STOP_CONFIRM.confirm}
                </Button>
                <Button variant="ghost" onClick={() => setConfirmStop(false)} disabled={busy !== null}>
                  {STOP_CONFIRM.cancel}
                </Button>
              </div>
            ) : (
              <>
                <Button
                  variant="outline"
                  onClick={() => setConfirmStop(true)}
                  disabled={!canStop(exp.status) || busy !== null}
                >
                  Stop
                </Button>
                <InfoTip label="About stop" align="end">
                  {CONTROL_HELP.stop}
                </InfoTip>
              </>
            )}
          </div>
        </div>
        {keepLearning && (limitError || blockedCounts.length > 0) && (
          <p className="mt-2 max-w-[72ch] text-xs text-muted-foreground tabular-nums">
            {limitError ??
              `One stream is capped at ${formatInt(MAX_CONTINUOUS_ROUNDS)} rounds, so ${joinCounts(
                blockedCounts
              )} segments aren't available at ${formatInt(horizon)} rounds per segment.`}
          </p>
        )}
        {actionError && (
          <p role="alert" className="mt-3 text-sm text-mark-fail">
            {actionError}
          </p>
        )}
        {loadError && (
          <p role="status" className="mt-3 text-sm text-mark-pending">
            Lost contact with the experiment ({loadError}). Still retrying.
          </p>
        )}
      </section>

      {/* Results: Overview (creative scoreboard) | Analysis (charts) */}
      <Tabs
        value={view}
        onValueChange={(v) => setView(parseView(String(v)))}
        className="mt-8 gap-6"
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
          <TabsList aria-label="Results view" className="h-9">
            <TabsTrigger value="overview" className="px-4 font-semibold">
              Overview
            </TabsTrigger>
            <TabsTrigger value="analysis" className="px-4 font-semibold">
              Analysis
            </TabsTrigger>
          </TabsList>
          <div className="flex flex-wrap items-center gap-3">
            {shownRun && <RunSelector runs={runs} selected={shownRun} onSelect={setRunParam} />}
            <ExplainSwitch on={explain} onChange={setExplain} />
          </div>
        </div>

        <TabsContent value="overview">
          <CreativeScoreboard
            lanes={lanes}
            insights={insights}
            series={liveSeries}
            explain={explain}
            emptyMessage={scoreboardDirection(exp.status)}
            onOpen={hasMetrics(metrics) ? setOpenCreative : undefined}
            runView={runView}
            laneRegimes={regimesByLane}
          />
          {grid && (
            <SegmentGridView
              grid={grid}
              lanes={lanes}
              explain={explain}
              onOpen={setOpenCreative}
              periods={
                runView && runView.seriesRegimes.length >= 2
                  ? { labels: runView.labels, regimes: runView.seriesRegimes }
                  : null
              }
            />
          )}
        </TabsContent>

        <TabsContent value="analysis">
          <section aria-labelledby="arms-heading" className="">
            <h2 id="arms-heading" className="mb-3 text-sm font-semibold text-foreground">
              Creatives under test <span className="font-normal text-muted-foreground tabular-nums">{arms.length}</span>
            </h2>
            <ul className="grid grid-cols-2 gap-3 md:grid-cols-4">
              {arms.map((arm) => (
                <li key={arm.creativeId} className="flex flex-col rounded-lg border border-border bg-card p-2">
                  <ProofImage src={armImageUrl(arm)} alt={arm.conceptName} className="aspect-square w-full" />
                  <div className="flex flex-1 flex-col px-1 pt-2">
                    <p className="flex items-start gap-1.5 text-sm leading-snug font-medium text-foreground">
                      <span
                        aria-hidden
                        className="mt-1.5 size-2 shrink-0 rounded-full"
                        style={{ backgroundColor: armColor(arms, arm.creativeId) }}
                      />
                      <span className="line-clamp-2">{arm.label || armName(arm)}</span>
                    </p>
                    <p className="mt-0.5 truncate text-xs text-muted-foreground">{armName(arm)}</p>
                    <div className="mt-auto flex items-center justify-between pt-2 text-xs text-muted-foreground tabular-nums">
                      <span className="inline-flex items-center gap-1">
                        {arm.overallScore === null ? "Not scored" : `Score ${Math.round(arm.overallScore * 100)}%`}
                        <InfoTip label="About the score" align="start">
                          {CARD_HELP.score}
                        </InfoTip>
                      </span>
                      <span className="inline-flex items-center gap-1">
                        <span className="font-mono">{shortId(arm.creativeId)}</span>
                        <InfoTip label="About the creative id" align="end">
                          {CARD_HELP.creativeId}
                        </InfoTip>
                      </span>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </section>
          {/* Metrics */}
          <section aria-labelledby="metrics-heading" className="mt-6">
            <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
              <h2 id="metrics-heading" className="text-sm font-semibold text-foreground">
                Strategies compared
              </h2>
              {hasMetrics(metrics) && (
                <p className="text-xs text-muted-foreground tabular-nums">
                  {continuousView ? (
                    <>
                      {metrics.episodes < continuousView.segments ? `${metrics.episodes} of ` : ""}
                      {continuousView.segments} {continuousView.segments === 1 ? "segment" : "segments"}
                      {continuousView.segmentRounds ? ` of ${formatInt(continuousView.segmentRounds)} rounds` : ""}, one
                      stream that keeps learning
                    </>
                  ) : (
                    <>
                      {metrics.episodes} {metrics.episodes === 1 ? "episode" : "episodes"}
                      {metrics.horizon ? ` of ${formatInt(metrics.horizon)} rounds` : ""}
                    </>
                  )}
                </p>
              )}
            </div>
            {hasMetrics(metrics) ? (
              <>
                <ExperimentCharts
                  metrics={metrics}
                  arms={arms}
                  rewardMode={exp.rewardMode}
                  readings={insights.readings}
                  explain={explain}
                  runView={runView}
                  continuous={continuousView}
                />
                <p className="mt-3 text-xs text-muted-foreground">
                  {exp.ctrMode === "demo" ? "Demo mode inflates click rates; " : ""}
                  pseudo-regret uses the simulator&apos;s true click probabilities.
                </p>
              </>
            ) : (
              <div className="rounded-lg border border-border bg-card px-4 py-6 text-sm text-muted-foreground">
                {exp.status === "running_traffic"
                  ? `Traffic is running. Charts appear when the first ${
                      isContinuousRun(shownRun) ? "segment" : "episode"
                    } finishes.`
                  : exp.status === "ready"
                    ? "No traffic yet. Start traffic to simulate readers; each episode replays the same readers for every policy so they can be compared fairly."
                    : exp.status === "deploying"
                      ? "Charts appear here once the endpoint is ready and traffic has run."
                      : "This experiment has no results: no traffic ran before the endpoint was removed."}
              </div>
            )}
          </section>
        </TabsContent>
      </Tabs>

      <CreativeDetailDrawer
        lane={hasMetrics(metrics) ? openLane : null}
        lanes={lanes}
        series={liveSeries}
        explain={explain}
        onClose={() => setOpenCreative(null)}
        runView={runView}
        continuous={continuous}
      />
    </div>
  );
}

/** The scoreboard's direction before there are results. */
function scoreboardDirection(status: string): string {
  switch (status) {
    case "running_traffic":
      return "Traffic is running. The scoreboard fills in when the first episode finishes.";
    case "ready":
      return "Start traffic to see how each creative performs.";
    case "deploying":
      return "Once the endpoint is ready, start traffic to see how each creative performs.";
    default:
      return "No traffic ran before the endpoint was removed, so there is nothing to score. Deploy again from the run's results page.";
  }
}

function StatusNote({ exp, continuous = false }: { exp: ExperimentSummary; continuous?: boolean }) {
  const p = exp.progress;
  const unit = continuous ? "segment" : "episode";
  switch (exp.status) {
    case "deploying":
      return (
        <p className="text-sm text-foreground" role="status">
          Deploying the endpoint — this usually takes 10–20 minutes. You can leave this page; it
          keeps deploying.
        </p>
      );
    case "ready":
      return <p className="text-sm text-foreground">The endpoint is live. Start traffic to simulate readers.</p>;
    case "running_traffic":
      return (
        <p className="text-sm text-foreground tabular-nums" role="status">
          Simulating readers
          {p ? `: ${unit} ${Math.min(p.episodesDone + 1, p.episodesTotal)} of ${p.episodesTotal}` : ""}
          {continuous ? ", one stream that keeps learning" : ""}. Charts update as {unit}s finish.
        </p>
      );
    case "stopping":
      return (
        <p className="text-sm text-foreground" role="status">
          Stopping: removing the endpoint.
        </p>
      );
    case "stopped":
      return <p className="text-sm text-muted-foreground">Stopped. The endpoint was deleted; results stay here.</p>;
    case "expired":
      return (
        <p className="text-sm text-muted-foreground">
          Expired. The endpoint reached its lifetime and was deleted; results stay here.
        </p>
      );
    case "failed":
      return (
        <p className="text-sm text-mark-fail">
          The experiment failed{exp.error ? `: ${exp.error}` : "."} Deploy again from the run&apos;s results page.
        </p>
      );
    default:
      return null;
  }
}
