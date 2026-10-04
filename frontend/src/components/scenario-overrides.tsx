"use client";

import { ChevronRight } from "lucide-react";
import { InfoTip } from "@/components/ui/info-tip";
import { hasOverrides, type ScenarioOverrides } from "@/lib/experiments";
import { TUNE_HELP } from "@/lib/experiment-help";
import { describeOverrides } from "@/lib/scenario-preview";
import { cn } from "@/lib/utils";

/** "(custom)" after a scenario label when the experiment ran with tuned readers; nothing otherwise. */
export function CustomBadge({
  overrides,
  className,
}: {
  overrides: ScenarioOverrides | null | undefined;
  className?: string;
}) {
  if (!hasOverrides(overrides)) return null;
  return <span className={cn("font-normal text-muted-foreground", className)}>(custom)</span>;
}

/**
 * Disclosure listing a custom experiment's reader settings in plain language,
 * each with the scenario default it replaced. Renders nothing for preset experiments.
 */
export function OverridesDisclosure({
  scenario,
  overrides,
  defaultOpen = false,
}: {
  scenario: string;
  overrides: ScenarioOverrides | null | undefined;
  defaultOpen?: boolean;
}) {
  const lines = describeOverrides(scenario, overrides);
  if (!lines.length) return null;
  return (
    <details open={defaultOpen} className="group mt-2 max-w-2xl">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1 rounded-sm text-sm text-foreground outline-none select-none focus-visible:ring-3 focus-visible:ring-ring/50 [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden className="size-3.5 text-muted-foreground transition-transform group-open:rotate-90 motion-reduce:transition-none" />
        Tuned reader settings
        <span className="text-muted-foreground tabular-nums">({lines.length})</span>
        <InfoTip label="About tuned reader settings" align="start">
          {TUNE_HELP.custom}
        </InfoTip>
      </summary>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 border-l-2 border-border pl-4 text-sm">
        {lines.map((l) => (
          <div key={l.label} className="contents">
            <dt className="text-muted-foreground">{l.label}</dt>
            <dd className="text-foreground">
              {l.value}
              <span className="block text-xs text-muted-foreground">Scenario default: {l.preset}</span>
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
