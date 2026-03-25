"use client";

import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export interface ParsedTrend {
  term: string;
  hook: string;
  context: string;
  whyItFits: string;
  strategicBridge: string;
}

/**
 * Parses the `selected_gtrends` markdown into structured trend objects.
 * Expected format:
 *   ### [trend term]
 *   * **The "Hook":** ...
 *   * **Context:** ...
 *   * **Why it fits:** ...
 *   * **The Strategic Bridge:** ...
 */
export function parseTrendsMarkdown(markdown: string): ParsedTrend[] {
  const trends: ParsedTrend[] = [];
  // Split on ### headings
  const sections = markdown.split(/^###\s+/m).filter((s) => s.trim());

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

export function TrendCards({
  trends,
  campaignState,
}: {
  trends: ParsedTrend[];
  campaignState: Record<string, unknown>;
}) {
  const router = useRouter();

  const handleClick = (trend: ParsedTrend) => {
    const params = new URLSearchParams({
      agent: "creative_agent",
      brand: (campaignState.brand as string) || "",
      targetAudience: (campaignState.target_audience as string) || "",
      targetProduct: (campaignState.target_product as string) || "",
      keySellingPoints: (campaignState.key_selling_points as string) || "",
      targetSearchTrend: trend.term,
    });
    router.push(`/?${params.toString()}`);
  };

  if (trends.length === 0) return null;

  return (
    <div className="space-y-3">
      <h3 className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
        Recommended Trends
      </h3>
      <p className="text-xs text-muted-foreground">
        Click a trend to start the Creative Agent workflow with it.
      </p>
      <div className="grid gap-3">
        {trends.map((trend, i) => (
          <div
            key={trend.term}
            className="glass rounded-xl cursor-pointer transition-all duration-200
                       hover:shadow-md hover:shadow-black/5
                       animate-fadeInUp opacity-0"
            style={{ animationDelay: `${i * 100}ms`, animationFillMode: "forwards" }}
            onClick={() => handleClick(trend)}
          >
            <div className="p-4">
              <div className="flex items-center justify-between mb-2">
                <h4 className="text-sm font-semibold text-foreground">
                  {trend.term}
                </h4>
                <Badge
                  variant="secondary"
                  className="text-[10px] bg-primary/10 text-primary border-0"
                >
                  Click to run
                </Badge>
              </div>
              {trend.hook && (
                <p className="text-xs font-medium text-primary mb-2">
                  {trend.hook}
                </p>
              )}
              <div className="space-y-1.5 text-xs text-foreground/75">
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
                className="mt-3 text-xs border-border bg-muted/50 hover:bg-muted"
                onClick={(e) => {
                  e.stopPropagation();
                  handleClick(trend);
                }}
              >
                Generate Creatives &rarr;
              </Button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
