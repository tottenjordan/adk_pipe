"use client";

import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

/*
 * The app's one disclosure pattern, on Base UI Collapsible: the panel's height
 * animates through --collapsible-panel-height (0 on data-starting-style /
 * data-ending-style) and the chevron turns on the same curve and durations
 * (--dur-dialog in, --dur-exit out). Reduced motion: both snap.
 *
 *   <Collapsible>
 *     <DisclosureTrigger>Label <DisclosureChevron /></DisclosureTrigger>
 *     <DisclosurePanel>…</DisclosurePanel>
 *   </Collapsible>
 */

/** A Collapsible trigger that drives its DisclosureChevron (via data-panel-open). */
export function DisclosureTrigger({ className, ...props }: React.ComponentProps<typeof CollapsibleTrigger>) {
  return <CollapsibleTrigger className={cn("group/disclosure", className)} {...props} />;
}

/** Down chevron turns 180°, right chevron 90°, in step with the panel. */
export function DisclosureChevron({
  direction = "down",
  className,
}: {
  direction?: "down" | "right";
  className?: string;
}) {
  const Icon = direction === "down" ? ChevronDownIcon : ChevronRightIcon;
  return (
    <Icon
      aria-hidden="true"
      className={cn(
        "shrink-0 text-muted-foreground transition-transform duration-(--dur-exit) ease-(--ease-out) group-data-[panel-open]/disclosure:duration-(--dur-dialog) motion-reduce:transition-none",
        direction === "down"
          ? "group-data-[panel-open]/disclosure:rotate-180"
          : "group-data-[panel-open]/disclosure:rotate-90",
        className
      )}
    />
  );
}

/** The panel, opening and closing by height. Put padding/margins on a child, not here. */
export function DisclosurePanel({ className, ...props }: React.ComponentProps<typeof CollapsibleContent>) {
  return (
    <CollapsibleContent
      className={cn(
        "h-(--collapsible-panel-height) overflow-hidden transition-[height] duration-(--dur-dialog) ease-(--ease-out) data-ending-style:h-0 data-ending-style:duration-(--dur-exit) data-starting-style:h-0 motion-reduce:transition-none",
        className
      )}
      {...props}
    />
  );
}

/** A quiet, collapsed-by-default panel (artifacts, raw session state). */
export function QuietDisclosure({
  title,
  count,
  children,
  className,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Collapsible>
      <div className={cn("overflow-hidden rounded-lg border border-border bg-card", className)}>
        <DisclosureTrigger className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left transition-colors hover:bg-muted/60">
          <span className="text-sm font-medium text-foreground">
            {title}
            {count !== undefined && (
              <span className="ml-1.5 font-normal text-muted-foreground tabular-nums">{count}</span>
            )}
          </span>
          <DisclosureChevron className="size-4" />
        </DisclosureTrigger>
        <DisclosurePanel>
          <div className="border-t border-border px-4 py-4">{children}</div>
        </DisclosurePanel>
      </div>
    </Collapsible>
  );
}
