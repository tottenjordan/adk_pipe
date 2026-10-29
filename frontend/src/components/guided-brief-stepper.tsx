"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import { FieldLabel } from "@/components/field-label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { LearnFromRatingsToggle } from "@/components/learn-from-ratings-toggle";
import { PersonSelect } from "@/components/person-select";
import { ExampleButtons } from "@/components/new-run/campaign-fields";
import { ReferenceImagesFieldset } from "@/components/new-run/reference-images-fieldset";
import {
  FieldError,
  requiredProps,
  type FieldErrors,
  type RequiredField,
} from "@/components/new-run/required-fields";
import { AGENTS, isCreativeAgent, submitLabel } from "@/lib/agents";
import { invalidReferenceUris } from "@/lib/reference-images";
import {
  AUDIENCE_PRESETS,
  BRAND_PRESETS,
  PRODUCT_PRESETS,
  SELLING_POINTS_PRESETS,
} from "@/lib/presets";
import type { BrandAssistSuggestion, CampaignInput } from "@/lib/types";
import { cn } from "@/lib/utils";

export const TREND_COUNTRIES = [
  { code: "US", label: "United States (US)" },
  { code: "GB", label: "United Kingdom (GB)" },
  { code: "CA", label: "Canada (CA)" },
  { code: "AU", label: "Australia (AU)" },
  { code: "DE", label: "Germany (DE)" },
  { code: "FR", label: "France (FR)" },
  { code: "JP", label: "Japan (JP)" },
  { code: "BR", label: "Brazil (BR)" },
  { code: "IN", label: "India (IN)" },
  { code: "MX", label: "Mexico (MX)" },
  { code: "KR", label: "South Korea (KR)" },
  { code: "ES", label: "Spain (ES)" },
  { code: "IT", label: "Italy (IT)" },
  { code: "NL", label: "Netherlands (NL)" },
  { code: "SG", label: "Singapore (SG)" },
] as const;

const HEX_COLOR_RE = /^#[0-9A-Fa-f]{6}$/;

const PALETTE_WEIGHTS: Record<number, readonly number[]> = {
  1: [100],
  2: [70, 30],
  3: [60, 30, 10],
};

/** Format up to 3 normalized `#RRGGBB` colors into a 60-30-10 `brandColors` string. */
export function formatPaletteForBrief(hexColors: readonly string[]): string {
  const valid = hexColors
    .map((h) => h.trim().toUpperCase())
    .filter((h) => HEX_COLOR_RE.test(h))
    .slice(0, 3);
  if (valid.length === 0) return "";
  const weights = PALETTE_WEIGHTS[valid.length] ?? [60, 30, 10];
  return valid.map((hex, idx) => `${weights[idx] ?? 10}% ${hex}`).join(", ");
}

/** Country + Region/Metro scope controls for `trend_scout`. */
export function TrendMarketScopeFields({
  form,
  onChange,
}: {
  form: CampaignInput;
  onChange: (next: CampaignInput) => void;
}) {
  const country = form.trendCountryCode || "US";
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <div className="space-y-1.5">
        <FieldLabel as="label" htmlFor="trend-country">
          Country
        </FieldLabel>
        <select
          id="trend-country"
          value={country}
          onChange={(e) =>
            onChange({ ...form, trendCountryCode: e.target.value })
          }
          className="h-8 w-full min-w-0 rounded-sm border border-input bg-card px-2.5 py-1 text-sm text-foreground transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 outline-hidden"
        >
          {TREND_COUNTRIES.map((c) => (
            <option key={c.code} value={c.code}>
              {c.label}
            </option>
          ))}
        </select>
      </div>

      <div className="space-y-1.5">
        <FieldLabel as="label" htmlFor="trend-region">
          Region or metro (optional)
        </FieldLabel>
        <Input
          id="trend-region"
          maxLength={80}
          placeholder={
            country === "US"
              ? 'e.g., "New York" or "Los Angeles"'
              : 'e.g., "England" or "Tokyo"'
          }
          value={form.trendRegion ?? ""}
          onChange={(e) => onChange({ ...form, trendRegion: e.target.value })}
        />
      </div>
    </div>
  );
}

export interface BrandAssistPanelProps {
  form: CampaignInput;
  onChange: (next: CampaignInput) => void;
  domainHint: string;
  onDomainHintChange: (next: string) => void;
  showDomainInput?: boolean;
  suggestion: BrandAssistSuggestion | null;
  loading: boolean;
  error: string | null;
  onFetch: () => void;
}

/**
 * Grounded Brand Assist trigger and non-destructive suggestion preview panel.
 * Renders product chips, audience/selling-points apply controls, 60-30-10 hex
 * palette swatches, and visual brand cue chips.
 */
export function BrandAssistPanel({
  form,
  onChange,
  domainHint,
  onDomainHintChange,
  showDomainInput = false,
  suggestion,
  loading,
  error,
  onFetch,
}: BrandAssistPanelProps) {
  const canRequest = Boolean(form.brand.trim()) && !loading;
  const validPaletteHex = suggestion
    ? suggestion.brand_palette_hex
        .map((h) => h.trim().toUpperCase())
        .filter((h) => HEX_COLOR_RE.test(h))
        .slice(0, 3)
    : [];

  const appendVisualCue = (cue: string) => {
    const current = (form.visualIntent ?? "").trim();
    if (!current) {
      onChange({ ...form, visualIntent: cue });
      return;
    }
    if (current.toLowerCase().includes(cue.toLowerCase())) return;
    const sep = /[.!?;,]$/.test(current) ? " " : "; ";
    onChange({ ...form, visualIntent: `${current}${sep}${cue}` });
  };

  return (
    <div className="space-y-3">
      <div
        className={cn(
          "flex flex-wrap items-end gap-3",
          showDomainInput && "sm:grid sm:grid-cols-[minmax(0,1fr)_auto]"
        )}
      >
        {showDomainInput && (
          <div className="w-full space-y-1.5">
            <FieldLabel as="label" htmlFor="domain-hint">
              Brand domain hint (optional)
            </FieldLabel>
            <Input
              id="domain-hint"
              autoComplete="off"
              placeholder="e.g., prsguitars.com"
              value={domainHint}
              onChange={(e) => onDomainHintChange(e.target.value)}
            />
          </div>
        )}
        <div className="flex items-center gap-2.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!canRequest}
            onClick={onFetch}
          >
            {loading ? (
              <span className="flex items-center gap-2">
                <span className="h-3.5 w-3.5 rounded-full border-2 border-foreground/30 border-t-foreground animate-spin" />
                Researching brand…
              </span>
            ) : (
              "Auto-fill with Brand Assist"
            )}
          </Button>
          {!showDomainInput && (
            <span className="text-xs text-muted-foreground">
              Grounded product, audience &amp; palette suggestions via Google
              Search
            </span>
          )}
        </div>
      </div>

      {error && (
        <p role="status" className="text-xs text-mark-fail">
          {error}
        </p>
      )}

      {suggestion && (
        <div
          aria-label="Brand Assist suggestions"
          className="space-y-3 rounded-md border border-border bg-muted/30 p-3.5"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <FieldLabel>Brand Assist suggestions (click to apply)</FieldLabel>
            {(suggestion.target_audience || suggestion.key_selling_points) && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() =>
                  onChange({
                    ...form,
                    ...(suggestion.target_audience && {
                      targetAudience: suggestion.target_audience,
                    }),
                    ...(suggestion.key_selling_points && {
                      keySellingPoints: suggestion.key_selling_points,
                    }),
                  })
                }
              >
                Apply audience &amp; selling points
              </Button>
            )}
          </div>

          {suggestion.suggested_products.length > 0 && (
            <div className="space-y-1.5">
              <FieldLabel>Suggested products</FieldLabel>
              <div className="flex flex-wrap gap-1.5">
                {suggestion.suggested_products.map((prod) => {
                  const selected = form.targetProduct.trim() === prod;
                  return (
                    <button
                      key={prod}
                      type="button"
                      onClick={() =>
                        onChange({ ...form, targetProduct: prod })
                      }
                      className={cn(
                        "rounded-sm border px-2.5 py-1 text-xs font-medium transition-colors",
                        selected
                          ? "border-primary bg-primary/10 text-primary"
                          : "border-border bg-card text-foreground hover:border-foreground/40"
                      )}
                    >
                      {prod}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {(suggestion.target_audience || suggestion.key_selling_points) && (
            <div className="grid gap-3 sm:grid-cols-2">
              {suggestion.target_audience && (
                <div className="space-y-1 rounded-sm border border-border bg-card p-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <FieldLabel>Audience</FieldLabel>
                    <button
                      type="button"
                      onClick={() =>
                        onChange({
                          ...form,
                          targetAudience: suggestion.target_audience,
                        })
                      }
                      className="text-xs font-medium text-primary underline-offset-4 hover:underline"
                    >
                      Apply audience
                    </button>
                  </div>
                  <p className="text-xs leading-relaxed text-foreground">
                    {suggestion.target_audience}
                  </p>
                </div>
              )}

              {suggestion.key_selling_points && (
                <div className="space-y-1 rounded-sm border border-border bg-card p-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <FieldLabel>Selling points</FieldLabel>
                    <button
                      type="button"
                      onClick={() =>
                        onChange({
                          ...form,
                          keySellingPoints: suggestion.key_selling_points,
                        })
                      }
                      className="text-xs font-medium text-primary underline-offset-4 hover:underline"
                    >
                      Apply selling points
                    </button>
                  </div>
                  <p className="text-xs leading-relaxed text-foreground">
                    {suggestion.key_selling_points}
                  </p>
                </div>
              )}
            </div>
          )}

          {validPaletteHex.length > 0 && (
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <FieldLabel>Brand palette (60-30-10)</FieldLabel>
                <button
                  type="button"
                  onClick={() =>
                    onChange({
                      ...form,
                      brandColors: formatPaletteForBrief(validPaletteHex),
                    })
                  }
                  className="text-xs font-medium text-primary underline-offset-4 hover:underline"
                >
                  Apply palette
                </button>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {validPaletteHex.map((hex, idx) => {
                  const weights =
                    PALETTE_WEIGHTS[Math.min(3, validPaletteHex.length)] ?? [
                      60, 30, 10,
                    ];
                  return (
                    <span
                      key={hex}
                      className="inline-flex items-center gap-2 rounded-sm border border-border bg-card px-2 py-1 text-xs"
                    >
                      <span
                        aria-hidden
                        data-testid={`palette-swatch-${hex}`}
                        style={{ backgroundColor: hex }}
                        className="inline-block h-3.5 w-3.5 shrink-0 rounded-xs border border-border"
                      />
                      <span className="font-mono text-foreground">{hex}</span>
                      <span className="text-muted-foreground tabular-nums">
                        {weights[idx]}%
                      </span>
                    </span>
                  );
                })}
              </div>
            </div>
          )}

          {(suggestion.tone_summary || suggestion.brand_cues.length > 0) && (
            <div className="space-y-2 border-t border-border pt-2.5">
              {suggestion.tone_summary && (
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="text-xs text-muted-foreground">
                    <span className="font-medium text-foreground">
                      Art direction:{" "}
                    </span>
                    {suggestion.tone_summary}
                  </p>
                  <button
                    type="button"
                    onClick={() =>
                      onChange({
                        ...form,
                        visualIntent: suggestion.tone_summary,
                      })
                    }
                    className="shrink-0 text-xs font-medium text-primary underline-offset-4 hover:underline"
                  >
                    Apply art direction
                  </button>
                </div>
              )}

              {suggestion.brand_cues.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs text-muted-foreground">
                    Visual cues:
                  </span>
                  {suggestion.brand_cues.map((cue) => (
                    <button
                      key={cue}
                      type="button"
                      onClick={() => appendVisualCue(cue)}
                      className="rounded-sm border border-border bg-card px-2 py-0.5 text-xs text-foreground transition-colors hover:border-foreground/40"
                    >
                      + {cue}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const STEPS = [
  { id: 1, label: "1. Brand & Assets" },
  { id: 2, label: "2. Trend & Market" },
  { id: 3, label: "3. Audience & Launch" },
] as const;

type StepId = (typeof STEPS)[number]["id"];

export interface GuidedBriefStepperProps {
  form: CampaignInput;
  onChange: React.Dispatch<React.SetStateAction<CampaignInput>>;
  errors?: FieldErrors;
  onLeave?: (field: RequiredField) => void;
  isValid: boolean;
  loading: boolean;
  error: string | null;
  domainHint: string;
  onDomainHintChange: (next: string) => void;
  assistSuggestion: BrandAssistSuggestion | null;
  assistLoading: boolean;
  assistError: string | null;
  onFetchBrandAssist: () => void;
}

/**
 * 3-step guided brief builder operating directly on the shared `CampaignInput`
 * state so switching between Quick form and Guided brief preserves every field.
 */
export function GuidedBriefStepper({
  form,
  onChange,
  errors = {},
  onLeave,
  isValid,
  loading,
  error,
  domainHint,
  onDomainHintChange,
  assistSuggestion,
  assistLoading,
  assistError,
  onFetchBrandAssist,
}: GuidedBriefStepperProps) {
  const [step, setStep] = useState<StepId>(1);
  const isCreative = isCreativeAgent(form.agent);

  const handleStepKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (step === 3) return;
    if (
      e.key === "Enter" &&
      !e.metaKey &&
      !e.ctrlKey &&
      !e.shiftKey &&
      !e.altKey &&
      !e.nativeEvent.isComposing &&
      e.target instanceof HTMLInputElement &&
      e.target.type !== "checkbox" &&
      e.target.type !== "radio"
    ) {
      e.preventDefault();
      if (
        e.target.id === "domain-hint" &&
        Boolean(form.brand.trim()) &&
        !assistLoading
      ) {
        onFetchBrandAssist();
      }
    }
  };

  return (
    <div className="space-y-5" onKeyDown={handleStepKeyDown}>
      <datalist id="brand-presets">
        {BRAND_PRESETS.map((b) => (
          <option key={b} value={b} />
        ))}
      </datalist>
      <datalist id="product-presets">
        {PRODUCT_PRESETS.map((p) => (
          <option key={p} value={p} />
        ))}
      </datalist>

      {/* Clickable step pills */}
      <nav aria-label="Guided brief steps" className="grid gap-2 sm:grid-cols-3">
        {STEPS.map((s) => {
          const active = step === s.id;
          return (
            <button
              key={s.id}
              type="button"
              aria-current={active ? "step" : undefined}
              onClick={() => setStep(s.id)}
              className={cn(
                "rounded-md border px-3 py-2 text-left text-xs font-medium transition-colors",
                active
                  ? "border-primary bg-primary/5 text-foreground ring-1 ring-primary"
                  : "border-border bg-background text-muted-foreground hover:border-foreground/30 hover:text-foreground"
              )}
            >
              {s.label}
            </button>
          );
        })}
      </nav>

      {step === 1 && (
        <div className="space-y-5">
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
                onChange={(e) => onChange({ ...form, brand: e.target.value })}
                onBlur={() => onLeave?.("brand")}
                {...requiredProps("brand", errors.brand)}
              />
              <FieldError field="brand" error={errors.brand} />
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
                  onChange({ ...form, targetProduct: e.target.value })
                }
                onBlur={() => onLeave?.("targetProduct")}
                {...requiredProps("targetProduct", errors.targetProduct)}
              />
              <FieldError field="targetProduct" error={errors.targetProduct} />
            </div>
          </div>

          <BrandAssistPanel
            form={form}
            onChange={onChange}
            domainHint={domainHint}
            onDomainHintChange={onDomainHintChange}
            showDomainInput
            suggestion={assistSuggestion}
            loading={assistLoading}
            error={assistError}
            onFetch={onFetchBrandAssist}
          />

          {isCreative ? (
            <div className="space-y-4">
              <ReferenceImagesFieldset form={form} setForm={onChange} />
              <PersonSelect
                value={form.personReference}
                onChange={(personReference) =>
                  onChange((prev) => ({ ...prev, personReference }))
                }
              />
            </div>
          ) : (
            <p className="rounded-md border border-border bg-background px-3 py-2 text-xs text-muted-foreground">
              Reference product, logo, and style images can be attached when a
              creative workflow is selected in Step 2.
            </p>
          )}

          <div className="flex items-center justify-end gap-3 border-t border-border pt-4">
            <Button type="button" onClick={() => setStep(2)}>
              Next: Trend &amp; Market
            </Button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-5">
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
                    onChange={() => onChange({ ...form, agent: a.id })}
                    className="sr-only"
                  />
                  <span className="text-sm font-semibold text-foreground">
                    {a.label}
                  </span>
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
            <>
              <TrendMarketScopeFields form={form} onChange={onChange} />

              <label
                htmlFor="interactive-trend-pick"
                className="flex items-start gap-3 rounded-md border border-border bg-background px-3 py-2.5 cursor-pointer hover:border-foreground/20 transition-colors"
              >
                <input
                  id="interactive-trend-pick"
                  type="checkbox"
                  checked={form.interactiveTrendPick ?? false}
                  onChange={(e) =>
                    onChange({
                      ...form,
                      interactiveTrendPick: e.target.checked,
                    })
                  }
                  className="mt-0.5 h-4 w-4 shrink-0 rounded-sm border-border accent-primary cursor-pointer"
                />
                <span className="space-y-0.5">
                  <span className="block text-sm font-medium text-foreground">
                    Let me pick the trends myself
                  </span>
                  <span className="block text-xs text-muted-foreground">
                    Pause after gathering the top ~25 trends so you can choose
                    which to keep.
                  </span>
                </span>
              </label>
            </>
          )}

          {isCreative && (
            <div className="space-y-1.5">
              <FieldLabel as="label" htmlFor="trend">
                Target search trend
              </FieldLabel>
              <Input
                id="trend"
                placeholder='e.g., "tswift engaged"'
                value={form.targetSearchTrend ?? ""}
                onChange={(e) =>
                  onChange({
                    ...form,
                    targetSearchTrend: e.target.value,
                    trendContext: undefined,
                    trendDetectedOn: undefined,
                    saferAngle: false,
                  })
                }
                onBlur={() => onLeave?.("targetSearchTrend")}
                {...requiredProps("targetSearchTrend", errors.targetSearchTrend)}
              />
              <FieldError
                field="targetSearchTrend"
                error={errors.targetSearchTrend}
              />
            </div>
          )}

          <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
            <Button type="button" variant="outline" onClick={() => setStep(1)}>
              Back
            </Button>
            <Button type="button" onClick={() => setStep(3)}>
              Next: Audience &amp; Launch
            </Button>
          </div>
        </div>
      )}

      {step === 3 && (
        <div className="space-y-5">
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
                onChange({ ...form, targetAudience: e.target.value })
              }
              onBlur={() => onLeave?.("targetAudience")}
              {...requiredProps("targetAudience", errors.targetAudience)}
              className="resize-y"
            />
            <FieldError field="targetAudience" error={errors.targetAudience} />
            <ExampleButtons
              examples={AUDIENCE_PRESETS}
              onPick={(v) => onChange({ ...form, targetAudience: v })}
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
                onChange({ ...form, keySellingPoints: e.target.value })
              }
              onBlur={() => onLeave?.("keySellingPoints")}
              {...requiredProps("keySellingPoints", errors.keySellingPoints)}
              className="resize-y"
            />
            <FieldError
              field="keySellingPoints"
              error={errors.keySellingPoints}
            />
            <ExampleButtons
              examples={SELLING_POINTS_PRESETS}
              onPick={(v) => onChange({ ...form, keySellingPoints: v })}
            />
          </div>

          {isCreative && (
            <>
              <div className="grid gap-3 sm:grid-cols-2">
                <label
                  htmlFor="enterprise-grounding-enabled"
                  className="flex items-start gap-3 rounded-md border border-border bg-background px-3 py-2.5 cursor-pointer hover:border-foreground/20 transition-colors"
                >
                  <input
                    id="enterprise-grounding-enabled"
                    type="checkbox"
                    checked={form.enterpriseGroundingEnabled ?? false}
                    onChange={(e) =>
                      onChange({
                        ...form,
                        enterpriseGroundingEnabled: e.target.checked,
                      })
                    }
                    className="mt-0.5 h-4 w-4 shrink-0 rounded-sm border-border accent-primary cursor-pointer"
                  />
                  <span className="space-y-0.5">
                    <span className="block text-sm font-medium text-foreground">
                      Ground in Enterprise Knowledge Base
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      Include internal Discovery Engine brand guidelines, approved
                      claims, and product specs when configured.
                    </span>
                  </span>
                </label>

                <label
                  htmlFor="video-production-enabled"
                  className="flex items-start gap-3 rounded-md border border-border bg-background px-3 py-2.5 cursor-pointer hover:border-foreground/20 transition-colors"
                >
                  <input
                    id="video-production-enabled"
                    type="checkbox"
                    checked={form.videoProductionEnabled ?? false}
                    onChange={(e) =>
                      onChange({
                        ...form,
                        videoProductionEnabled: e.target.checked,
                      })
                    }
                    className="mt-0.5 h-4 w-4 shrink-0 rounded-sm border-border accent-primary cursor-pointer"
                  />
                  <span className="space-y-0.5">
                    <span className="block text-sm font-medium text-foreground">
                      Phase 3: Multi-Cut Video Reel (30s / 15s / 6s)
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      Render an animated commercial reel (16:9 hero + 9:16
                      social &amp; bumper cuts) with voiceover and instrumental
                      score from your approved visual keyframes.
                    </span>
                  </span>
                </label>
              </div>

              <LearnFromRatingsToggle
                brand={form.brand}
                checked={form.learnFromRatings ?? false}
                onChange={(checked) =>
                  onChange({ ...form, learnFromRatings: checked })
                }
              />

              <div className="rounded-md border border-border bg-background p-3.5 space-y-3">
                <FieldLabel>Visual direction (all optional)</FieldLabel>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-1.5 sm:col-span-2">
                    <FieldLabel as="label" htmlFor="visualIntent">
                      Art direction
                    </FieldLabel>
                    <Textarea
                      id="visualIntent"
                      placeholder="e.g., moody film-noir look, dramatic lighting, close-up on the product"
                      rows={2}
                      value={form.visualIntent ?? ""}
                      onChange={(e) =>
                        onChange({ ...form, visualIntent: e.target.value })
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
                      value={form.brandColors ?? ""}
                      onChange={(e) =>
                        onChange({ ...form, brandColors: e.target.value })
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
                      value={form.visualStylePreference ?? ""}
                      onChange={(e) =>
                        onChange({
                          ...form,
                          visualStylePreference: e.target.value,
                        })
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
                      value={form.visualAvoid ?? ""}
                      onChange={(e) =>
                        onChange({ ...form, visualAvoid: e.target.value })
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
                        onChange({
                          ...form,
                          visualAspectRatio: v === "auto" ? "" : v,
                        })
                      }
                    >
                      <SelectTrigger
                        id="aspectRatio"
                        className="w-full hover:border-foreground/30 transition-colors"
                      >
                        <SelectValue placeholder="Auto (let AI choose)" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="auto">
                          Auto (let AI choose)
                        </SelectItem>
                        <SelectItem value="9:16">
                          9:16 — vertical reel / story
                        </SelectItem>
                        <SelectItem value="1:1">1:1 — square feed</SelectItem>
                        <SelectItem value="4:5">4:5 — portrait feed</SelectItem>
                        <SelectItem value="3:4">3:4 — portrait</SelectItem>
                        <SelectItem value="16:9">16:9 — landscape</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              </div>
            </>
          )}

          {error && (
            <div
              role="alert"
              className="rounded-md bg-destructive/5 border border-destructive/30 px-4 py-3"
            >
              <p className="text-sm text-destructive">{error}</p>
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-4 border-t border-border pt-4">
            <Button type="button" variant="outline" onClick={() => setStep(2)}>
              Back
            </Button>

            <div className="flex flex-wrap items-center gap-4">
              <span
                id="submit-hint"
                aria-live="polite"
                className="text-xs text-muted-foreground"
              >
                {isValid ? (
                  <>
                    or press <Kbd>Ctrl</Kbd>/<Kbd>⌘</Kbd> + <Kbd>Enter</Kbd>
                  </>
                ) : isCreative && invalidReferenceUris(form).length ? (
                  "Fix the reference image URL in Step 1 to start."
                ) : isCreative ? (
                  "Fill in Brand & Product (Step 1), Trend (Step 2), and Audience & Selling points to start."
                ) : (
                  "Fill in Brand & Product (Step 1) and Audience & Selling points to start."
                )}
              </span>

              <Button
                type="submit"
                size="lg"
                className="h-10 px-5 text-sm font-semibold"
                disabled={!isValid || loading}
                aria-describedby="submit-hint"
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
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
