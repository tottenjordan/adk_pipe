import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Layout-matched loading placeholder: a muted block that pulses (static under
 * reduced motion). Decorative, so it is aria-hidden; wrap a group of skeletons
 * in `role="status"` with an sr-only label ("Loading runs…").
 */
function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      aria-hidden
      className={cn("animate-pulse rounded-sm bg-muted motion-reduce:animate-none", className)}
      {...props}
    />
  );
}

export { Skeleton };
