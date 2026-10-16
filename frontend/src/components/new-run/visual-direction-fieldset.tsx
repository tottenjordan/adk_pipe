"use client";

import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FieldLabel } from "@/components/field-label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { CampaignInput } from "@/lib/types";
import type { FormSectionProps } from "./types";

const VISUAL_FIELDS = [
  "visualIntent",
  "brandColors",
  "visualStylePreference",
  "visualAvoid",
  "visualAspectRatio",
] as const;

/** Any of the optional visual-direction fields is set. */
export function hasVisualDirection(values: Partial<CampaignInput>): boolean {
  return VISUAL_FIELDS.some((f) => Boolean(values[f]?.trim()));
}

/** The collapsible "Visual direction (all optional)" fields for the creative agents. */
export function VisualDirectionFieldset({
  form,
  setForm,
  open,
  onOpenChange,
}: FormSectionProps & { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <details
      open={open}
      onToggle={(e) => onOpenChange(e.currentTarget.open)}
      className="group rounded-md border border-border bg-background"
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-md px-3 py-2.5 text-sm font-medium text-foreground [&::-webkit-details-marker]:hidden">
        <span
          aria-hidden
          className="inline-block text-muted-foreground transition-transform duration-(--dur-dialog) ease-(--ease-out) group-open:rotate-90 motion-reduce:transition-none"
        >
          ›
        </span>
        Visual direction (all optional)
        {!open && hasVisualDirection(form) && (
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
  );
}
