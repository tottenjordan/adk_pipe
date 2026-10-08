/**
 * Per-run opt-in for rating-driven learning (creative agents only). Checked,
 * the session is seeded with `learn_from_ratings: true` (buildInitialState), so
 * the backend reads the brand's past human ratings for guidance, style
 * steering and stricter checks (creative_agent/rating_signals.py). Off by
 * default; restored by Duplicate brief.
 */
export function LearnFromRatingsToggle({
  brand,
  checked,
  onChange,
}: {
  brand: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  const name = brand.trim();
  const whose = name ? `earlier ${name} creatives` : "earlier creatives for this brand";
  return (
    <label
      htmlFor="learn-from-ratings"
      className="flex items-start gap-3 rounded-md border border-border bg-background px-3 py-2.5 cursor-pointer hover:border-foreground/20 transition-colors"
    >
      <input
        id="learn-from-ratings"
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 shrink-0 rounded-sm border-border accent-primary cursor-pointer"
      />
      <span className="space-y-0.5">
        <span className="block text-sm font-medium text-foreground">
          Learn from past ratings for this brand
        </span>
        <span className="block text-xs text-muted-foreground">
          {`Uses your team's ratings of ${whose} to steer styles, guidance and checks. Off by default.`}
        </span>
      </span>
    </label>
  );
}
