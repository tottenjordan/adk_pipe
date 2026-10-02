"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Button, buttonVariants } from "@/components/ui/button";
import { FieldLabel } from "@/components/field-label";
import { ExperimentStatusLabel } from "@/components/experiment-status";
import { ProofImage } from "@/app/results/[sessionId]/proof-grid";
import { formatInt } from "@/lib/chart";
import {
  armColor,
  armImageUrl,
  armName,
  canStop,
  ctrModeLabel,
  defaultHorizon,
  EPISODE_OPTIONS,
  getExperimentMetrics,
  hasMetrics,
  pollExperiment,
  rewardModeLabel,
  scenarioLabel,
  shortId,
  startTraffic,
  stopExperiment,
  ttlText,
  type ExperimentMetrics,
  type ExperimentSummary,
} from "@/lib/experiments";
import { ExperimentCharts } from "./experiment-charts";

const METRICS_INTERVAL_MS = 10_000;

export default function ExperimentPage({
  params,
}: {
  params: Promise<{ experimentId: string }>;
}) {
  const { experimentId } = use(params);
  const [exp, setExp] = useState<ExperimentSummary | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<ExperimentMetrics | null>(null);
  const [pollKey, setPollKey] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [episodes, setEpisodes] = useState<number>(20);
  const [busy, setBusy] = useState<"traffic" | "stop" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);

  // Follow the experiment's status until it settles; restarted (pollKey) after an action.
  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      try {
        for await (const s of pollExperiment(experimentId, { signal: ctrl.signal })) {
          setExp(s);
          setLoadError(null);
        }
      } catch (err) {
        if (ctrl.signal.aborted) return;
        setLoadError(err instanceof Error ? err.message : "Couldn't load the experiment.");
      }
    })();
    return () => ctrl.abort();
  }, [experimentId, pollKey]);

  const status = exp?.status;

  // Metrics: once per status change, and on an interval while traffic runs.
  const loadMetrics = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setMetrics(await getExperimentMetrics(experimentId, { signal }));
      } catch {
        // Keep the last good metrics; the status poll surfaces real failures.
      }
    },
    [experimentId]
  );
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

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

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
  const arms = [...exp.arms].sort((a, b) => a.index - b.index);

  const onStartTraffic = async () => {
    setBusy("traffic");
    setActionError(null);
    try {
      await startTraffic(experimentId, episodes, horizon);
      setExp((e) => (e ? { ...e, status: "running_traffic" } : e));
      setPollKey((k) => k + 1);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Couldn't start traffic.");
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
          <h1 className="text-2xl font-semibold text-foreground">{scenarioLabel(exp.scenario)}</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {ctrModeLabel(exp.ctrMode)}, {rewardModeLabel(exp.rewardMode).toLowerCase()},{" "}
            {arms.length} creatives
          </p>
          <p className="mt-1 font-mono text-xs text-muted-foreground">{exp.experimentId}</p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <ExperimentStatusLabel status={exp.status} className="text-sm" />
          {ttl && <p className="text-xs text-muted-foreground tabular-nums">Endpoint: {ttl}</p>}
        </div>
      </div>

      {/* Status + controls */}
      <section aria-label="Experiment controls" className="mt-4 rounded-lg border border-border bg-card p-4">
        <StatusNote exp={exp} />

        <div className="mt-3 flex flex-wrap items-end gap-x-6 gap-y-3">
          <div>
            <FieldLabel as="label" htmlFor="traffic-episodes" className="mb-1.5">
              Episodes
            </FieldLabel>
            <select
              id="traffic-episodes"
              value={episodes}
              onChange={(e) => setEpisodes(Number(e.target.value))}
              disabled={exp.status !== "ready"}
              className="h-8 rounded-sm border border-input bg-card px-2 text-sm tabular-nums disabled:opacity-50"
            >
              {EPISODE_OPTIONS.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </div>
          <div>
            <FieldLabel className="mb-1.5">Rounds per episode</FieldLabel>
            <p className="h-8 text-sm leading-8 text-foreground tabular-nums">{formatInt(horizon)}</p>
          </div>
          <Button
            onClick={onStartTraffic}
            disabled={exp.status !== "ready" || busy !== null}
          >
            {busy === "traffic" ? "Starting…" : "Start traffic"}
          </Button>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {confirmStop ? (
              <>
                <span className="text-sm text-foreground">Stop and delete the endpoint?</span>
                <Button variant="destructive" onClick={onStop} disabled={busy !== null}>
                  {busy === "stop" ? "Stopping…" : "Stop experiment"}
                </Button>
                <Button variant="ghost" onClick={() => setConfirmStop(false)} disabled={busy !== null}>
                  Keep it running
                </Button>
              </>
            ) : (
              <Button
                variant="outline"
                onClick={() => setConfirmStop(true)}
                disabled={!canStop(exp.status) || busy !== null}
              >
                Stop
              </Button>
            )}
          </div>
        </div>
        {actionError && (
          <p role="alert" className="mt-3 text-sm text-mark-fail">
            {actionError}
          </p>
        )}
        {loadError && (
          <p role="status" className="mt-3 text-sm text-mark-pending">
            Lost contact with the experiment ({loadError}). Reload to try again.
          </p>
        )}
      </section>

      {/* Arms */}
      <section aria-labelledby="arms-heading" className="mt-6">
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
                  <span className="line-clamp-2">{armName(arm)}</span>
                </p>
                <p className="mt-0.5 truncate text-xs text-muted-foreground">{arm.conceptName}</p>
                <div className="mt-auto flex items-center justify-between pt-2 text-xs text-muted-foreground tabular-nums">
                  <span>
                    {arm.overallScore === null ? "Not scored" : `Score ${Math.round(arm.overallScore * 100)}%`}
                  </span>
                  <span className="font-mono">{shortId(arm.creativeId)}</span>
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
            Results
          </h2>
          {hasMetrics(metrics) && (
            <p className="text-xs text-muted-foreground tabular-nums">
              {metrics.episodes} {metrics.episodes === 1 ? "episode" : "episodes"}
              {metrics.horizon ? ` of ${formatInt(metrics.horizon)} rounds` : ""}
            </p>
          )}
        </div>
        {hasMetrics(metrics) ? (
          <>
            <ExperimentCharts metrics={metrics} arms={arms} rewardMode={exp.rewardMode} />
            <p className="mt-3 text-xs text-muted-foreground">
              {exp.ctrMode === "demo" ? "Demo mode inflates click rates; " : ""}
              pseudo-regret uses the simulator&apos;s true click probabilities.
            </p>
          </>
        ) : (
          <div className="rounded-lg border border-border bg-card px-4 py-6 text-sm text-muted-foreground">
            {exp.status === "running_traffic"
              ? "Traffic is running. Charts appear when the first episode finishes."
              : exp.status === "ready"
                ? "No traffic yet. Start traffic to simulate readers; each episode replays the same readers for every policy so they can be compared fairly."
                : exp.status === "deploying"
                  ? "Charts appear here once the endpoint is ready and traffic has run."
                  : "This experiment has no results: no traffic ran before the endpoint was removed."}
          </div>
        )}
      </section>
    </div>
  );
}

function StatusNote({ exp }: { exp: ExperimentSummary }) {
  const p = exp.progress;
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
          {p ? `: episode ${Math.min(p.episodesDone + 1, p.episodesTotal)} of ${p.episodesTotal}` : ""}. Charts
          update as episodes finish.
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
