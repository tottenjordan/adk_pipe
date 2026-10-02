"use client";

import { Suspense, useState, useEffect, useRef } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FieldLabel } from "@/components/field-label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { RunList, useRunHistory } from "@/components/run-list";
import { createSession, SELF_USER_ID } from "@/lib/api";
import { AGENTS, isAgentId, isCreativeAgent, submitLabel } from "@/lib/agents";
import { isFormValid } from "@/lib/form-validation";
import { buildInitialState } from "@/lib/initial-state";
import { takeDuplicateBrief, type Brief, type RunRow } from "@/lib/run-history";
import type { CampaignInput } from "@/lib/types";
import {
  BRAND_PRESETS,
  AUDIENCE_PRESETS,
  PRODUCT_PRESETS,
  SELLING_POINTS_PRESETS,
} from "@/lib/presets";

const RECENT_RUNS = 5;

const EMPTY_FORM: CampaignInput = {
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
};

const VISUAL_FIELDS = [
  "visualIntent",
  "brandColors",
  "visualStylePreference",
  "visualAvoid",
  "visualAspectRatio",
] as const;

function hasVisualDirection(values: Partial<CampaignInput>): boolean {
  return VISUAL_FIELDS.some((f) => Boolean(values[f]?.trim()));
}

export default function Home() {
  return (
    <Suspense>
      <HomeContent />
    </Suspense>
  );
}

/** Quiet "use example" buttons for a textarea, where a datalist can't apply. */
function ExampleButtons({
  examples,
  onPick,
}: {
  examples: readonly string[];
  onPick: (value: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
      <span className="text-muted-foreground">Use example:</span>
      {examples.map((ex) => (
        <button
          key={ex}
          type="button"
          title={ex}
          aria-label={`Use example: ${ex}`}
          onClick={() => onPick(ex)}
          className="max-w-[16rem] truncate rounded-sm text-muted-foreground underline decoration-border underline-offset-2 hover:text-foreground hover:decoration-foreground"
        >
          {ex}
        </button>
      ))}
    </div>
  );
}

function HomeContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const formRef = useRef<HTMLFormElement>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<CampaignInput>(EMPTY_FORM);
  const [visualOpen, setVisualOpen] = useState(false);
  const [duplicated, setDuplicated] = useState(false);
  const history = useRunHistory();

  const applyBrief = (brief: Brief) => {
    setForm({ ...EMPTY_FORM, ...brief });
    setVisualOpen(hasVisualDirection(brief));
    setDuplicated(true);
  };

  // Pre-fill from URL params (when clicking a trend card on a trend_scout run).
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
        ...(isAgentId(agent) && { agent }),
        ...(brand && { brand }),
        ...(targetAudience && { targetAudience }),
        ...(targetProduct && { targetProduct }),
        ...(keySellingPoints && { keySellingPoints }),
        ...(targetSearchTrend && { targetSearchTrend }),
      }));
    }
  }, [searchParams]);

  // Pre-fill from a "Duplicate brief" on the Runs page (one-shot sessionStorage
  // hand-off: the brief carries visual-direction fields too long for a URL).
  useEffect(() => {
    const brief = takeDuplicateBrief();
    if (brief) applyBrief(brief);
  }, []);

  const isCreative = isCreativeAgent(form.agent);
  const isValid = isFormValid(form);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!isValid || loading) return;
    setLoading(true);
    setError(null);

    try {
      const userId = SELF_USER_ID;
      // Seed the session's initial state: the agent (for run history), the
      // trend_scout trend-pick opt-in, or the creative agents' optional
      // visual-intent fields. See buildInitialState.
      const initialState = buildInitialState(form);
      const session = await createSession(form.agent, userId, initialState);

      // Build the user message with campaign metadata
      let message = `Brand Name: "${form.brand}"\nTarget Audience: "${form.targetAudience}"\nTarget Product: "${form.targetProduct}"\nKey Selling Points: "${form.keySellingPoints}"`;
      if (isCreative && form.targetSearchTrend) {
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
      setError(
        err instanceof Error
          ? `Couldn't start the run: ${err.message}. Check your connection and try again.`
          : "Couldn't start the run. Check your connection and try again.",
      );
    } finally {
      setLoading(false);
    }
  };

  // Cmd/Ctrl+Enter submits from anywhere in the form (including textareas).
  const handleKeyDown = (e: React.KeyboardEvent<HTMLFormElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (isValid && !loading) formRef.current?.requestSubmit();
    }
  };

  const duplicateHere = (row: RunRow) => {
    applyBrief(row.brief);
    window.scrollTo({ top: 0 });
    document.getElementById("brand")?.focus({ preventScroll: true });
  };

  const prefilledTrend = searchParams.get("targetSearchTrend");
  const recentRows = history.rows.slice(0, RECENT_RUNS);

  return (
    <div className="mx-auto grid w-full max-w-6xl gap-6 px-6 py-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
      <section
        aria-labelledby="new-run-heading"
        className="rounded-lg border border-border bg-card p-6"
      >
        <div className="mb-5 space-y-1">
          <h1 id="new-run-heading" className="text-2xl font-semibold text-foreground">
            New run
          </h1>
          <p className="text-sm text-muted-foreground">
            Describe your campaign, pick what to run, and we&apos;ll turn today&apos;s
            trends into ad creatives.
          </p>
        </div>

        {(prefilledTrend || duplicated) && (
          <p className="mb-5 rounded-md border border-border bg-muted/50 px-3 py-2 text-sm text-foreground">
            {duplicated ? (
              "Brief copied from a past run. Edit anything before you start."
            ) : (
              <>
                Pre-filled with trend <strong>{prefilledTrend}</strong> from your trend
                scout run.
              </>
            )}
          </p>
        )}

        <form
          ref={formRef}
          onSubmit={handleSubmit}
          onKeyDown={handleKeyDown}
          className="space-y-5"
        >
          <fieldset className="space-y-2">
            <FieldLabel as="legend" className="mb-2">
              What to run
            </FieldLabel>
            <div className="grid gap-2 sm:grid-cols-3">
              {AGENTS.map((a) => (
                <label
                  key={a.id}
                  className="relative flex cursor-pointer flex-col gap-1 rounded-md border border-border bg-background px-3 py-2.5 transition-colors hover:border-foreground/30 has-[:checked]:border-primary has-[:checked]:bg-card has-[:checked]:ring-1 has-[:checked]:ring-primary has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50"
                >
                  <input
                    type="radio"
                    name="agent"
                    value={a.id}
                    checked={form.agent === a.id}
                    onChange={() => setForm({ ...form, agent: a.id })}
                    className="sr-only"
                  />
                  <span className="text-sm font-semibold text-foreground">{a.label}</span>
                  <span className="text-xs leading-snug text-muted-foreground">
                    {a.description}
                  </span>
                  <span className="mt-auto flex flex-wrap gap-x-3 pt-1 text-xs text-muted-foreground">
                    <span className="tabular-nums">{a.duration}</span>
                    <span>{a.pauses}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          {form.agent === "trend_scout" && (
            <label
              htmlFor="interactive-trend-pick"
              className="flex items-start gap-3 rounded-md border border-border bg-background px-3 py-2.5 cursor-pointer hover:border-foreground/20 transition-colors"
            >
              <input
                id="interactive-trend-pick"
                type="checkbox"
                checked={form.interactiveTrendPick ?? false}
                onChange={(e) =>
                  setForm({ ...form, interactiveTrendPick: e.target.checked })
                }
                className="mt-0.5 h-4 w-4 shrink-0 rounded-sm border-border accent-primary cursor-pointer"
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

          <div className="grid gap-5 sm:grid-cols-2">
            <div className="space-y-1.5">
              <FieldLabel as="label" htmlFor="brand">
                Brand name
              </FieldLabel>
              <Input
                id="brand"
                list="brand-presets"
                autoComplete="off"
                placeholder='e.g., "Paul Reed Smith (PRS)"'
                value={form.brand}
                onChange={(e) => setForm({ ...form, brand: e.target.value })}
              />
              <datalist id="brand-presets">
                {BRAND_PRESETS.map((b) => (
                  <option key={b} value={b} />
                ))}
              </datalist>
            </div>

            <div className="space-y-1.5">
              <FieldLabel as="label" htmlFor="product">
                Target product
              </FieldLabel>
              <Input
                id="product"
                list="product-presets"
                autoComplete="off"
                placeholder='e.g., "PRS SE CE24 Electric Guitar"'
                value={form.targetProduct}
                onChange={(e) =>
                  setForm({ ...form, targetProduct: e.target.value })
                }
              />
              <datalist id="product-presets">
                {PRODUCT_PRESETS.map((p) => (
                  <option key={p} value={p} />
                ))}
              </datalist>
            </div>
          </div>

          <div className="space-y-1.5">
            <FieldLabel as="label" htmlFor="audience">
              Target audience
            </FieldLabel>
            <Textarea
              id="audience"
              placeholder="Who are they? Include psychographics, lifestyle, hobbies..."
              rows={2}
              value={form.targetAudience}
              onChange={(e) =>
                setForm({ ...form, targetAudience: e.target.value })
              }
              className="resize-y"
            />
            <ExampleButtons
              examples={AUDIENCE_PRESETS}
              onPick={(v) => setForm({ ...form, targetAudience: v })}
            />
          </div>

          <div className="space-y-1.5">
            <FieldLabel as="label" htmlFor="selling-points">
              Key selling points
            </FieldLabel>
            <Textarea
              id="selling-points"
              placeholder="What's the core benefit? Why will the audience care?"
              rows={2}
              value={form.keySellingPoints}
              onChange={(e) =>
                setForm({ ...form, keySellingPoints: e.target.value })
              }
              className="resize-y"
            />
            <ExampleButtons
              examples={SELLING_POINTS_PRESETS}
              onPick={(v) => setForm({ ...form, keySellingPoints: v })}
            />
          </div>

          {isCreative && (
            <div className="grid gap-5 sm:grid-cols-2">
              <div className="space-y-1.5">
                <FieldLabel as="label" htmlFor="trend">
                  Target search trend
                </FieldLabel>
                <Input
                  id="trend"
                  placeholder='e.g., "tswift engaged"'
                  value={form.targetSearchTrend}
                  onChange={(e) =>
                    setForm({ ...form, targetSearchTrend: e.target.value })
                  }
                />
              </div>

              <div className="space-y-1.5">
                <FieldLabel as="label" htmlFor="referenceImage">
                  Reference image URL (optional)
                </FieldLabel>
                <Input
                  id="referenceImage"
                  placeholder="gs://bucket/product.png or https://…"
                  value={form.referenceImageUri}
                  onChange={(e) =>
                    setForm({ ...form, referenceImageUri: e.target.value })
                  }
                />
                {form.referenceImageUri?.trim() && (
                  <Select
                    value={form.referenceImageRole || ""}
                    onValueChange={(v) =>
                      v && setForm({ ...form, referenceImageRole: v })
                    }
                  >
                    <SelectTrigger
                      aria-label="How to use the reference image"
                      className="w-full hover:border-foreground/30 transition-colors"
                    >
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
            </div>
          )}

          {isCreative && (
            <details
              open={visualOpen}
              onToggle={(e) => setVisualOpen(e.currentTarget.open)}
              className="group rounded-md border border-border bg-background"
            >
              <summary className="flex cursor-pointer list-none items-center gap-2 rounded-md px-3 py-2.5 text-sm font-medium text-foreground [&::-webkit-details-marker]:hidden">
                <span
                  aria-hidden
                  className="inline-block text-muted-foreground transition-transform group-open:rotate-90"
                >
                  ›
                </span>
                Visual direction (all optional)
                {!visualOpen && hasVisualDirection(form) && (
                  <span className="text-xs font-normal text-muted-foreground">(set)</span>
                )}
              </summary>

              <div className="grid gap-4 border-t border-border p-3 sm:grid-cols-2">
                <div className="space-y-1.5 sm:col-span-2">
                  <FieldLabel as="label" htmlFor="visualIntent">
                    Art direction
                  </FieldLabel>
                  <Textarea
                    id="visualIntent"
                    placeholder="e.g., moody film-noir look, dramatic lighting, close-up on the product"
                    rows={2}
                    value={form.visualIntent}
                    onChange={(e) =>
                      setForm({ ...form, visualIntent: e.target.value })
                    }
                    className="resize-y"
                  />
                </div>

                <div className="space-y-1.5">
                  <FieldLabel as="label" htmlFor="brandColors">
                    Brand colors
                  </FieldLabel>
                  <Input
                    id="brandColors"
                    placeholder="e.g., deep charcoal #1a1a1a with warm gold accents"
                    value={form.brandColors}
                    onChange={(e) =>
                      setForm({ ...form, brandColors: e.target.value })
                    }
                  />
                </div>

                <div className="space-y-1.5">
                  <FieldLabel as="label" htmlFor="visualStyle">
                    Preferred style
                  </FieldLabel>
                  <Input
                    id="visualStyle"
                    placeholder="e.g., cinematic, flat vector, 3D character, watercolor"
                    value={form.visualStylePreference}
                    onChange={(e) =>
                      setForm({ ...form, visualStylePreference: e.target.value })
                    }
                  />
                </div>

                <div className="space-y-1.5">
                  <FieldLabel as="label" htmlFor="visualAvoid">
                    Avoid
                  </FieldLabel>
                  <Input
                    id="visualAvoid"
                    placeholder="e.g., busy backgrounds, crowds"
                    value={form.visualAvoid}
                    onChange={(e) =>
                      setForm({ ...form, visualAvoid: e.target.value })
                    }
                  />
                </div>

                <div className="space-y-1.5">
                  <FieldLabel as="label" htmlFor="aspectRatio">
                    Aspect ratio
                  </FieldLabel>
                  <Select
                    value={form.visualAspectRatio || ""}
                    onValueChange={(v) =>
                      v &&
                      setForm({ ...form, visualAspectRatio: v === "auto" ? "" : v })
                    }
                  >
                    <SelectTrigger id="aspectRatio" className="w-full hover:border-foreground/30 transition-colors">
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
            </details>
          )}

          {error && (
            <div role="alert" className="rounded-md bg-destructive/5 border border-destructive/30 px-4 py-3">
              <p className="text-sm text-destructive">{error}</p>
            </div>
          )}

          <div className="flex items-center gap-4">
            <Button
              type="submit"
              size="lg"
              className="h-10 px-5 text-sm font-semibold"
              disabled={!isValid || loading}
            >
              {loading ? (
                <span className="flex items-center gap-2">
                  <span className="h-4 w-4 rounded-full border-2 border-primary-foreground/30 border-t-primary-foreground animate-spin" />
                  Starting…
                </span>
              ) : (
                submitLabel(form.agent)
              )}
            </Button>
            <span className="text-xs text-muted-foreground">
              {isValid ? (
                <>
                  or press <kbd className="font-mono">Ctrl</kbd>/<kbd className="font-mono">⌘</kbd>{" "}
                  + <kbd className="font-mono">Enter</kbd>
                </>
              ) : isCreative ? (
                "Fill in the campaign fields and a trend to start."
              ) : (
                "Fill in the campaign fields to start."
              )}
            </span>
          </div>
        </form>
      </section>

      <aside
        aria-labelledby="recent-runs-heading"
        className="rounded-lg border border-border bg-card"
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 id="recent-runs-heading" className="text-sm font-semibold text-foreground">
            Recent runs
          </h2>
          <Link href="/runs" className="text-xs text-primary underline-offset-4 hover:underline">
            See all runs
          </Link>
        </div>
        {history.loading ? (
          <p className="px-4 py-4 text-sm text-muted-foreground">Loading runs…</p>
        ) : history.error ? (
          <p className="px-4 py-4 text-sm text-muted-foreground">
            Couldn&apos;t load runs. Check your connection and reload.
          </p>
        ) : recentRows.length === 0 ? (
          <p className="px-4 py-4 text-sm text-muted-foreground">
            No runs yet. Fill in the brief to start your first one.
          </p>
        ) : (
          <RunList rows={recentRows} now={history.now} onDuplicate={duplicateHere} compact />
        )}
      </aside>
    </div>
  );
}
