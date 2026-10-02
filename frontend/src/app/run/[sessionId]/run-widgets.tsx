"use client";

import React, { useEffect, useRef, useState } from "react";
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
          className="text-xs px-1.5 py-0 bg-muted text-foreground border-0 font-semibold tabular-nums"
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

/** "4 ad copies" / "1 ad copy" — the descriptive open-button label. */
export function countLabel(count: number, noun: [string, string]): string {
  return `${count} ${count === 1 ? noun[0] : noun[1]}`;
}

export function PipelineWidget({
  label,
  stateKey,
  noun,
  data,
}: {
  label: string;
  stateKey: string;
  noun: [string, string];
  data: unknown;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const items = extractItems(data);
  const itemCount = items ? items.length : 0;
  const summary = items ? countLabel(itemCount, noun) : `${label} details`;

  // Escape closes the overlay (keyboard users have no other close path besides the button).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    const trigger = triggerRef.current;
    return () => {
      document.removeEventListener("keydown", onKey);
      trigger?.focus(); // return focus to the open button on close
    };
  }, [open]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="block w-full rounded-lg border border-border bg-card px-4 py-3 text-left transition-colors hover:border-primary/40"
        onClick={() => setOpen(true)}
      >
        <span className="block text-xs font-medium text-muted-foreground">{label}</span>
        <span className="mt-0.5 block text-sm font-medium text-primary">{summary}</span>
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/30"
          onClick={() => setOpen(false)}
        >
          <div
            className="relative mx-4 w-full max-w-3xl max-h-[85vh] overflow-hidden flex flex-col
                       rounded-lg border border-border bg-card shadow-lg"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label={`${label}: ${summary}`}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <h2 className="text-lg font-bold flex items-center gap-2">
                <span>{label}</span>
                {items && (
                  <span className="text-sm font-normal text-muted-foreground">
                    {summary}
                  </span>
                )}
              </h2>
              <button
                autoFocus
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
