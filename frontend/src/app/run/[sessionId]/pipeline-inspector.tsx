"use client";

import { useMemo, useState } from "react";
import { ChevronDown } from "lucide-react";
import { FieldLabel } from "@/components/field-label";
import { cleanEnterpriseLabel } from "@/lib/research-report";

const ISSUE_LABELS: Record<string, string> = {
  image_qa: "Image check",
  ad_copy_critique: "Ad copy check",
  final_visual_concepts: "Visual concept check",
  creative_brief: "Creative brief",
  enterprise_research_findings: "Enterprise knowledge base",
  research_report_gcs_uri: "Research PDF save",
  eval_report_gcs_uri: "Eval report save",
  creative_gallery_gcs_uri: "Gallery save",
  creative_row_uuid: "Trend row save",
  eval_bq_row_uuid: "Eval row save",
};

function humanizeStageKey(key: string): string {
  if (ISSUE_LABELS[key]) return ISSUE_LABELS[key];
  const spaced = key.replace(/_/g, " ").trim();
  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : key;
}

function parseMaybeJson(raw: unknown): unknown {
  if (typeof raw !== "string") return raw;
  const trimmed = raw.trim();
  if (!trimmed) return "";
  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      return JSON.parse(trimmed);
    } catch {
      return trimmed;
    }
  }
  return trimmed;
}

function toSafeNonNegInt(raw: unknown): number {
  if (typeof raw === "boolean" || raw == null) return 0;
  const n = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : 0;
}

/** Extract search query strings from a planner schema object, JSON string, or array. */
export function extractSearchQueries(raw: unknown): string[] {
  const parsed = parseMaybeJson(raw);
  if (!parsed) return [];
  if (typeof parsed === "string") return [parsed];

  const list = Array.isArray(parsed)
    ? parsed
    : typeof parsed === "object" && parsed !== null
      ? ((parsed as Record<string, unknown>).queries ??
        (parsed as Record<string, unknown>).follow_up_queries)
      : null;

  if (!Array.isArray(list)) return [];
  const out: string[] = [];
  for (const item of list) {
    if (typeof item === "string" && item.trim()) {
      out.push(item.trim());
    } else if (item && typeof item === "object") {
      const obj = item as Record<string, unknown>;
      const q = obj.search_query ?? obj.query;
      if (typeof q === "string" && q.trim()) {
        out.push(q.trim());
      }
    }
  }
  return out;
}

export interface InspectorSourceItem {
  id: string;
  label: string;
  domain?: string;
}

/** Extract normalized source entries from `sources` or `enterprise_sources`. */
export function extractSourceItems(
  raw: unknown,
  isEnterprise = false
): InspectorSourceItem[] {
  const parsed = parseMaybeJson(raw);
  if (!parsed || typeof parsed !== "object") return [];

  const entries: Array<[string, unknown]> = Array.isArray(parsed)
    ? parsed.map((v, idx) => [`item-${idx + 1}`, v])
    : Object.entries(parsed as Record<string, unknown>);

  const out: InspectorSourceItem[] = [];
  for (const [id, val] of entries) {
    if (typeof val === "string" && val.trim()) {
      const label = isEnterprise ? cleanEnterpriseLabel(val) : val.trim();
      if (label) out.push({ id, label });
      continue;
    }
    if (!val || typeof val !== "object") continue;
    const obj = val as Record<string, unknown>;
    const rawTitle = typeof obj.title === "string" ? obj.title.trim() : "";
    const rawDomain = typeof obj.domain === "string" ? obj.domain.trim() : "";
    const rawUrl = typeof obj.url === "string" ? obj.url.trim() : "";

    const label = isEnterprise
      ? cleanEnterpriseLabel(rawTitle) ||
        cleanEnterpriseLabel(rawDomain) ||
        cleanEnterpriseLabel(rawUrl) ||
        id
      : rawTitle || rawDomain || rawUrl || id;
    const domain = isEnterprise
      ? cleanEnterpriseLabel(rawDomain)
      : rawDomain;

    out.push({
      id,
      label,
      ...(domain && domain !== label ? { domain } : {}),
    });
  }
  return out;
}

interface ResearchEvalSummary {
  findingType: string;
  comment: string;
  followUpQueries: string[];
}

function extractResearchEvaluation(raw: unknown): ResearchEvalSummary | null {
  const parsed = parseMaybeJson(raw);
  if (!parsed) return null;
  if (typeof parsed === "string") {
    return { findingType: "Evaluated", comment: parsed, followUpQueries: [] };
  }
  if (typeof parsed !== "object") return null;
  const obj = parsed as Record<string, unknown>;
  const findingType =
    typeof obj.finding_type === "string" ? obj.finding_type.trim() : "";
  const comment =
    typeof obj.analysis_comment === "string" ? obj.analysis_comment.trim() : "";
  const followUpQueries = extractSearchQueries(obj.follow_up_queries);
  if (!findingType && !comment && followUpQueries.length === 0) return null;
  return {
    findingType: findingType || "Evaluated",
    comment,
    followUpQueries,
  };
}

export interface ResidualWarning {
  key: string;
  stageLabel: string;
  kind: "issues" | "retry_exhausted";
  items: string[];
}

/** Scan polled `sessionState` for `<key>__issues` and `<key>__retry_exhausted` markers. */
export function extractResiduals(
  sessionState: Record<string, unknown>
): ResidualWarning[] {
  const out: ResidualWarning[] = [];
  for (const [fullKey, rawVal] of Object.entries(sessionState)) {
    if (!rawVal) continue;
    if (fullKey.endsWith("__retry_exhausted")) {
      const base = fullKey.slice(0, -"__retry_exhausted".length);
      out.push({
        key: fullKey,
        stageLabel: humanizeStageKey(base),
        kind: "retry_exhausted",
        items: [`Step '${base}' exhausted retries and produced no output.`],
      });
    } else if (fullKey.endsWith("__issues")) {
      const base = fullKey.slice(0, -"__issues".length);
      const parsed = parseMaybeJson(rawVal);
      const list = Array.isArray(parsed) ? parsed : [parsed];
      const items = list
        .filter((x) => x != null && String(x).trim() !== "")
        .map((x) => String(x).trim());
      if (items.length > 0) {
        out.push({
          key: fullKey,
          stageLabel: humanizeStageKey(base),
          kind: "issues",
          items,
        });
      }
    }
  }
  return out.sort((a, b) => a.key.localeCompare(b.key));
}

function cleanText(raw: unknown): string {
  if (typeof raw === "string") return raw.trim();
  if (raw && typeof raw === "object") {
    try {
      return JSON.stringify(raw);
    } catch {
      return "";
    }
  }
  return "";
}

/**
 * Live Pipeline Inspector disclosure on `/run/[sessionId]`.
 *
 * Collapsed by default and lazy-mounted (matching `TechnicalLog`) so the run
 * page stays quiet during execution while exposing search queries, source
 * provenance, gate revision rounds, brand history, and degradation residuals
 * on demand.
 */
export function PipelineInspector({
  sessionState,
  appName,
}: {
  sessionState: Record<string, unknown>;
  appName?: string;
}) {
  const [open, setOpen] = useState(false);

  const summaryStats = useMemo(() => {
    const webCount = extractSourceItems(sessionState.sources, false).length;
    const entCount = extractSourceItems(
      sessionState.enterprise_sources,
      true
    ).length;
    const totalSources = webCount + entCount;

    const briefRounds = toSafeNonNegInt(
      sessionState.brief_revision_rounds_used
    );
    const copyRounds = toSafeNonNegInt(
      sessionState.ad_copy_revision_rounds_used
    );
    const visualRounds = toSafeNonNegInt(
      sessionState.visual_concept_revision_rounds_used
    );
    const totalRounds = briefRounds + copyRounds + visualRounds;

    const residuals = extractResiduals(sessionState);

    return {
      webCount,
      entCount,
      totalSources,
      briefRounds,
      copyRounds,
      visualRounds,
      totalRounds,
      residuals,
    };
  }, [sessionState]);

  const detailsData = useMemo(() => {
    if (!open) return null;
    const gsQueries = extractSearchQueries(sessionState.initial_gs_queries);
    const campaignQueries = extractSearchQueries(
      sessionState.initial_campaign_queries
    );
    const enterpriseQueries = extractSearchQueries(
      sessionState.initial_enterprise_queries
    );
    const webSources = extractSourceItems(sessionState.sources, false);
    const enterpriseSources = extractSourceItems(
      sessionState.enterprise_sources,
      true
    );
    const researchEval = extractResearchEvaluation(
      sessionState.combined_research_evaluation
    );
    const brandHistory = cleanText(sessionState.brand_history);
    const refProductDesc = cleanText(
      sessionState.reference_product_description
    );
    const trendCountry =
      typeof sessionState.trend_country_code === "string"
        ? sessionState.trend_country_code.trim().toUpperCase()
        : "";
    const trendRegion =
      typeof sessionState.trend_region === "string"
        ? sessionState.trend_region.trim()
        : "";
    const showRegionalScope =
      appName === "trend_scout" || Boolean(trendCountry) || Boolean(trendRegion);

    return {
      gsQueries,
      campaignQueries,
      enterpriseQueries,
      webSources,
      enterpriseSources,
      researchEval,
      brandHistory,
      refProductDesc,
      trendCountry: trendCountry || (appName === "trend_scout" ? "US" : ""),
      trendRegion,
      showRegionalScope,
    };
  }, [open, sessionState, appName]);

  const warningCount = summaryStats.residuals.length;

  return (
    <details
      open={open}
      className="group rounded-lg border border-border bg-card"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary
        onClick={(e) => {
          e.preventDefault();
          setOpen((v) => !v);
        }}
        className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-lg px-5 py-3 [&::-webkit-details-marker]:hidden"
      >
        <span className="flex items-center gap-2 text-sm font-semibold text-foreground">
          <ChevronDown
            aria-hidden
            className="h-4 w-4 -rotate-90 group-open:rotate-0"
          />
          Pipeline inspector
        </span>

        <span className="flex flex-wrap items-center gap-2.5 text-xs text-muted-foreground tabular-nums">
          <span>
            {summaryStats.totalSources} source
            {summaryStats.totalSources !== 1 ? "s" : ""}
          </span>
          <span aria-hidden>·</span>
          <span>
            {summaryStats.totalRounds} gate revision
            {summaryStats.totalRounds !== 1 ? "s" : ""}
          </span>
          {warningCount > 0 && (
            <span className="inline-flex items-center rounded-sm border border-mark-pending/40 bg-mark-pending/10 px-2 py-0.5 text-xs font-medium text-mark-pending">
              {warningCount} warning{warningCount !== 1 ? "s" : ""}
            </span>
          )}
        </span>
      </summary>

      {open && detailsData && (
        <div className="space-y-5 border-t border-border px-5 py-4">
          {/* 1. Search Queries & Grounding */}
          <section className="space-y-3">
            <FieldLabel as="h3">Search queries &amp; grounding</FieldLabel>

            {detailsData.gsQueries.length === 0 &&
            detailsData.campaignQueries.length === 0 &&
            detailsData.enterpriseQueries.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No search queries recorded yet.
              </p>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2">
                {detailsData.gsQueries.length > 0 && (
                  <div className="rounded-md border border-border bg-background p-3 space-y-1.5">
                    <FieldLabel>
                      Google Trends web queries ({detailsData.gsQueries.length})
                    </FieldLabel>
                    <ul className="list-disc space-y-1 pl-4 text-xs text-foreground">
                      {detailsData.gsQueries.map((q, idx) => (
                        <li key={idx}>{q}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {detailsData.campaignQueries.length > 0 && (
                  <div className="rounded-md border border-border bg-background p-3 space-y-1.5">
                    <FieldLabel>
                      Campaign web queries ({detailsData.campaignQueries.length}
                      )
                    </FieldLabel>
                    <ul className="list-disc space-y-1 pl-4 text-xs text-foreground">
                      {detailsData.campaignQueries.map((q, idx) => (
                        <li key={idx}>{q}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {detailsData.enterpriseQueries.length > 0 && (
                  <div className="rounded-md border border-border bg-background p-3 space-y-1.5 sm:col-span-2">
                    <FieldLabel>
                      Enterprise knowledge queries (
                      {detailsData.enterpriseQueries.length})
                    </FieldLabel>
                    <ul className="list-disc space-y-1 pl-4 text-xs text-foreground">
                      {detailsData.enterpriseQueries.map((q, idx) => (
                        <li key={idx}>{q}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-md border border-border bg-background p-3 space-y-1.5">
                <FieldLabel>
                  Public web sources ({detailsData.webSources.length})
                </FieldLabel>
                {detailsData.webSources.length === 0 ? (
                  <p className="text-xs text-muted-foreground">None</p>
                ) : (
                  <ul className="space-y-1 text-xs text-foreground">
                    {detailsData.webSources.slice(0, 8).map((s) => (
                      <li key={s.id} className="truncate">
                        <span className="font-medium">{s.label}</span>
                        {s.domain && (
                          <span className="ml-1.5 text-muted-foreground">
                            ({s.domain})
                          </span>
                        )}
                      </li>
                    ))}
                    {detailsData.webSources.length > 8 && (
                      <li className="text-muted-foreground">
                        +{detailsData.webSources.length - 8} more web sources
                      </li>
                    )}
                  </ul>
                )}
              </div>

              <div className="rounded-md border border-border bg-background p-3 space-y-1.5">
                <FieldLabel>
                  Enterprise knowledge sources (
                  {detailsData.enterpriseSources.length})
                </FieldLabel>
                {detailsData.enterpriseSources.length === 0 ? (
                  <p className="text-xs text-muted-foreground">None</p>
                ) : (
                  <ul className="space-y-1 text-xs text-foreground">
                    {detailsData.enterpriseSources.slice(0, 8).map((s) => (
                      <li key={s.id} className="truncate">
                        <span className="font-medium">{s.label}</span>
                      </li>
                    ))}
                    {detailsData.enterpriseSources.length > 8 && (
                      <li className="text-muted-foreground">
                        +{detailsData.enterpriseSources.length - 8} more
                        enterprise documents
                      </li>
                    )}
                  </ul>
                )}
              </div>
            </div>

            {detailsData.researchEval && (
              <div className="rounded-md border border-border bg-background p-3 space-y-1">
                <FieldLabel>
                  Refinement gate ({detailsData.researchEval.findingType})
                </FieldLabel>
                {detailsData.researchEval.comment && (
                  <p className="text-xs text-foreground">
                    {detailsData.researchEval.comment}
                  </p>
                )}
                {detailsData.researchEval.followUpQueries.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    Follow-up queries:{" "}
                    {detailsData.researchEval.followUpQueries.join(" · ")}
                  </p>
                )}
              </div>
            )}
          </section>

          {/* 2. Quality Gates & Revision Rounds */}
          <section className="space-y-2 border-t border-border pt-4">
            <FieldLabel as="h3">Quality gates &amp; revision rounds</FieldLabel>
            <dl className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-md border border-border bg-background px-3 py-2">
                <FieldLabel as="dt">Brief gate</FieldLabel>
                <dd className="mt-0.5 text-sm font-medium text-foreground tabular-nums">
                  {summaryStats.briefRounds} revision
                  {summaryStats.briefRounds !== 1 ? "s" : ""}
                </dd>
              </div>
              <div className="rounded-md border border-border bg-background px-3 py-2">
                <FieldLabel as="dt">Ad copy gate</FieldLabel>
                <dd className="mt-0.5 text-sm font-medium text-foreground tabular-nums">
                  {summaryStats.copyRounds} revision
                  {summaryStats.copyRounds !== 1 ? "s" : ""}
                </dd>
              </div>
              <div className="rounded-md border border-border bg-background px-3 py-2">
                <FieldLabel as="dt">Visual concept gate</FieldLabel>
                <dd className="mt-0.5 text-sm font-medium text-foreground tabular-nums">
                  {summaryStats.visualRounds} revision
                  {summaryStats.visualRounds !== 1 ? "s" : ""}
                </dd>
              </div>
            </dl>
          </section>

          {/* 3. Context & Asset Conditioning */}
          {(detailsData.brandHistory ||
            detailsData.refProductDesc ||
            detailsData.showRegionalScope) && (
            <section className="space-y-2.5 border-t border-border pt-4">
              <FieldLabel as="h3">Context &amp; asset conditioning</FieldLabel>

              {detailsData.showRegionalScope && (
                <div className="rounded-md border border-border bg-background p-3">
                  <FieldLabel>Regional trend scope</FieldLabel>
                  <p className="mt-0.5 text-xs text-foreground">
                    Country: {detailsData.trendCountry || "US"}
                    {detailsData.trendRegion
                      ? ` · Region / metro: ${detailsData.trendRegion}`
                      : " (national)"}
                  </p>
                </div>
              )}

              {detailsData.brandHistory && (
                <div className="rounded-md border border-border bg-background p-3">
                  <FieldLabel>Cross-run brand memory</FieldLabel>
                  <p className="mt-0.5 text-xs leading-relaxed text-foreground">
                    {detailsData.brandHistory}
                  </p>
                </div>
              )}

              {detailsData.refProductDesc && (
                <div className="rounded-md border border-border bg-background p-3">
                  <FieldLabel>Reference asset descriptor</FieldLabel>
                  <p className="mt-0.5 text-xs leading-relaxed text-foreground">
                    {detailsData.refProductDesc}
                  </p>
                </div>
              )}
            </section>
          )}

          {/* 4. Degradation & Gate Residuals */}
          <section className="space-y-2 border-t border-border pt-4">
            <FieldLabel as="h3">Degradation &amp; gate residuals</FieldLabel>
            {summaryStats.residuals.length === 0 ? (
              <p className="text-xs font-medium text-mark-pass">
                All quality gates clean
              </p>
            ) : (
              <ul className="space-y-2">
                {summaryStats.residuals.map((res) => (
                  <li
                    key={res.key}
                    className="rounded-md border border-mark-pending/40 bg-mark-pending/5 px-3 py-2 text-xs"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-foreground">
                        {res.stageLabel}
                      </span>
                      <span className="rounded-xs border border-mark-pending/40 bg-mark-pending/10 px-1.5 py-0.5 text-[11px] font-medium text-mark-pending">
                        {res.kind === "retry_exhausted"
                          ? "Retry exhausted"
                          : `${res.items.length} residual issue${res.items.length !== 1 ? "s" : ""}`}
                      </span>
                    </div>
                    <ul className="mt-1 list-disc space-y-0.5 pl-4 text-muted-foreground">
                      {res.items.map((item, idx) => (
                        <li key={idx}>{item}</li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
    </details>
  );
}
