"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { FieldLabel } from "@/components/field-label";
import { InfoTip, InfoTipList } from "@/components/ui/info-tip";
import { SegmentedControl } from "@/components/segmented-control";
import { proofScore, type Proof } from "@/lib/eval-matching";
import {
  ActiveExperimentError,
  createExperiment,
  CTR_MODE_OPTIONS,
  deployBlockedReason,
  REWARD_MODE_OPTIONS,
  SCENARIO_OPTIONS,
  selectionToPayload,
  TTL_OPTIONS,
  type CtrMode,
  type RewardMode,
  type Scenario,
} from "@/lib/experiments";
import {
  CTR_MODE_HELP,
  DEPLOY_HELP,
  REWARD_HELP,
  SCENARIO_HELP,
} from "@/lib/experiment-help";
import { cn } from "@/lib/utils";
import { ProofImage } from "./proof-grid";
import { pct } from "./score-mark";

/** "ⓘ" list content: every option's label with its one-line explanation. */
const optionHelp = <T extends string>(opts: { value: T; label: string }[], help: Record<T, string>) => (
  <InfoTipList items={opts.map((o) => ({ term: o.label, text: help[o.value] }))} />
);

/**
 * Optional last step on the results page: pick 2–4 creatives and deploy them as
 * the arms of a contextual bandit experiment (docs/bandit/contracts.md §5).
 */
export function DeployPanel({
  proofs,
  appName,
  sessionId,
  imageUrlFor,
  stoppedEarly,
}: {
  /** Pipeline order (Proof.index is the creative index the backend expects). */
  proofs: Proof[];
  appName: string;
  sessionId: string;
  imageUrlFor: (conceptName: string) => string | null;
  stoppedEarly: boolean;
}) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [scenario, setScenario] = useState<Scenario>("segment_winners");
  const [ctrMode, setCtrMode] = useState<CtrMode>("demo");
  const [rewardMode, setRewardMode] = useState<RewardMode>("click");
  const [ttlMinutes, setTtlMinutes] = useState<number>(120);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeConflict, setActiveConflict] = useState(false);

  const ordered = [...proofs].sort((a, b) => a.index - b.index);
  const count = selected.size;
  const blocked = deployBlockedReason(count, stoppedEarly);

  const toggle = (index: number) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });

  const deploy = async () => {
    if (blocked || submitting) return;
    setSubmitting(true);
    setError(null);
    setActiveConflict(false);
    try {
      const { experimentId } = await createExperiment(
        selectionToPayload({ appName, sessionId, selected, scenario, ctrMode, rewardMode, ttlMinutes })
      );
      router.push(`/experiments/${encodeURIComponent(experimentId)}`);
    } catch (err) {
      if (err instanceof ActiveExperimentError) setActiveConflict(true);
      else setError(err instanceof Error ? err.message : "Couldn't deploy the experiment.");
      setSubmitting(false);
    }
  };

  return (
    <section
      aria-labelledby="deploy-heading"
      className="mb-6 rounded-lg border border-border bg-card p-5"
    >
      <h2 id="deploy-heading" className="text-base font-semibold text-foreground">
        Deploy creatives as a live experiment
      </h2>
      <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
        A bandit chooses which creative to show each reader of a page about the trend, learns
        from simulated clicks, and shifts traffic toward what works for each kind of reader.
      </p>

      <fieldset className="mt-4">
        <div className="mb-2 flex flex-wrap items-center gap-3">
          <FieldLabel as="legend" className="float-left">
            Creatives to test{" "}
            <span className="tabular-nums text-foreground">
              {count} of {ordered.length}
            </span>
          </FieldLabel>
          <InfoTip label="About choosing creatives" align="start">
            {DEPLOY_HELP.creatives}
          </InfoTip>
          <span className="flex gap-1">
            <Button
              variant="ghost"
              size="xs"
              onClick={() => setSelected(new Set(ordered.slice(0, 4).map((p) => p.index)))}
            >
              Select all
            </Button>
            <Button variant="ghost" size="xs" onClick={() => setSelected(new Set())}>
              Select none
            </Button>
          </span>
        </div>
        <ul className="clear-left grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {ordered.map((p) => {
            const checked = selected.has(p.index);
            const score = proofScore(p);
            const id = `deploy-creative-${p.index}`;
            return (
              <li key={p.index}>
                <label
                  htmlFor={id}
                  className={cn(
                    "flex h-full cursor-pointer items-start gap-3 rounded-md border p-2 transition-colors has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50",
                    checked ? "border-primary bg-primary/5" : "border-border hover:border-foreground/40"
                  )}
                >
                  <input
                    id={id}
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(p.index)}
                    className="mt-1 size-4 shrink-0 accent-primary"
                  />
                  <ProofImage
                    src={imageUrlFor(p.concept.concept_name)}
                    alt=""
                    className="size-14 shrink-0 rounded-sm text-[10px]"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="line-clamp-2 text-sm leading-snug font-medium text-foreground">
                      {p.concept.headline}
                    </span>
                    <span className="mt-1 block text-xs text-muted-foreground tabular-nums">
                      {score === null ? "Not scored" : `Score ${pct(score)}%`}
                    </span>
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      </fieldset>

      <div className="mt-5 grid gap-5 md:grid-cols-2 xl:grid-cols-4">
        <div>
          <div className="mb-1.5 flex items-center gap-1">
            <FieldLabel id="deploy-scenario-label">Scenario</FieldLabel>
            <InfoTip label="About scenarios" align="start">
              {optionHelp(SCENARIO_OPTIONS, SCENARIO_HELP)}
            </InfoTip>
          </div>
          <SegmentedControl
            labelledBy="deploy-scenario-label"
            options={SCENARIO_OPTIONS}
            value={scenario}
            onChange={setScenario}
          />
          <p className="mt-1.5 text-xs text-muted-foreground">{SCENARIO_HELP[scenario]}</p>
        </div>
        <div>
          <div className="mb-1.5 flex items-center gap-1">
            <FieldLabel id="deploy-ctr-label">Click rates</FieldLabel>
            <InfoTip label="About click rates" align="start">
              {optionHelp(CTR_MODE_OPTIONS, CTR_MODE_HELP)}
            </InfoTip>
          </div>
          <SegmentedControl
            labelledBy="deploy-ctr-label"
            options={CTR_MODE_OPTIONS}
            value={ctrMode}
            onChange={setCtrMode}
          />
          <p className="mt-1.5 text-xs text-muted-foreground">
            {CTR_MODE_HELP[ctrMode]}
          </p>
        </div>
        <div>
          <div className="mb-1.5 flex items-center gap-1">
            <FieldLabel id="deploy-reward-label">Reward</FieldLabel>
            <InfoTip label="About reward" align="start">
              {optionHelp(REWARD_MODE_OPTIONS, REWARD_HELP)}
            </InfoTip>
          </div>
          <SegmentedControl
            labelledBy="deploy-reward-label"
            options={REWARD_MODE_OPTIONS}
            value={rewardMode}
            onChange={setRewardMode}
          />
          <p className="mt-1.5 text-xs text-muted-foreground">{REWARD_HELP[rewardMode]}</p>
        </div>
        <div>
          <div className="mb-1.5 flex items-center gap-1">
            <FieldLabel as="label" htmlFor="deploy-ttl">
              Endpoint lifetime
            </FieldLabel>
            <InfoTip label="About endpoint lifetime" align="start">
              {DEPLOY_HELP.ttl}
            </InfoTip>
          </div>
          <select
            id="deploy-ttl"
            value={ttlMinutes}
            onChange={(e) => setTtlMinutes(Number(e.target.value))}
            className="h-8 rounded-sm border border-input bg-card px-2 text-sm tabular-nums"
          >
            {TTL_OPTIONS.map((m) => (
              <option key={m} value={m}>
                {m} min
              </option>
            ))}
          </select>
          <p className="mt-1.5 text-xs text-muted-foreground">
            The endpoint is torn down automatically after this, or when you stop it.
          </p>
        </div>
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-border pt-4">
        <Button onClick={deploy} disabled={Boolean(blocked) || submitting}>
          {submitting
            ? "Deploying…"
            : `Deploy ${count} ${count === 1 ? "creative" : "creatives"}`}
        </Button>
        {blocked && !submitting && (
          <p className="text-sm text-muted-foreground" role="status">
            {blocked}
          </p>
        )}
      </div>

      {activeConflict && (
        <div
          role="alert"
          className="mt-3 rounded-md border border-mark-pending/40 bg-mark-pending/5 px-3 py-2 text-sm text-foreground"
        >
          You already have a live experiment, and only one can run at a time. Stop it on{" "}
          <Link href="/experiments" className="font-medium text-primary underline-offset-4 hover:underline">
            your experiments
          </Link>
          , then deploy these creatives.
        </div>
      )}
      {error && (
        <p role="alert" className="mt-3 text-sm text-mark-fail">
          {error}
        </p>
      )}
    </section>
  );
}
