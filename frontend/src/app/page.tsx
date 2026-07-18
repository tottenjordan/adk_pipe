"use client";

import { Suspense, useState, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
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
import { buildInitialState } from "@/lib/initial-state";
import type { CampaignInput } from "@/lib/types";
import {
  BRAND_PRESETS,
  AUDIENCE_PRESETS,
  PRODUCT_PRESETS,
  SELLING_POINTS_PRESETS,
} from "@/lib/presets";

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
    agent: "trend_scout",
    brand: "",
    targetAudience: "",
    targetProduct: "",
    keySellingPoints: "",
    targetSearchTrend: "",
    interactiveTrendPick: false,
    referenceImageUri: "",
    referenceImageRole: "",
    visualIntent: "",
    brandColors: "",
    visualStylePreference: "",
    visualAvoid: "",
    visualAspectRatio: "",
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
      // Seed the session's initial state: trend_scout's interactive-trend-pick
      // opt-in, or the creative agents' optional visual-intent fields. See
      // buildInitialState (snake_case keys match creative_agent/callbacks.py).
      const initialState = buildInitialState(form);
      const session = await createSession(form.agent, userId, initialState);

      // Build the user message with campaign metadata
      let message = `Brand Name: "${form.brand}"\nTarget Audience: "${form.targetAudience}"\nTarget Product: "${form.targetProduct}"\nKey Selling Points: "${form.keySellingPoints}"`;
      if ((form.agent === "creative_agent" || form.agent === "interactive_creative") && form.targetSearchTrend) {
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
    (form.agent === "trend_scout" || form.targetSearchTrend);

  const isPreFilled = searchParams.get("targetSearchTrend");

  return (
    <div className="flex-1 flex flex-col items-center justify-center px-4 py-16">
      <div
        className="w-full max-w-2xl glass rounded-2xl p-8
                    shadow-xl shadow-black/5
                    transition-all duration-300 hover:shadow-2xl hover:shadow-black/8
                    animate-fadeInUp"
      >
        <div className="text-center space-y-3 mb-8">
          <h1 className="text-3xl font-bold tracking-tight text-foreground">
            New Campaign Run
          </h1>
          <p className="text-muted-foreground max-w-md mx-auto">
            Enter your campaign metadata to generate trend-informed ad
            creatives.
          </p>
        </div>

        {isPreFilled && (
          <div className="mb-6 flex items-center gap-3 rounded-xl bg-primary/8 border border-primary/15 px-4 py-3">
            <Badge
              variant="secondary"
              className="bg-primary/15 text-primary border-0"
            >
              From Trend Trawler
            </Badge>
            <p className="text-sm text-foreground">
              Pre-filled with trend:{" "}
              <strong>{searchParams.get("targetSearchTrend")}</strong>
            </p>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-5">
          <div className="space-y-2">
            <Label htmlFor="agent" className="text-muted-foreground text-xs uppercase tracking-wider">
              Agent
            </Label>
            <Select
              value={form.agent}
              onValueChange={(v) =>
                v && setForm({ ...form, agent: v as CampaignInput["agent"] })
              }
            >
              <SelectTrigger id="agent" className="w-full min-w-[400px] bg-background border-border hover:border-foreground/20 transition-colors">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="trend_scout">
                  Trend Scout &mdash; Discover relevant trends
                </SelectItem>
                <SelectItem value="creative_agent">
                  Creative Agent &mdash; Generate ad creatives
                </SelectItem>
                <SelectItem value="interactive_creative">
                  Interactive Creative &mdash; Generate with review checkpoints
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          {form.agent === "trend_scout" && (
            <label
              htmlFor="interactive-trend-pick"
              className="flex items-start gap-3 rounded-xl border border-border bg-background px-4 py-3 cursor-pointer hover:border-foreground/20 transition-colors animate-fadeInUpSmooth"
            >
              <input
                id="interactive-trend-pick"
                type="checkbox"
                checked={form.interactiveTrendPick ?? false}
                onChange={(e) =>
                  setForm({ ...form, interactiveTrendPick: e.target.checked })
                }
                className="mt-0.5 h-4 w-4 shrink-0 rounded border-border accent-primary cursor-pointer"
              />
              <span className="space-y-0.5">
                <span className="block text-sm font-medium text-foreground">
                  Let me pick the trends myself
                </span>
                <span className="block text-xs text-muted-foreground">
                  Pause after gathering the top ~25 trends so you can choose which
                  to keep.
                </span>
              </span>
            </label>
          )}

          <div className="space-y-2">
            <Label htmlFor="brand" className="text-muted-foreground text-xs uppercase tracking-wider">
              Brand Name
            </Label>
            <Select
              value=""
              onValueChange={(v) => v && setForm({ ...form, brand: v })}
            >
              <SelectTrigger className="w-full bg-background border-border hover:border-foreground/20 transition-colors text-muted-foreground">
                <SelectValue placeholder="Select a preset..." />
              </SelectTrigger>
              <SelectContent>
                {BRAND_PRESETS.map((b) => (
                  <SelectItem key={b} value={b}>{b}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input
              id="brand"
              placeholder='e.g., "Paul Reed Smith (PRS)"'
              value={form.brand}
              onChange={(e) => setForm({ ...form, brand: e.target.value })}
              className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="audience" className="text-muted-foreground text-xs uppercase tracking-wider">
              Target Audience
            </Label>
            <Select
              value=""
              onValueChange={(v) => v && setForm({ ...form, targetAudience: v })}
            >
              <SelectTrigger className="w-full bg-background border-border hover:border-foreground/20 transition-colors text-muted-foreground">
                <SelectValue placeholder="Select a preset..." />
              </SelectTrigger>
              <SelectContent>
                {AUDIENCE_PRESETS.map((a) => (
                  <SelectItem key={a} value={a}>
                    {a.length > 80 ? `${a.slice(0, 80)}...` : a}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Textarea
              id="audience"
              placeholder="Who are they? Include psychographics, lifestyle, hobbies..."
              rows={3}
              value={form.targetAudience}
              onChange={(e) =>
                setForm({ ...form, targetAudience: e.target.value })
              }
              className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors resize-none"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="product" className="text-muted-foreground text-xs uppercase tracking-wider">
              Target Product
            </Label>
            <Select
              value=""
              onValueChange={(v) => v && setForm({ ...form, targetProduct: v })}
            >
              <SelectTrigger className="w-full bg-background border-border hover:border-foreground/20 transition-colors text-muted-foreground">
                <SelectValue placeholder="Select a preset..." />
              </SelectTrigger>
              <SelectContent>
                {PRODUCT_PRESETS.map((p) => (
                  <SelectItem key={p} value={p}>{p}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input
              id="product"
              placeholder='e.g., "PRS SE CE24 Electric Guitar"'
              value={form.targetProduct}
              onChange={(e) =>
                setForm({ ...form, targetProduct: e.target.value })
              }
              className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="selling-points" className="text-muted-foreground text-xs uppercase tracking-wider">
              Key Selling Points
            </Label>
            <Select
              value=""
              onValueChange={(v) => v && setForm({ ...form, keySellingPoints: v })}
            >
              <SelectTrigger className="w-full bg-background border-border hover:border-foreground/20 transition-colors text-muted-foreground">
                <SelectValue placeholder="Select a preset..." />
              </SelectTrigger>
              <SelectContent>
                {SELLING_POINTS_PRESETS.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s.length > 80 ? `${s.slice(0, 80)}...` : s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Textarea
              id="selling-points"
              placeholder="What's the core benefit? Why will the audience care?"
              rows={3}
              value={form.keySellingPoints}
              onChange={(e) =>
                setForm({ ...form, keySellingPoints: e.target.value })
              }
              className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors resize-none"
            />
          </div>

          {(form.agent === "creative_agent" || form.agent === "interactive_creative") && (
            <div className="space-y-2 animate-fadeInUpSmooth">
              <Label htmlFor="trend" className="text-muted-foreground text-xs uppercase tracking-wider">
                Target Search Trend
              </Label>
              <Input
                id="trend"
                placeholder='e.g., "tswift engaged"'
                value={form.targetSearchTrend}
                onChange={(e) =>
                  setForm({ ...form, targetSearchTrend: e.target.value })
                }
                className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors"
              />
            </div>
          )}

          {(form.agent === "creative_agent" || form.agent === "interactive_creative") && (
            <div className="space-y-2 animate-fadeInUpSmooth">
              <Label htmlFor="referenceImage" className="text-muted-foreground text-xs uppercase tracking-wider">
                Reference Image URL (optional)
              </Label>
              <Input
                id="referenceImage"
                placeholder='gs://bucket/product.png or https://…'
                value={form.referenceImageUri}
                onChange={(e) =>
                  setForm({ ...form, referenceImageUri: e.target.value })
                }
                className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors"
              />
              {form.referenceImageUri?.trim() && (
                <Select
                  value={form.referenceImageRole || ""}
                  onValueChange={(v) =>
                    v && setForm({ ...form, referenceImageRole: v })
                  }
                >
                  <SelectTrigger className="w-full bg-background border-border hover:border-foreground/20 transition-colors">
                    <SelectValue placeholder="How to use the reference image..." />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="product">Product — put this product in the image</SelectItem>
                    <SelectItem value="logo">Logo — include this brand logo</SelectItem>
                    <SelectItem value="style">Style — match this look/aesthetic</SelectItem>
                  </SelectContent>
                </Select>
              )}
            </div>
          )}

          {(form.agent === "creative_agent" || form.agent === "interactive_creative") && (
            <div className="space-y-4 animate-fadeInUpSmooth rounded-xl border border-border bg-background/50 p-4">
              <p className="text-xs uppercase tracking-wider text-muted-foreground">
                Visual direction (all optional)
              </p>

              <div className="space-y-2">
                <Label htmlFor="visualIntent" className="text-muted-foreground text-xs uppercase tracking-wider">
                  Art direction
                </Label>
                <Textarea
                  id="visualIntent"
                  placeholder="e.g., moody film-noir look, dramatic lighting, close-up on the product"
                  rows={2}
                  value={form.visualIntent}
                  onChange={(e) =>
                    setForm({ ...form, visualIntent: e.target.value })
                  }
                  className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors resize-none"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="brandColors" className="text-muted-foreground text-xs uppercase tracking-wider">
                  Brand colors
                </Label>
                <Input
                  id="brandColors"
                  placeholder="e.g., deep charcoal #1a1a1a with warm gold accents"
                  value={form.brandColors}
                  onChange={(e) =>
                    setForm({ ...form, brandColors: e.target.value })
                  }
                  className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="visualStyle" className="text-muted-foreground text-xs uppercase tracking-wider">
                  Preferred style
                </Label>
                <Input
                  id="visualStyle"
                  placeholder="e.g., cinematic, flat vector cartoon, 3D character, anime, watercolor"
                  value={form.visualStylePreference}
                  onChange={(e) =>
                    setForm({ ...form, visualStylePreference: e.target.value })
                  }
                  className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="visualAvoid" className="text-muted-foreground text-xs uppercase tracking-wider">
                  Avoid
                </Label>
                <Input
                  id="visualAvoid"
                  placeholder="e.g., busy backgrounds, crowds"
                  value={form.visualAvoid}
                  onChange={(e) =>
                    setForm({ ...form, visualAvoid: e.target.value })
                  }
                  className="bg-background border-border hover:border-foreground/20 focus:border-primary/50 transition-colors"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="aspectRatio" className="text-muted-foreground text-xs uppercase tracking-wider">
                  Aspect ratio
                </Label>
                <Select
                  value={form.visualAspectRatio || ""}
                  onValueChange={(v) =>
                    v &&
                    setForm({ ...form, visualAspectRatio: v === "auto" ? "" : v })
                  }
                >
                  <SelectTrigger id="aspectRatio" className="w-full bg-background border-border hover:border-foreground/20 transition-colors">
                    <SelectValue placeholder="Auto (let AI choose)" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="auto">Auto (let AI choose)</SelectItem>
                    <SelectItem value="9:16">9:16 — vertical reel / story</SelectItem>
                    <SelectItem value="1:1">1:1 — square feed</SelectItem>
                    <SelectItem value="4:5">4:5 — portrait feed</SelectItem>
                    <SelectItem value="3:4">3:4 — portrait</SelectItem>
                    <SelectItem value="16:9">16:9 — landscape</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          )}

          {error && (
            <div className="rounded-lg bg-destructive/8 border border-destructive/15 px-4 py-3">
              <p className="text-sm text-destructive">{error}</p>
            </div>
          )}

          <Button
            type="submit"
            className="w-full h-11 text-sm font-semibold tracking-wide"
            disabled={!isValid || loading}
          >
            {loading ? (
              <span className="flex items-center gap-2">
                <span className="h-4 w-4 rounded-full border-2 border-primary-foreground/30 border-t-primary-foreground animate-spin" />
                Starting...
              </span>
            ) : (
              "Run Agent"
            )}
          </Button>
        </form>
      </div>
    </div>
  );
}
