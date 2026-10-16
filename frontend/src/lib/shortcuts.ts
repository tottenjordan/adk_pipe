/**
 * The keyboard-shortcut registry: the single source for the inline key hints
 * (`<Kbd>`), the controls' `aria-keyshortcuts` and the `?` help dialog
 * (`components/shortcut-help.tsx`). A shortcut that exists in code but not here
 * is undiscoverable, so add it here first.
 *
 * Plain-key shortcuts (everything but Ctrl/⌘+Enter) never fire while typing in a
 * field (`isTypingTarget`) or with Ctrl, ⌘ or Alt held (`hasCommandModifier`).
 */

export type ShortcutScope = "global" | "new-run" | "review" | "proof";

export interface Shortcut {
  /** What the key does, as the help lists it. */
  label: string;
  scope: ShortcutScope;
  /** Alternative key combos, each a list of key caps (e.g. [["Ctrl", "Enter"], ["⌘", "Enter"]]). */
  combos: readonly (readonly string[])[];
  /** The ARIA `aria-keyshortcuts` value for the control the shortcut activates. */
  aria: string;
}

/** Help-dialog section titles, in display order. */
export const SCOPE_LABELS: Readonly<Record<ShortcutScope, string>> = {
  global: "Everywhere",
  "new-run": "New-run form",
  review: "Review checkpoints",
  proof: "Proof viewer",
};

const CTRL_ENTER = [
  ["Ctrl", "Enter"],
  ["⌘", "Enter"],
] as const;

export const SHORTCUTS = {
  help: { label: "Show keyboard shortcuts", scope: "global", combos: [["?"]], aria: "Shift+?" },
  startRun: {
    label: "Start the run",
    scope: "new-run",
    combos: CTRL_ENTER,
    aria: "Control+Enter Meta+Enter",
  },
  approve: {
    label: "Approve or confirm the open checkpoint",
    scope: "review",
    combos: CTRL_ENTER,
    aria: "Control+Enter Meta+Enter",
  },
  prev: { label: "Previous creative", scope: "proof", combos: [["←"]], aria: "ArrowLeft" },
  next: { label: "Next creative", scope: "proof", combos: [["→"]], aria: "ArrowRight" },
  rateVisual: {
    label: "Rate the visual (the default rating target)",
    scope: "proof",
    combos: [["V"]],
    aria: "V",
  },
  rateCopy: { label: "Rate the ad copy instead", scope: "proof", combos: [["C"]], aria: "C" },
  pass: { label: "Verdict: pass", scope: "proof", combos: [["P"]], aria: "P" },
  fail: { label: "Verdict: fail", scope: "proof", combos: [["F"]], aria: "F" },
  score: {
    label: "Score 1 to 5",
    scope: "proof",
    combos: [["1"], ["2"], ["3"], ["4"], ["5"]],
    aria: "1 2 3 4 5",
  },
  noScore: { label: "No score", scope: "proof", combos: [["0"]], aria: "0" },
  save: { label: "Save the rating", scope: "proof", combos: [["S"]], aria: "S" },
} as const satisfies Record<string, Shortcut>;

export type ShortcutId = keyof typeof SHORTCUTS;

/** The registry grouped by scope, in `SCOPE_LABELS` order (empty scopes dropped). */
export function shortcutsByScope(): { scope: ShortcutScope; title: string; items: Shortcut[] }[] {
  const all: Shortcut[] = Object.values(SHORTCUTS);
  return (Object.keys(SCOPE_LABELS) as ShortcutScope[])
    .map((scope) => ({
      scope,
      title: SCOPE_LABELS[scope],
      items: all.filter((s) => s.scope === scope),
    }))
    .filter((g) => g.items.length > 0);
}

/** Elements whose keys belong to the user's typing or to a widget's own key handling. */
const TYPING_SELECTOR = [
  "input",
  "textarea",
  "select",
  '[contenteditable]:not([contenteditable="false"])',
  '[role="textbox"]',
  '[role="combobox"]',
  '[role="listbox"]',
  '[role="menu"]',
  '[role="slider"]',
  '[role="spinbutton"]',
].join(", ");

/** True when a key event's target is a text field or a widget with its own keys. */
export function isTypingTarget(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest(TYPING_SELECTOR) !== null;
}

/** True with Ctrl, ⌘ or Alt held (Shift is part of typing `?`, so it's not counted). */
export function hasCommandModifier(e: Pick<KeyboardEvent, "ctrlKey" | "metaKey" | "altKey">): boolean {
  return e.ctrlKey || e.metaKey || e.altKey;
}
