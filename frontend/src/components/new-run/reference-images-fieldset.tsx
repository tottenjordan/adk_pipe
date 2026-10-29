"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FieldLabel } from "@/components/field-label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { MAX_REFERENCE_IMAGES, isReferenceUri } from "@/lib/reference-images";
import type { ReferenceImageInput, ReferenceRowInput } from "@/lib/types";
import type { FormSectionProps } from "./types";

// Client-only React keys for the extra reference rows (stable across
// edits/removals, unlike the array index).
let referenceRowSeq = 0;
export function newReferenceRow(
  row: ReferenceImageInput = { uri: "", role: "" },
): ReferenceRowInput {
  referenceRowSeq += 1;
  return { ...row, id: `ref-row-${referenceRowSeq}` };
}

/** One reference-image row: URI input + (once a URI is set) a role select. */
function ReferenceRow({
  id,
  index,
  value,
  onChange,
  onRemove,
}: {
  id: string;
  index: number;
  value: ReferenceImageInput;
  onChange: (value: ReferenceImageInput) => void;
  onRemove?: () => void;
}) {
  const invalid = Boolean(value.uri.trim()) && !isReferenceUri(value.uri);
  const hintId = `${id}-hint`;
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <Input
          id={id}
          aria-label={index > 1 ? `Reference image ${index} URL` : undefined}
          aria-invalid={invalid || undefined}
          aria-describedby={invalid ? hintId : undefined}
          placeholder="gs://bucket/product.png or https://…"
          value={value.uri}
          onChange={(e) => onChange({ ...value, uri: e.target.value })}
        />
        {onRemove && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onRemove}
            aria-label={`Remove reference image ${index}`}
          >
            Remove
          </Button>
        )}
      </div>
      {invalid && (
        <p id={hintId} className="text-xs text-mark-fail">
          Use a gs://bucket/object or http(s):// URL.
        </p>
      )}
      {value.uri.trim() && (
        <Select
          value={value.role || ""}
          onValueChange={(v) => v && onChange({ ...value, role: v })}
        >
          <SelectTrigger
            aria-label={`How to use reference image ${index}`}
            className="w-full hover:border-foreground/30 transition-colors"
          >
            <SelectValue placeholder="Use as: product (default)" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="product">Product — reproduce this exact product</SelectItem>
            <SelectItem value="logo">Logo — place this brand logo, small and legible</SelectItem>
            <SelectItem value="style">Style — match its palette, texture and lighting</SelectItem>
            <SelectItem value="cast">Cast — feature this talent / spokesperson in cast shots</SelectItem>
          </SelectContent>
        </Select>
      )}
    </div>
  );
}

/** Up to MAX_REFERENCE_IMAGES reference images (URI + role each) for the creative agents. */
export function ReferenceImagesFieldset({ form, setForm }: FormSectionProps) {
  const extraReferences = form.extraReferenceImages ?? [];
  const canAddReference =
    Boolean(form.referenceImageUri?.trim()) &&
    1 + extraReferences.length < MAX_REFERENCE_IMAGES &&
    extraReferences.every((r) => r.uri.trim());

  return (
    <div className="space-y-1.5">
      <FieldLabel as="label" htmlFor="referenceImage">
        Reference images (optional, up to {MAX_REFERENCE_IMAGES})
      </FieldLabel>
      <ReferenceRow
        id="referenceImage"
        index={1}
        value={{
          uri: form.referenceImageUri ?? "",
          role: form.referenceImageRole ?? "",
        }}
        onChange={(ref) =>
          setForm({
            ...form,
            referenceImageUri: ref.uri,
            referenceImageRole: ref.role,
          })
        }
      />
      {extraReferences.map((ref, i) => (
        <ReferenceRow
          key={ref.id ?? i}
          id={`referenceImage${i + 2}`}
          index={i + 2}
          value={ref}
          onChange={(next) =>
            setForm({
              ...form,
              extraReferenceImages: extraReferences.map((r, j) =>
                j === i ? next : r,
              ),
            })
          }
          onRemove={() =>
            setForm({
              ...form,
              extraReferenceImages: extraReferences.filter((_, j) => j !== i),
            })
          }
        />
      ))}
      {canAddReference && (
        <button
          type="button"
          onClick={() =>
            setForm({
              ...form,
              extraReferenceImages: [...extraReferences, newReferenceRow()],
            })
          }
          className="text-xs text-primary underline-offset-4 hover:underline"
        >
          Add another reference image
        </button>
      )}
      {form.referenceImageUri?.trim() && (
        <p className="text-xs text-muted-foreground">
          Any text in a reference image is ignored.
        </p>
      )}
    </div>
  );
}
