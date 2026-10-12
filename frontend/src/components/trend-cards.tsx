"use client";

import { useId, useMemo } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FieldLabel } from "@/components/field-label";
import { SensitiveChip } from "@/components/sensitive-chip";
import { normalizeTerm, parseJsonObject, parseTrendFlags } from "@/lib/trend-flags";

export interface ParsedTrend {
  term: string;
  hook: string;
  context: string;
  whyItFits: string;
  strategicBridge: string;
}

/**
 * Parses the `selected_gtrends` markdown into structured trend objects.
 * Expected format (anything before the first `### ` heading is ignored):
 *   ## Selected Trends Strategy
 *   ### [trend term]
 *   * **The "Hook":** ...
 *   * **Context:** ...
 *   * **Why it fits:** ...
 *   * **The Strategic Bridge:** ...
 */
export function parseTrendsMarkdown(markdown: string): ParsedTrend[] {
  const trends: ParsedTrend[] = [];
  // Split on ### headings. Only text AFTER a `### ` heading is a trend: the
  // preamble (the picker's `## Selected Trends Strategy` header, or its
  // headerless "no trend research available" line) is dropped, never a card.
  const sections = markdown
    .split(/^###\s+/m)
    .slice(1)
    .filter((s) => s.trim());

  for (const section of sections) {
    const lines = section.trim().split("\n");
    const term = lines[0]?.trim().replace(/^#+\s*/, "") || "";
    if (!term) continue;

    const content = lines.slice(1).join("\n");

    const extractField = (label: string): string => {
      const regex = new RegExp(
        `\\*\\*${label}:?\\*\\*:?\\s*(.+?)(?=\\n\\*\\s*\\*\\*|$)`,
        "s"
      );
      const match = content.match(regex);
      return match?.[1]?.trim() || "";
    };

    trends.push({
      term,
      hook: extractField('The "Hook"') || extractField("The Hook"),
      context: extractField("Context"),
      whyItFits: extractField("Why it fits"),
      strategicBridge: extractField("The Strategic Bridge"),
    });
  }

  return trends;
}

// Mirrors trend_scout/trend_context.py (the BigQuery `trend_context` value).
const MAX_FIELD_CHARS = 400;
/** A single-line, capped string ("" for non-strings). */
function clean(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.split(/\s+/).filter(Boolean).join(" ").slice(0, MAX_FIELD_CHARS).trimEnd();
}

/** The `analyzed_trends` entry for `term` in `info_gtrends` (JSON string, fenced JSON or object), or {}. */
function researchItem(term: string, infoGtrends: unknown): Record<string, unknown> {
  const key = normalizeTerm(term);
  const items = parseJsonObject(infoGtrends)?.analyzed_trends;
  if (!key || !Array.isArray(items)) return {};
  for (const item of items) {
    if (item && typeof item === "object" && normalizeTerm((item as { term?: unknown }).term) === key) {
      return item as Record<string, unknown>;
    }
  }
  return {};
}

/**
 * trend_scout's research for one card as compact JSON — the same shape (and
 * precedence) trend_scout persists to BigQuery `trend_context` for batch runs:
 * `{context, cultural_angle, event_hint, hook}`, blank fields dropped, "" when
 * all are blank. `cultural_angle` / `event_hint` (and the research `context`)
 * come from the scout run's `info_gtrends` entry whose `term` matches the card;
 * without one only the card's context + hook are sent. A term the scout run's
 * `trend_flags` marks sensitive also gets `sensitive: true` + `sensitive_reason`
 * (appended, as in the backend; absent otherwise). Seeded as
 * `trend_scout_context`, which the creative agents read only as a hint to
 * re-verify.
 */
export function trendContextJson(
  trend: ParsedTrend,
  infoGtrends?: unknown,
  trendFlags?: unknown,
): string {
  const item = researchItem(trend.term, infoGtrends);
  const fields: Record<string, string> = {
    context: clean(item.context) || clean(trend.context),
    cultural_angle: clean(item.cultural_angle),
    event_hint: clean(item.event_hint),
    hook: clean(trend.hook),
  };
  const compact: Record<string, unknown> = Object.fromEntries(
    Object.entries(fields).filter(([, v]) => v),
  );
  const reason = parseTrendFlags(trendFlags).get(normalizeTerm(trend.term));
  if (reason !== undefined) {
    compact.sensitive = true;
    compact.sensitive_reason = clean(reason);
  }
  return Object.keys(compact).length ? JSON.stringify(compact) : "";
}

/**
 * The home-form URL params that start a creative run from a trend card: the
 * scout run's campaign fields, the trend, its context (enriched from the scout
 * run's `info_gtrends`) and the date the scout run found it (`current_date`,
 * seeded per run; omitted for older runs). The batch (CRF) path seeds
 * `trend_detected_on` from the row's `refresh_date` (the Google Trends refresh)
 * instead.
 */
export function trendRunParams(
  trend: ParsedTrend,
  campaignState: Record<string, unknown>,
): URLSearchParams {
  const params = new URLSearchParams({
    agent: "creative_agent",
    brand: (campaignState.brand as string) || "",
    targetAudience: (campaignState.target_audience as string) || "",
    targetProduct: (campaignState.target_product as string) || "",
    keySellingPoints: (campaignState.key_selling_points as string) || "",
    targetSearchTrend: trend.term,
  });
  const context = trendContextJson(
    trend,
    campaignState.info_gtrends,
    campaignState.trend_flags,
  );
  if (context) params.set("trendContext", context);
  const detectedOn = campaignState.current_date;
  if (typeof detectedOn === "string" && detectedOn.trim()) {
    params.set("trendDetectedOn", detectedOn.trim());
  }
  return params;
}

export function TrendCards({
  trends,
  campaignState,
}: {
  trends: ParsedTrend[];
  campaignState: Record<string, unknown>;
}) {
  const router = useRouter();
  const idBase = useId();
  const flags = useMemo(
    () => parseTrendFlags(campaignState.trend_flags),
    [campaignState.trend_flags],
  );

  const handleClick = (trend: ParsedTrend) => {
    router.push(`/?${trendRunParams(trend, campaignState).toString()}`);
  };

  if (trends.length === 0) return null;

  return (
    <div className="space-y-3">
      <FieldLabel as="h3">Recommended trends</FieldLabel>
      <p className="text-xs text-muted-foreground">
        Click a trend to start a creative run with it.
      </p>
      <div className="grid gap-3">
        {trends.map((trend, i) => {
          const reason = flags.get(normalizeTerm(trend.term));
          const reasonId = `${idBase}-sensitive-${i}`;
          return (
            <div
              key={trend.term}
              className="rounded-lg border border-border bg-card cursor-pointer transition-colors hover:border-primary/40"
              onClick={() => handleClick(trend)}
            >
              <div className="p-4">
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-sm font-semibold text-foreground">
                    {trend.term}
                  </h4>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {reason !== undefined && (
                      <>
                        <SensitiveChip reason={reason} />
                        <span id={reasonId} className="sr-only">
                          {reason ? `Sensitive: ${reason}` : "Sensitive trend"}
                        </span>
                      </>
                    )}
                    <Badge variant="secondary" className="text-xs">
                      Click to run
                    </Badge>
                  </div>
                </div>
                {trend.hook && (
                  <p className="text-xs font-medium text-foreground mb-2">
                    {trend.hook}
                  </p>
                )}
                <div className="space-y-1.5 text-xs text-foreground/85">
                  {trend.context && (
                    <div>
                      <span className="font-medium text-muted-foreground">
                        Context:{" "}
                      </span>
                      {trend.context}
                    </div>
                  )}
                  {trend.whyItFits && (
                    <div>
                      <span className="font-medium text-muted-foreground">
                        Why it fits:{" "}
                      </span>
                      {trend.whyItFits}
                    </div>
                  )}
                  {trend.strategicBridge && (
                    <div>
                      <span className="font-medium text-muted-foreground">
                        Strategic bridge:{" "}
                      </span>
                      {trend.strategicBridge}
                    </div>
                  )}
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-3 text-xs"
                  aria-describedby={reason !== undefined ? reasonId : undefined}
                  onClick={(e) => {
                    e.stopPropagation();
                    handleClick(trend);
                  }}
                >
                  Generate creatives &rarr;
                </Button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
