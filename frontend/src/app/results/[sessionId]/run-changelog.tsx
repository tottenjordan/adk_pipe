"use client";

import { FieldLabel } from "@/components/field-label";
import { QuietDisclosure } from "@/components/quiet-disclosure";
import type { RunChangelog } from "@/lib/api";

const ORIGIN_LABELS: Record<RunChangelog["origin"], string> = {
  studio: "Trend Trawler Studio",
  a2a: "A2A",
  gemini_enterprise: "Gemini Enterprise",
};

/**
 * Collapsed-by-default disclosure summarizing session provenance, source counts,
 * deterministic quality-gate revision rounds, timeline milestones, and warnings.
 */
export function RunChangelogPanel({
  changelog,
  loading,
  error,
  onOpen,
  className,
}: {
  changelog: RunChangelog | null;
  loading: boolean;
  error: string | null;
  onOpen: () => void;
  className?: string;
}) {
  return (
    <QuietDisclosure
      title="Run changelog"
      count={changelog?.entries.length}
      onOpenChange={(open) => {
        if (open) onOpen();
      }}
      className={className}
    >
      {loading && !changelog ? (
        <p className="text-sm text-muted-foreground" role="status">
          Loading changelog…
        </p>
      ) : error ? (
        <p className="text-sm text-mark-fail" role="alert">
          {error}
        </p>
      ) : changelog ? (
        <div className="space-y-4">
          <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-3">
            <div>
              <FieldLabel as="dt">Origin</FieldLabel>
              <dd className="mt-0.5 text-sm text-foreground">
                {ORIGIN_LABELS[changelog.origin] ?? changelog.origin}
                {changelog.handoff_source_session_id && (
                  <span className="mt-0.5 block font-mono text-xs text-muted-foreground">
                    Source: {changelog.handoff_source_session_id}
                  </span>
                )}
              </dd>
            </div>
            <div>
              <FieldLabel as="dt">Sources</FieldLabel>
              <dd className="mt-0.5 text-sm text-foreground tabular-nums">
                {changelog.source_counts.web} web · {changelog.source_counts.enterprise} enterprise
              </dd>
            </div>
            <div>
              <FieldLabel as="dt">Gate revisions</FieldLabel>
              <dd className="mt-0.5 text-sm text-foreground tabular-nums">
                Brief {changelog.gate_rounds.brief} · Copy {changelog.gate_rounds.ad_copy} · Visual{" "}
                {changelog.gate_rounds.visual_concept}
                {typeof changelog.gate_rounds.video_storyboard === "number"
                  ? ` · Storyboard ${changelog.gate_rounds.video_storyboard}`
                  : ""}
              </dd>
            </div>
          </dl>

          {changelog.entries.length > 0 && (
            <div className="border-t border-border pt-3">
              <FieldLabel as="h3">Timeline</FieldLabel>
              <ol className="mt-2 space-y-2">
                {changelog.entries.map((entry, idx) => (
                  <li key={`${entry.kind}-${idx}`} className="text-sm">
                    <span className="font-medium text-foreground">{entry.label}</span>
                    {entry.detail && (
                      <p className="text-xs text-muted-foreground">{entry.detail}</p>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}

          {changelog.warnings.length > 0 && (
            <div className="border-t border-border pt-3">
              <FieldLabel as="h3">Run warnings</FieldLabel>
              <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
                {changelog.warnings.map((w, idx) => (
                  <li key={idx}>{w}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">No changelog entries recorded.</p>
      )}
    </QuietDisclosure>
  );
}
