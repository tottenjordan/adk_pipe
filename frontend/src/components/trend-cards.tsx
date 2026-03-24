"use client";

import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
      <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">
        Recommended Trends
      </h3>
      <p className="text-xs text-muted-foreground">
        Click a trend to start the Creative Agent workflow with it.
      </p>
      <div className="grid gap-3">
        {trends.map((trend) => (
          <Card
            key={trend.term}
            className="cursor-pointer transition-all hover:border-primary hover:shadow-md"
            onClick={() => handleClick(trend)}
          >
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">{trend.term}</CardTitle>
                <Badge variant="secondary" className="text-xs">
                  Click to run
                </Badge>
              </div>
              {trend.hook && (
                <p className="text-sm font-medium text-primary">
                  {trend.hook}
                </p>
              )}
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
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
              <Button
                size="sm"
                variant="outline"
                className="mt-2"
                onClick={(e) => {
                  e.stopPropagation();
                  handleClick(trend);
                }}
              >
                Generate Creatives &rarr;
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
