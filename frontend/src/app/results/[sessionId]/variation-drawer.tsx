"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { FieldLabel } from "@/components/field-label";
import { SegmentedControl } from "@/components/segmented-control";
import {
  generateVariations,
  type GenerateVariationsResponse,
} from "@/lib/api";
import {
  VARIATION_AXIS_LABELS,
  type Proof,
  type VariationAxis,
} from "@/lib/eval-matching";
import { cn } from "@/lib/utils";
import { ProofImage } from "./proof-grid";

interface AxisMeta {
  value: VariationAxis;
  label: string;
  mutates: string;
  freezes: string;
  placeholder: string;
}

export const VARIATION_AXIS_OPTIONS: AxisMeta[] = [
  {
    value: "visual_style",
    label: VARIATION_AXIS_LABELS.visual_style,
    mutates: "Visual style family and image generation prompt",
    freezes: "All ad copy fields, composition mode, aspect ratio, trend motif, and brand cue",
    placeholder: "Candid 35mm film photo, 2D flat / vector cartoon",
  },
  {
    value: "composition_mode",
    label: VARIATION_AXIS_LABELS.composition_mode,
    mutates: "Composition framing mode and scene layout prompt",
    freezes: "All ad copy fields, visual style, aspect ratio, trend motif, and brand cue",
    placeholder: "product_hero, lifestyle_context",
  },
  {
    value: "cta_hook",
    label: VARIATION_AXIS_LABELS.cta_hook,
    mutates: "Opening headline hook and call-to-action phrase",
    freezes: "Body copy, social caption, tone, visual style, composition mode, and aspect ratio",
    placeholder: "Urgency limited-window hook, Curiosity question hook",
  },
  {
    value: "locale_market",
    label: VARIATION_AXIS_LABELS.locale_market,
    mutates: "Localized copy and regional/cultural environment cues",
    freezes: "Visual style, composition mode, aspect ratio, tone, angle, and brand cue",
    placeholder: "Tokyo, Japan; London, UK",
  },
  {
    value: "audience_persona",
    label: VARIATION_AXIS_LABELS.audience_persona,
    mutates: "Persona-adapted copy, tone, and talent/scene cues",
    freezes: "Visual style, composition mode, aspect ratio, trend motif, brand cue, and angle",
    placeholder: "Gen-Z mobile creators, Time-pressed weekday commuters",
  },
  {
    value: "sibling_sku",
    label: VARIATION_AXIS_LABELS.sibling_sku,
    mutates: "Featured sibling SKU/product variant across copy and image prompt",
    freezes: "Visual style, composition mode, aspect ratio, trend motif, brand cue, and tone",
    placeholder: "Travel Edition, Pro Series",
  },
];

const COMPOSITION_MODES: { value: string; label: string }[] = [
  { value: "product_hero", label: "product_hero" },
  { value: "product_in_scene", label: "product_in_scene" },
  { value: "cast_with_product", label: "cast_with_product" },
  { value: "lifestyle_context", label: "lifestyle_context" },
  { value: "graphic_lockup", label: "graphic_lockup" },
];

const COUNT_OPTIONS: { value: "1" | "2" | "3"; label: string }[] = [
  { value: "1", label: "1 variant" },
  { value: "2", label: "2 variants" },
  { value: "3", label: "3 variants" },
];

function parseTargetValues(raw: string, maxItems: number): string[] {
  // For semicolon-separated entries (e.g. "Tokyo, Japan; London, UK"), split on ";" first;
  // otherwise split on commas so simple comma lists ("Tokyo, London") work intuitively.
  const delimiter = raw.includes(";") ? ";" : ",";
  return raw
    .split(delimiter)
    .map((item) => item.trim())
    .filter((item) => item.length > 0)
    .slice(0, maxItems);
}

/**
 * Dialog for generating 1–3 controlled single-axis creative variations from a
 * base creative so a contextual bandit experiment can isolate causal lift.
 */
export function VariationDrawer({
  open,
  proof,
  appName,
  userId,
  sessionId,
  imageUrlFor,
  remainingSlots = 12,
  onClose,
  onGenerated,
}: {
  open: boolean;
  proof: Proof | null;
  appName: string;
  userId: string;
  sessionId: string;
  imageUrlFor: (conceptName: string) => string | null;
  /** Remaining visual concept slots before hitting the 16-concept session cap. */
  remainingSlots?: number;
  onClose: () => void;
  onGenerated: (response: GenerateVariationsResponse) => void;
}) {
  const [axis, setAxis] = useState<VariationAxis>("visual_style");
  const [countStr, setCountStr] = useState<"1" | "2" | "3">("2");
  const [targetInput, setTargetInput] = useState("");
  const [selectedModes, setSelectedModes] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const count = Number(countStr) as 1 | 2 | 3;
  const effectiveCount = Math.min(count, Math.max(1, remainingSlots)) as 1 | 2 | 3;
  const countOptions = useMemo(
    () =>
      COUNT_OPTIONS.map((opt) => ({
        ...opt,
        disabled: Number(opt.value) > remainingSlots,
      })),
    [remainingSlots]
  );
  const activeAxisMeta = useMemo(
    () =>
      VARIATION_AXIS_OPTIONS.find((item) => item.value === axis) ??
      VARIATION_AXIS_OPTIONS[0],
    [axis]
  );

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      abortRef.current?.abort();
      abortRef.current = null;
      setSubmitting(false);
      setError(null);
      onClose();
    }
  };

  const toggleCompositionChip = (mode: string) => {
    setSelectedModes((prev) => {
      if (prev.includes(mode)) return prev.filter((m) => m !== mode);
      if (prev.length >= effectiveCount) return [...prev.slice(1), mode];
      return [...prev, mode];
    });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!proof || submitting) return;
    if (remainingSlots <= 0) {
      setError("This session has reached the 16-creative limit.");
      return;
    }

    const targetValues =
      axis === "composition_mode" && selectedModes.length > 0
        ? selectedModes.slice(0, effectiveCount)
        : parseTargetValues(targetInput, effectiveCount);

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setSubmitting(true);
    setError(null);

    try {
      const response = await generateVariations(
        appName,
        userId,
        sessionId,
        {
          base_concept_index: proof.index,
          axis,
          count: effectiveCount,
          target_values: targetValues,
        },
        { signal: controller.signal }
      );
      if (controller.signal.aborted) return;
      setSubmitting(false);
      setTargetInput("");
      setSelectedModes([]);
      onGenerated(response);
      onClose();
    } catch (err) {
      if (
        controller.signal.aborted ||
        (err instanceof Error && err.name === "AbortError")
      ) {
        return;
      }
      setSubmitting(false);
      setError(
        err instanceof Error ? err.message : "Could not generate variations."
      );
    }
  };

  const baseMode = proof?.concept.composition_mode || "product_in_scene";

  return (
    <Dialog open={open && proof !== null} onOpenChange={handleOpenChange}>
      {proof && (
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl data-open:animate-none data-closed:animate-none">
          <DialogHeader>
            <DialogTitle className="text-lg font-semibold text-foreground">
              Generate controlled variations
            </DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground">
              Vary a single causal dimension while freezing every other creative
              attribute so bandit experiments isolate what drives lift.
            </DialogDescription>
          </DialogHeader>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="flex items-center gap-3 rounded-md border border-border bg-muted/30 p-2.5">
              <ProofImage
                src={imageUrlFor(proof.concept.concept_name)}
                alt={proof.concept.concept_summary}
                className="size-14 shrink-0 rounded-sm text-[10px]"
              />
              <div className="min-w-0 flex-1">
                <FieldLabel as="p">Base creative</FieldLabel>
                <p className="truncate text-sm font-medium text-foreground">
                  {proof.concept.headline}
                </p>
                <p className="truncate text-xs text-muted-foreground">
                  {proof.concept.concept_name}
                  {proof.concept.visual_style
                    ? ` · ${proof.concept.visual_style}`
                    : ""}
                </p>
              </div>
            </div>

            <fieldset disabled={submitting} className="space-y-2">
              <FieldLabel as="legend">Variation axis</FieldLabel>
              <div
                role="radiogroup"
                aria-label="Variation axis"
                className="grid grid-cols-1 gap-2 sm:grid-cols-2"
              >
                {VARIATION_AXIS_OPTIONS.map((opt) => {
                  const active = axis === opt.value;
                  return (
                    <label
                      key={opt.value}
                      className={cn(
                        "flex cursor-pointer items-start gap-2.5 rounded-md border p-2.5 text-left transition-colors",
                        active
                          ? "border-primary bg-primary/5"
                          : "border-border hover:border-foreground/40"
                      )}
                    >
                      <input
                        type="radio"
                        name="variation-axis"
                        value={opt.value}
                        checked={active}
                        onChange={() => {
                          setAxis(opt.value);
                          setError(null);
                        }}
                        className="mt-0.5 size-4 shrink-0 accent-primary"
                      />
                      <span className="min-w-0">
                        <span className="block text-sm font-medium text-foreground">
                          {opt.label}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            </fieldset>

            <div
              data-testid="axis-invariant-preview"
              className="rounded-md border border-border bg-card p-3 text-xs"
            >
              <div className="grid gap-2 sm:grid-cols-2">
                <div>
                  <FieldLabel as="p">Changes</FieldLabel>
                  <p className="mt-0.5 text-foreground">{activeAxisMeta.mutates}</p>
                </div>
                <div>
                  <FieldLabel as="p">Frozen invariant</FieldLabel>
                  <p className="mt-0.5 text-muted-foreground">
                    {activeAxisMeta.freezes}
                  </p>
                </div>
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-start">
              <div>
                <FieldLabel id="variation-count-label" className="mb-1.5 block">
                  Number of variants
                </FieldLabel>
                <SegmentedControl
                  labelledBy="variation-count-label"
                  options={countOptions}
                  value={String(effectiveCount) as "1" | "2" | "3"}
                  onChange={setCountStr}
                />
              </div>

              <div className="min-w-0">
                <FieldLabel
                  as="label"
                  htmlFor="variation-targets-input"
                  className="mb-1.5 block"
                >
                  Target values (optional)
                </FieldLabel>
                {axis === "composition_mode" ? (
                  <div className="flex flex-wrap gap-1.5" role="group" aria-label="Target composition modes">
                    {COMPOSITION_MODES.map((m) => {
                      const isBase = m.value === baseMode;
                      const picked = selectedModes.includes(m.value);
                      return (
                        <button
                          key={m.value}
                          type="button"
                          disabled={submitting || isBase}
                          onClick={() => toggleCompositionChip(m.value)}
                          aria-pressed={picked}
                          className={cn(
                            "rounded-md border px-2 py-1 font-mono text-xs transition-colors",
                            isBase
                              ? "cursor-not-allowed border-border bg-muted/50 text-muted-foreground/60"
                              : picked
                                ? "border-primary bg-primary/10 text-foreground"
                                : "border-border bg-background text-foreground hover:border-foreground/40"
                          )}
                        >
                          {m.label}
                          {isBase ? " (base)" : ""}
                        </button>
                      );
                    })}
                  </div>
                ) : (
                  <Input
                    id="variation-targets-input"
                    value={targetInput}
                    disabled={submitting}
                    onChange={(e) => setTargetInput(e.target.value)}
                    placeholder={activeAxisMeta.placeholder}
                  />
                )}
                <p className="mt-1 text-xs text-muted-foreground">
                  Leave blank to auto-select {effectiveCount} high-contrast value
                  {effectiveCount > 1 ? "s" : ""}.
                </p>
              </div>
            </div>

            {error && (
              <p role="alert" className="text-sm text-mark-fail">
                {error}
              </p>
            )}

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={submitting}
                onClick={() => handleOpenChange(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={submitting || remainingSlots <= 0}
              >
                {submitting
                  ? "Generating variations…"
                  : `Generate ${effectiveCount} variation${effectiveCount > 1 ? "s" : ""}`}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      )}
    </Dialog>
  );
}
