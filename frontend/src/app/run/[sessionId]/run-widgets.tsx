"use client";

import React, { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { FieldLabel } from "@/components/field-label";
import {
  FIELD_LABELS,
  HIDDEN_FIELDS,
  WIDGET_LAYOUTS,
  DEFAULT_LAYOUT,
} from "./run-config";
import { extractItems } from "./run-helpers";

/** Render a single field with label + value. */
function FieldCell({ fieldKey, value }: { fieldKey: string; value: unknown }) {
  const label = FIELD_LABELS[fieldKey] || fieldKey.replace(/_/g, " ");
  return (
    <div>
      <FieldLabel as="dt">{label}</FieldLabel>
      <dd className="text-xs leading-snug break-words whitespace-pre-wrap text-foreground/85 mt-0.5">
        {typeof value === "string" || typeof value === "number"
          ? String(value)
          : JSON.stringify(value, null, 2)}
      </dd>
    </div>
  );
}

/** Render a single item card with side-by-side panels layout. */
function ItemCard({
  item,
  index,
  widgetKey,
}: {
  item: Record<string, unknown>;
  index: number;
  widgetKey: string;
}): React.ReactNode {
  const title =
    (item.concept_name as string) ||
    (item.headline as string) ||
    `Item ${index + 1}`;

  const layout = WIDGET_LAYOUTS[widgetKey] || DEFAULT_LAYOUT;
  const pairs: [string, string][] = layout.pairs;
  const fullWidth = layout.fullWidth;

  // Collect fields used in structured layout
  const structuredFields = new Set<string>();
  pairs.forEach(([a, b]) => { structuredFields.add(a); structuredFields.add(b); });
  if (fullWidth) structuredFields.add(fullWidth);
  structuredFields.add("concept_name");
  structuredFields.add("headline");
  HIDDEN_FIELDS.forEach((f) => structuredFields.add(f));

  // Remaining fields not covered by structured layout
  const remainingEntries = Object.entries(item).filter(
    ([k, v]) => v !== null && v !== undefined && v !== "" && !structuredFields.has(k)
  );

  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3 space-y-1.5">
      {/* Title bar */}
      <div className="flex items-center gap-2 border-b border-border pb-2">
        <Badge
          variant="secondary"
          className="text-[10px] px-1.5 py-0 bg-muted text-foreground border-0 font-semibold tabular-nums"
        >
          {index + 1}
        </Badge>
        <span className="text-sm font-bold leading-tight text-foreground">
          {title}
        </span>
      </div>

      {/* Side-by-side panels */}
      {pairs.map(([leftKey, rightKey]): React.ReactNode => {
        const leftVal = item[leftKey];
        const rightVal = item[rightKey];
        if (!leftVal && !rightVal) return null;
        return (
          <div key={`${leftKey}-${rightKey}`} className="grid grid-cols-2 gap-2">
            <div className="rounded-md bg-muted/60 px-2.5 py-2">
              {leftVal ? (
                <FieldCell fieldKey={leftKey} value={leftVal} />
              ) : (
                <span className="text-xs text-muted-foreground italic">--</span>
              )}
            </div>
            <div className="rounded-md bg-muted/60 px-2.5 py-2">
              {rightVal ? (
                <FieldCell fieldKey={rightKey} value={rightVal} />
              ) : (
                <span className="text-xs text-muted-foreground italic">--</span>
              )}
            </div>
          </div>
        );
      })}

      {/* Full-width panel */}
      {fullWidth && !!item[fullWidth] && (
        <div className="rounded-md bg-muted/60 px-2.5 py-2">
          <FieldCell fieldKey={fullWidth} value={item[fullWidth]} />
        </div>
      )}

      {/* Remaining fields not in the structured layout */}
      {remainingEntries.length > 0 && (
        <div className="space-y-1.5 pt-1 border-t border-border">
          {remainingEntries.map(([key, value]) => (
            <FieldCell key={key} fieldKey={key} value={value} />
          ))}
        </div>
      )}
    </div>
  );
}

export function PipelineWidget({
  label,
  stateKey,
  data,
}: {
  label: string;
  stateKey: string;
  data: unknown;
}) {
  const [open, setOpen] = useState(false);
  const items = extractItems(data);
  const itemCount = items ? items.length : 0;

  return (
    <>
      <button
        type="button"
        className="block w-full rounded-lg border border-border bg-card text-left transition-colors hover:border-primary/40"
        onClick={() => setOpen(true)}
      >
        <div className="py-2.5 px-4 flex items-center justify-between">
          <span className="flex items-center gap-2 text-sm font-bold">
            <span className="text-foreground">{label}</span>
            {itemCount > 0 && (
              <Badge
                variant="secondary"
                className="text-[10px] px-1.5 py-0 font-semibold tabular-nums"
              >
                {itemCount}
              </Badge>
            )}
          </span>
          <span className="text-xs text-primary">View</span>
        </div>
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/30 animate-in fade-in duration-150"
          onClick={() => setOpen(false)}
        >
          <div
            className="relative mx-4 w-full max-w-3xl max-h-[85vh] overflow-hidden flex flex-col
                       rounded-lg border border-border bg-card shadow-lg
                       animate-in zoom-in-95 duration-200"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <h2 className="text-lg font-bold flex items-center gap-2">
                <span>{label}</span>
                {itemCount > 0 && (
                  <Badge variant="secondary" className="font-semibold tabular-nums">
                    {itemCount} items
                  </Badge>
                )}
              </h2>
              <button
                onClick={() => setOpen(false)}
                className="flex h-7 w-7 items-center justify-center rounded-sm text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                aria-label="Close"
              >
                &times;
              </button>
            </div>
            <div className="flex-1 overflow-auto p-5">
              {items ? (
                <div className="space-y-3">
                  {items.map((item, i) => (
                    <ItemCard key={i} item={item} index={i} widgetKey={stateKey} />
                  ))}
                </div>
              ) : (
                <pre className="whitespace-pre-wrap break-words rounded-md bg-muted/60 p-4 text-sm font-mono leading-relaxed text-foreground/80">
                  {typeof data === "string"
                    ? data
                    : JSON.stringify(data, null, 2)}
                </pre>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
