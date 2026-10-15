import { saferAngleText } from "@/lib/safer-angle";
import { cn } from "@/lib/utils";

/**
 * The respectful angle a sensitive-trend run chose, as a muted read-only line
 * next to the current-moment line (run-page outputs, results). Renders nothing
 * when the run is not looking for one (or has no brief yet).
 */
export function SaferAngleLine({
  state,
  className,
}: {
  state: Record<string, unknown>;
  className?: string;
}) {
  const text = saferAngleText(state);
  if (!text) return null;
  return (
    <p className={cn("text-sm text-muted-foreground", className)}>
      <span className="font-medium">Safer angle:</span> {text}
    </p>
  );
}
