import * as React from "react";

import { cn } from "@/lib/utils";

/** A key cap for shortcut hints and help, e.g. <Kbd>?</Kbd> or <Kbd>⌘</Kbd><Kbd>Enter</Kbd>. */
function Kbd({ className, ...props }: React.ComponentProps<"kbd">) {
  return (
    <kbd
      data-slot="kbd"
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-sm border border-border bg-card px-1 font-mono text-[0.7rem] leading-none font-medium text-muted-foreground shadow-[inset_0_-1px_0_var(--rule)] select-none",
        className
      )}
      {...props}
    />
  );
}

export { Kbd };
