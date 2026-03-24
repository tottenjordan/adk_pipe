"use client";

import { Suspense, useState, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { createSession } from "@/lib/api";
import type { CampaignInput } from "@/lib/types";

export default function Home() {
  return (
    <Suspense>
      <HomeContent />
    </Suspense>
  );
}

function HomeContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<CampaignInput>({
    agent: "trend_trawler",
    brand: "",
    targetAudience: "",
    targetProduct: "",
    keySellingPoints: "",
    targetSearchTrend: "",
  });

  // Pre-fill form from URL params (when clicking a trend card)
  useEffect(() => {
    const agent = searchParams.get("agent");
    const brand = searchParams.get("brand");
    const targetAudience = searchParams.get("targetAudience");
    const targetProduct = searchParams.get("targetProduct");
    const keySellingPoints = searchParams.get("keySellingPoints");
    const targetSearchTrend = searchParams.get("targetSearchTrend");

    if (agent || brand || targetSearchTrend) {
      setForm((prev) => ({
        ...prev,
        ...(agent && { agent: agent as CampaignInput["agent"] }),
        ...(brand && { brand }),
        ...(targetAudience && { targetAudience }),
        ...(targetProduct && { targetProduct }),
        ...(keySellingPoints && { keySellingPoints }),
        ...(targetSearchTrend && { targetSearchTrend }),
      }));
    }
  }, [searchParams]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const userId = `user_${Date.now()}`;
      const session = await createSession(form.agent, userId);

      // Build the user message with campaign metadata
      let message = `Brand Name: "${form.brand}"\nTarget Audience: "${form.targetAudience}"\nTarget Product: "${form.targetProduct}"\nKey Selling Points: "${form.keySellingPoints}"`;
      if (form.agent === "creative_agent" && form.targetSearchTrend) {
        message += `\ntarget_search_trend: "${form.targetSearchTrend}"`;
      }

      // Store message in sessionStorage to avoid URL length limits
      sessionStorage.setItem(`run:${session.id}`, JSON.stringify({ message }));

      const params = new URLSearchParams({
        app: form.agent,
        userId,
      });
      router.push(`/run/${session.id}?${params.toString()}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start agent");
    } finally {
      setLoading(false);
    }
  };

  const isValid =
    form.brand &&
    form.targetAudience &&
    form.targetProduct &&
    form.keySellingPoints &&
    (form.agent !== "creative_agent" || form.targetSearchTrend);

  const isPreFilled = searchParams.get("targetSearchTrend");

  return (
    <div className="mx-auto max-w-3xl px-6 py-12">
      <div className="mb-8">
        <h1 className="text-3xl font-bold tracking-tight">New Campaign Run</h1>
        <p className="mt-2 text-muted-foreground">
          Enter your campaign metadata to generate trend-informed ad creatives.
        </p>
      </div>

      {isPreFilled && (
        <Card className="mb-6 border-primary/30 bg-primary/5">
          <CardContent className="flex items-center gap-3 py-3">
            <Badge variant="secondary">From Trend Trawler</Badge>
            <p className="text-sm">
              Pre-filled with trend:{" "}
              <strong>{searchParams.get("targetSearchTrend")}</strong>
            </p>
          </CardContent>
        </Card>
      )}

      <form onSubmit={handleSubmit}>
        <Card>
          <CardHeader>
            <CardTitle>Campaign Details</CardTitle>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="agent">Agent</Label>
              <Select
                value={form.agent}
                onValueChange={(v) =>
                  setForm({ ...form, agent: v as CampaignInput["agent"] })
                }
              >
                <SelectTrigger id="agent" className="w-full min-w-[400px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="trend_trawler">
                    Trend Trawler &mdash; Discover relevant trends
                  </SelectItem>
                  <SelectItem value="creative_agent">
                    Creative Agent &mdash; Generate ad creatives
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="brand">Brand Name</Label>
              <Input
                id="brand"
                placeholder='e.g., "Paul Reed Smith (PRS)"'
                value={form.brand}
                onChange={(e) => setForm({ ...form, brand: e.target.value })}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="audience">Target Audience</Label>
              <Textarea
                id="audience"
                placeholder="Who are they? Include psychographics, lifestyle, hobbies..."
                rows={3}
                value={form.targetAudience}
                onChange={(e) =>
                  setForm({ ...form, targetAudience: e.target.value })
                }
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="product">Target Product</Label>
              <Input
                id="product"
                placeholder='e.g., "PRS SE CE24 Electric Guitar"'
                value={form.targetProduct}
                onChange={(e) =>
                  setForm({ ...form, targetProduct: e.target.value })
                }
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="selling-points">Key Selling Points</Label>
              <Textarea
                id="selling-points"
                placeholder="What's the core benefit? Why will the audience care?"
                rows={3}
                value={form.keySellingPoints}
                onChange={(e) =>
                  setForm({ ...form, keySellingPoints: e.target.value })
                }
              />
            </div>

            {form.agent === "creative_agent" && (
              <div className="space-y-2">
                <Label htmlFor="trend">Target Search Trend</Label>
                <Input
                  id="trend"
                  placeholder='e.g., "tswift engaged"'
                  value={form.targetSearchTrend}
                  onChange={(e) =>
                    setForm({ ...form, targetSearchTrend: e.target.value })
                  }
                />
              </div>
            )}

            {error && (
              <p className="text-sm text-destructive">{error}</p>
            )}

            <Button
              type="submit"
              className="w-full"
              disabled={!isValid || loading}
            >
              {loading ? "Starting..." : "Run Agent"}
            </Button>
          </CardContent>
        </Card>
      </form>
    </div>
  );
}
