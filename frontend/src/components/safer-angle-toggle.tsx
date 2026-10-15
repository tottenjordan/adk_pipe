/** The sensitive-trend alert's advice line while the respectful angle is on. */
export const SAFER_ANGLE_ADVICE =
  "Creatives will look for a respectful angle; the event is never referenced.";

export const SAFER_ANGLE_HELP =
  "Builds on the positive side of the trend, like a person's legacy. The event itself is never referenced, and a named person appears only as a respectful tribute, never as an endorsement.";

/**
 * Per-run opt-in shown inside the home form's "Sensitive trend" alert
 * (creative agents, sensitive carried trend only). Checked, the session is
 * seeded with `safer_angle_opt_in: true` (buildInitialState). Off by default;
 * cleared when the trend is edited; restored by Duplicate brief.
 */
export function SaferAngleToggle({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label htmlFor="safer-angle" className="flex cursor-pointer items-start gap-3">
      <input
        id="safer-angle"
        type="checkbox"
        checked={checked}
        aria-labelledby="safer-angle-label"
        aria-describedby="safer-angle-help"
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded-sm border-border accent-primary"
      />
      <span className="space-y-0.5">
        <span id="safer-angle-label" className="block text-sm font-medium text-foreground">
          Look for a respectful angle
        </span>
        <span id="safer-angle-help" className="block text-xs text-muted-foreground">
          {SAFER_ANGLE_HELP}
        </span>
      </span>
    </label>
  );
}
