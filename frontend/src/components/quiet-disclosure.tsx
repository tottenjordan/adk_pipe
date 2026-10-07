"use client";

import { useState } from "react";
import { ChevronDownIcon } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

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
  const [open, setOpen] = useState(false);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <div className={cn("overflow-hidden rounded-lg border border-border bg-card", className)}>
        <CollapsibleTrigger className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left transition-colors hover:bg-muted/60">
          <span className="text-sm font-medium text-foreground">
            {title}
            {count !== undefined && (
              <span className="ml-1.5 font-normal text-muted-foreground tabular-nums">{count}</span>
            )}
          </span>
          <ChevronDownIcon
            aria-hidden="true"
            className={cn("size-4 text-muted-foreground transition-transform", open && "rotate-180")}
          />
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="border-t border-border px-4 py-4">{children}</div>
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}
