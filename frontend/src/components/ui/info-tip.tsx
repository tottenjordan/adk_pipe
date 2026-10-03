"use client";

import * as React from "react";
import { Popover as PopoverPrimitive } from "@base-ui/react/popover";
import { Info } from "lucide-react";

import { cn } from "@/lib/utils";

type InfoTipProps = {
  /** Accessible name for the trigger, e.g. "About cumulative regret". */
  label: string;
  /** Help content: plain text or a small list. */
  children: React.ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  align?: "start" | "center" | "end";
  className?: string;
};

/**
 * Small muted "ⓘ" button that reveals plain-language help. Opens on hover, on
 * keyboard focus and on click/tap (touch); closes on Escape, outside press, blur
 * and mouse leave. Built on base-ui Popover (non-modal; focus stays on the trigger),
 * with the content wired to the trigger via aria-describedby while open.
 */
function InfoTip({ label, children, side = "top", align = "center", className }: InfoTipProps) {
  const [open, setOpen] = React.useState(false);
  const contentId = React.useId();
  const pointerDown = React.useRef(false);
  const popupRef = React.useRef<HTMLDivElement>(null);

  return (
    <PopoverPrimitive.Root open={open} onOpenChange={setOpen}>
      <PopoverPrimitive.Trigger
        type="button"
        aria-label={label}
        aria-describedby={open ? contentId : undefined}
        openOnHover
        delay={150}
        closeDelay={100}
        data-slot="info-tip-trigger"
        onPointerDown={() => {
          // A mouse/touch press focuses the button too; let the click toggle instead.
          pointerDown.current = true;
        }}
        onFocus={() => {
          if (!pointerDown.current) setOpen(true);
          pointerDown.current = false;
        }}
        onBlur={(e) => {
          const next = e.relatedTarget as Node | null;
          if (next && popupRef.current?.contains(next)) return;
          setOpen(false);
        }}
        className={cn(
          "inline-flex size-4 shrink-0 cursor-help items-center justify-center rounded-full align-middle text-muted-foreground/70 outline-none hover:text-foreground focus-visible:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 data-[popup-open]:text-foreground",
          className
        )}
      >
        <Info aria-hidden className="size-3.5" strokeWidth={2} />
      </PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Positioner side={side} align={align} sideOffset={6} collisionPadding={8} className="z-50">
          <PopoverPrimitive.Popup
            ref={popupRef}
            id={contentId}
            initialFocus={false}
            finalFocus={false}
            data-slot="info-tip-content"
            className="max-w-xs rounded-lg border border-border bg-popover px-3 py-2 text-sm leading-snug font-normal text-popover-foreground shadow-sm outline-none transition-opacity duration-100 data-[ending-style]:opacity-0 data-[starting-style]:opacity-0 motion-reduce:transition-none"
          >
            {children}
          </PopoverPrimitive.Popup>
        </PopoverPrimitive.Positioner>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}

/** A definition-style list for multi-option help ("Label: description" per line). */
function InfoTipList({ items }: { items: { term: string; text: string }[] }) {
  return (
    <ul className="space-y-1.5">
      {items.map((it) => (
        <li key={it.term}>
          <span className="font-medium text-foreground">{it.term}:</span> {it.text}
        </li>
      ))}
    </ul>
  );
}

export { InfoTip, InfoTipList };
