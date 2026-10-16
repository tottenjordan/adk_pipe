"use client";

import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FieldLabel } from "@/components/field-label";
import {
  BRAND_PRESETS,
  AUDIENCE_PRESETS,
  PRODUCT_PRESETS,
  SELLING_POINTS_PRESETS,
} from "@/lib/presets";
import { FieldError, requiredProps, type FieldErrors, type RequiredField } from "./required-fields";
import type { FormSectionProps } from "./types";

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

/** The four campaign fields every agent needs: brand, product, audience, selling points. */
export function CampaignFields({
  form,
  setForm,
  errors,
  onLeave,
}: FormSectionProps & {
  /** Messages for the missing fields to flag (see required-fields). */
  errors: FieldErrors;
  /** A required field lost focus (its message may show from now on). */
  onLeave: (field: RequiredField) => void;
}) {
  return (
    <>
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
            onBlur={() => onLeave("brand")}
            {...requiredProps("brand", errors.brand)}
          />
          <FieldError field="brand" error={errors.brand} />
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
            onBlur={() => onLeave("targetProduct")}
            {...requiredProps("targetProduct", errors.targetProduct)}
          />
          <FieldError field="targetProduct" error={errors.targetProduct} />
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
          onBlur={() => onLeave("targetAudience")}
          {...requiredProps("targetAudience", errors.targetAudience)}
          className="resize-y"
        />
        <FieldError field="targetAudience" error={errors.targetAudience} />
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
          onBlur={() => onLeave("keySellingPoints")}
          {...requiredProps("keySellingPoints", errors.keySellingPoints)}
          className="resize-y"
        />
        <FieldError field="keySellingPoints" error={errors.keySellingPoints} />
        <ExampleButtons
          examples={SELLING_POINTS_PRESETS}
          onPick={(v) => setForm({ ...form, keySellingPoints: v })}
        />
      </div>
    </>
  );
}
