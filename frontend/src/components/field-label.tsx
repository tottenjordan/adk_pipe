import * as React from "react";

import { cn } from "@/lib/utils";

type FieldLabelElement =
  | "span"
  | "p"
  | "div"
  | "dt"
  | "label"
  | "h2"
  | "h3"
  | "h4"
  | "legend";

type FieldLabelProps = React.HTMLAttributes<HTMLElement> & {
  /** Element to render. Use "label" (with htmlFor) for form controls. */
  as?: FieldLabelElement;
  htmlFor?: string;
};

/**
 * The one small label style: sentence case, muted, no uppercase or tracking.
 * Replaces the old uppercase "eyebrow" labels across the app.
 */
function FieldLabel({ as = "span", className, ...props }: FieldLabelProps) {
  const Tag = as as React.ElementType;
  return (
    <Tag
      data-slot="field-label"
      className={cn(
        "block text-xs leading-snug font-medium text-muted-foreground",
        className
      )}
      {...props}
    />
  );
}

export { FieldLabel };
